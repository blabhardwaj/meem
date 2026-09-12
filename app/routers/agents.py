"""
HTTP surface for the Drafting, RAG, and Scanner agents — the CLI's chat loop
exposed over HTTP.

  POST /agents/draft/message            one drafting turn { session_id, message }
  GET  /agents/draft/download/{filename} download a finalized draft
  POST /agents/rag/message              one RAG turn { session_id?, project_id, message }
  POST /agents/query/message            one Query turn { session_id?, project_id, message }
  POST /agents/scan/message             one standalone-scan turn { session_id, message }

DECOUPLED FROM PERSISTENCE (MERGE_DECISIONS §4): /draft and /scan never create a
Document row, never touch has_permission(), and never ask about a stage/team/
project — they only require authentication, and there is no ABAC because there
is no project resource involved. /rag IS project-scoped: it checks project
access and enforces per-(user, project) conversation ownership.
"""

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.database import get_db
from app.models.document import DocumentScan, ScanReviewStatus
from app.models.project import Project
from app.models.team import Team
from app.services.access_control import has_any_project_access, resolve_sensitivity
from app.services.auth import ResolvedIdentity
from app.services.chat_history import SessionScopeError
from app.services.document_persistence import (
    PermissionDeniedError,
    StageNotFoundError,
    _check_upload_access,
    _persist_new_document,
)
from app.services.draft_chat import run_draft_turn
from app.services.draft_export import DRAFTS_DIR
from app.services.draft_workspace import (
    DraftNotFoundError,
    DraftPermissionError,
    get_finalized_draft,
)
from app.services.query_chat import QueryTurnError, run_query_turn
from app.services.rag_chat import run_rag_turn
from app.services.scan_chat import ScanTurnError, run_scan_turn

router = APIRouter(prefix="/agents", tags=["agents"])


class DraftMessageRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    message: str = Field(default="", max_length=20000)
    project_id: uuid.UUID | None = None


class DraftMessageResponse(BaseModel):
    reply: str
    drafted: bool = False
    finalized: bool = False
    scan: dict | None = None
    scan_error: str | None = None
    final_content: str | None = None
    draft_content: str | None = None
    download_url: str | None = None
    filename: str | None = None
    draft_id: str | None = None
    session_id: str | None = None


@router.post("/draft/message", response_model=DraftMessageResponse)
def draft_message(
    body: DraftMessageRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
):
    # session_id is the caller's own opaque conversation id (a UUID from the
    # frontend). It scopes the on-disk working file — see draft_workspace.
    safe_id = Path(body.session_id).name
    if safe_id != body.session_id:
        raise HTTPException(status_code=422, detail="Invalid session_id")

    try:
        turn = run_draft_turn(
            safe_id,
            body.message,
            user_id=identity.user_id,
            project_id=body.project_id,
        )
    except Exception as exc:  # noqa: BLE001 — Groq / rate-limit / parse failures
        raise HTTPException(
            status_code=502,
            detail=f"The drafting agent could not complete this turn: {exc}",
        ) from exc

    return DraftMessageResponse(
        reply=turn["reply"],
        drafted=turn["drafted"],
        finalized=turn["finalized"],
        scan=turn["scan"],
        scan_error=turn["scan_error"],
        final_content=turn["final_content"],
        draft_content=turn.get("draft_content"),
        filename=turn["filename"],
        draft_id=turn.get("draft_id"),
        session_id=turn.get("session_id", safe_id),
        download_url=(
            f"/agents/draft/download/{turn['filename']}" if turn["filename"] else None
        ),
    )


class RagMessageRequest(BaseModel):
    # Omit on the first turn; pass the session_id from the previous response
    # to continue the same conversation.
    session_id: str | None = Field(default=None, max_length=200)
    project_id: uuid.UUID
    message: str = Field(min_length=1, max_length=20000)


class RagMessageResponse(BaseModel):
    reply: str
    tools_called: list[str] = []
    session_id: str  # canonical id — echo it back on the next turn
    timing: dict = {}


@router.post("/rag/message", response_model=RagMessageResponse)
def rag_message(
    body: RagMessageRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.session_id is not None and Path(body.session_id).name != body.session_id:
        raise HTTPException(status_code=422, detail="Invalid session_id")

    project = db.get(Project, body.project_id)
    if project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")
    if not has_any_project_access(db, identity.user_id, body.project_id):
        raise HTTPException(status_code=403, detail="You don't have access to this project")

    try:
        turn = run_rag_turn(
            user_id=identity.user_id,
            project_id=body.project_id,
            session_id=body.session_id,
            message=body.message,
        )
    except SessionScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — Groq / rate-limit / retrieval failures
        raise HTTPException(
            status_code=502,
            detail=f"The RAG agent could not complete this turn: {exc}",
        ) from exc

    return RagMessageResponse(
        reply=turn["reply"],
        tools_called=turn["tools_called"],
        session_id=turn["session_id"],
        timing=turn.get("timing", {}),
    )


class QueryMessageRequest(BaseModel):
    # Omit on the first turn; pass the session_id from the previous response
    # to continue the same conversation.
    session_id: str | None = Field(default=None, max_length=200)
    project_id: uuid.UUID
    message: str = Field(min_length=1, max_length=20000)


class QueryMessageResponse(BaseModel):
    reply: str
    tools_called: list[str] = []
    session_id: str  # canonical id — echo it back on the next turn


@router.post("/query/message", response_model=QueryMessageResponse)
def query_message(
    body: QueryMessageRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    One turn of the read-only metadata Q&A chat (the Query tab). Project-scoped
    exactly like /rag/message: project access is checked, and conversation
    ownership is enforced per (user, project).
    """
    if body.session_id is not None and Path(body.session_id).name != body.session_id:
        raise HTTPException(status_code=422, detail="Invalid session_id")

    project = db.get(Project, body.project_id)
    if project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")
    if not has_any_project_access(db, identity.user_id, body.project_id):
        raise HTTPException(status_code=403, detail="You don't have access to this project")

    try:
        turn = run_query_turn(
            user_id=identity.user_id,
            project_id=body.project_id,
            session_id=body.session_id,
            message=body.message,
        )
    except SessionScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except QueryTurnError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The Query agent could not complete this turn: {exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001 — Groq / rate-limit failures
        raise HTTPException(
            status_code=502,
            detail=f"The Query agent could not complete this turn: {exc}",
        ) from exc

    return QueryMessageResponse(
        reply=turn["reply"],
        tools_called=turn["tools_called"],
        session_id=turn["session_id"],
    )


class ScanMessageRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    message: str = Field(min_length=1, max_length=200000)


class ScanMessageResponse(BaseModel):
    reply: str
    tools_called: list[str] = []


@router.post("/scan/message", response_model=ScanMessageResponse)
def scan_message(
    body: ScanMessageRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    One turn of the standalone Structure Scanner chat (DEFERRED_ITEMS.md #4):
    paste content, get it scored / reformed / injection-checked independently
    of any drafting or upload flow.

    Like /draft/message this is decoupled from persistence — authentication
    only, no project/stage/team, no ABAC (nothing is written anywhere).
    session_id is the caller's own opaque conversation id; it only scopes the
    Scanner Agent's in-context history.
    """
    safe_id = Path(body.session_id).name
    if safe_id != body.session_id:
        raise HTTPException(status_code=422, detail="Invalid session_id")

    try:
        turn = run_scan_turn(safe_id, body.message)
    except ScanTurnError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The scanner agent could not complete this turn: {exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001 — Groq / rate-limit / parse failures
        raise HTTPException(
            status_code=502,
            detail=f"The scanner agent could not complete this turn: {exc}",
        ) from exc

    return ScanMessageResponse(reply=turn["reply"], tools_called=turn["tools_called"])


@router.get("/draft/download/{filename}")
def download_draft(
    filename: str,
    identity: ResolvedIdentity = Depends(get_current_user),
):
    safe = Path(filename).name
    if safe != filename or not safe.endswith(".md"):
        raise HTTPException(status_code=422, detail="Invalid filename")
    path = DRAFTS_DIR / safe
    if not path.is_file():
        raise HTTPException(status_code=404, detail="That drafted file was not found")
    return FileResponse(path, media_type="text/markdown", filename=safe)


class UploadDraftToProjectRequest(BaseModel):
    draft_id: str = Field(min_length=1, max_length=200)
    project_id: uuid.UUID
    stage_id: uuid.UUID
    team_id: uuid.UUID
    sensitivity_level: str = "internal"


class UploadDraftToProjectResponse(BaseModel):
    document_id: str
    version_id: str
    project_id: str
    stage_id: str
    stage_name: str
    uploaded_as_team_id: str
    sensitivity_level: str
    original_filename: str
    workflow_state: str | None


@router.post("/draft/upload-to-project", response_model=UploadDraftToProjectResponse, status_code=201)
def upload_draft_to_project(
    body: UploadDraftToProjectRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Directly persist an already-finalized Drafting Agent artifact into a project
    without browser download/re-upload or redundant scanning.
    """
    # 1. Resolve finalized draft artifact & enforce user ownership
    try:
        draft_meta = get_finalized_draft(body.draft_id, user_id=identity.user_id)
    except DraftNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DraftPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    # 2. Check project existence and tenant boundary
    project = db.get(Project, body.project_id)
    if project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")

    # 3. Check team
    team = db.get(Team, body.team_id)
    if team is None or team.project_id != project.project_id:
        raise HTTPException(status_code=400, detail="Team does not belong to this project")

    # 4. Check user has project access and resolve role
    if not has_any_project_access(db, identity.user_id, project.project_id):
        raise HTTPException(status_code=403, detail="You do not have access to this project")

    role = identity.role_on_team(body.team_id, project.project_id)

    # 5. Check stage & upload permissions via existing upload gate
    try:
        stage = _check_upload_access(
            db,
            user_id=identity.user_id,
            team_id=body.team_id,
            project_id=project.project_id,
            stage_id=body.stage_id,
        )
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except StageNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    final_sensitivity = resolve_sensitivity(body.sensitivity_level, role)

    # 6. Direct persistence using server-side bytes (NO SCANNER INVOKED)
    file_bytes = draft_meta["content_bytes"]
    filename = draft_meta.get("filename") or f"draft-{body.draft_id[:8]}.md"

    created = _persist_new_document(
        db,
        user_id=identity.user_id,
        team_id=body.team_id,
        project_id=project.project_id,
        stage=stage,
        final_sensitivity=final_sensitivity,
        original_filename=filename,
        mime_type="text/markdown",
        file_data=file_bytes,
    )

    # 7. Safely associate existing completed scan record if present (do not execute scanner)
    if draft_meta.get("scan") and isinstance(draft_meta["scan"], dict):
        scan_data = draft_meta["scan"]
        if "overall_score" in scan_data:
            db.add(DocumentScan(
                version_id=created.version_id,
                overall_score=scan_data["overall_score"],
                criteria=scan_data.get("criteria", []),
                reform_triggered=False,
                reformed_content=None,
                injection_flagged=False,
                injection_findings=None,
                review_status=ScanReviewStatus.pending,
            ))
            db.commit()

    return UploadDraftToProjectResponse(
        document_id=str(created.document_id),
        version_id=str(created.version_id),
        project_id=str(project.project_id),
        stage_id=str(created.stage_id),
        stage_name=created.stage_name,
        uploaded_as_team_id=str(body.team_id),
        sensitivity_level=created.sensitivity_level.name,
        original_filename=filename,
        workflow_state=created.workflow_state,
    )
