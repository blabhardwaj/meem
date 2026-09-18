"""
HTTP surface for the Drafting and Search agents — the CLI's chat loop exposed
over HTTP.

  GET  /agents/draft/layouts            built-in standard section layouts (item 14; kept for backward-compat, not surfaced in the UI — see UI_FIXES_2026-09-15.md #1)
  POST /agents/draft/extract-outline    upload a reference doc -> content-stripped outline (item 14)
  POST /agents/draft/message            one drafting turn { session_id, message, layout? }
  GET  /agents/draft/download/{filename} download a finalized draft
  POST /agents/search/message           one Search turn { session_id?, project_id, message } — merged RAG + Query (see UI_FIXES_2026-09-15.md #5)

DECOUPLED FROM PERSISTENCE (MERGE_DECISIONS §4): /draft never creates a
Document row, never touches has_permission(), and never asks about a stage/
team/project — it only requires authentication, and there is no ABAC because
there is no project resource involved. /search IS project-scoped: it checks
project access and enforces per-(user, project) conversation ownership.

The standalone Scanner chat (/agents/scan/message) was removed
(UI_FIXES_2026-09-15.md #4) — every real upload/finalize/new-version already
runs the full scan automatically via app/services/document_finalize.py; the
interactive "paste text, get it scored" tab was a redundant, disconnected
surface for the same pipeline.
"""

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db_with_tenant
from app.models.document import DocumentScan, ScanReviewStatus
from app.models.project import Project
from app.models.team import Team
from app.models.user import User
from app.services.access_control import has_any_project_access, resolve_sensitivity
from app.services.auth import ResolvedIdentity
from app.services.chat_history import SessionScopeError
from app.services.document_parser import DocumentParseError, UnsupportedDocumentTypeError, parse_document_to_markdown
from app.services.document_persistence import (
    PermissionDeniedError,
    StageNotFoundError,
    _check_upload_access,
    _persist_new_document,
    read_upload_safely,
    sanitize_filename,
    validate_file_magic,
)
from app.services.draft_chat import run_draft_turn
from app.services.draft_export import DRAFTS_DIR
from app.services.draft_layouts import list_builtin_layouts
from app.services.draft_workspace import (
    DraftNotFoundError,
    DraftPermissionError,
    get_finalized_draft,
)
from app.services.outline_extraction import OutlineExtractionError, extract_outline_from_reference
from app.services.search_chat import SearchTurnError, run_search_turn

router = APIRouter(prefix="/agents", tags=["agents"])


class LayoutSection(BaseModel):
    name: str
    purpose: str


class DraftMessageRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    message: str = Field(default="", max_length=20000)
    project_id: uuid.UUID | None = None
    # Master Plan v2, item 14: a standard section layout (built-in or
    # extracted from an uploaded reference doc) to fold into the FIRST
    # drafting turn only. Ignored on later turns (see draft_chat.py).
    layout: list[LayoutSection] | None = None


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
    db: Session = Depends(get_db_with_tenant),
):
    # session_id is the caller's own opaque conversation id (a UUID from the
    # frontend). It scopes the on-disk working file — see draft_workspace.
    safe_id = Path(body.session_id).name
    if safe_id != body.session_id:
        raise HTTPException(status_code=422, detail="Invalid session_id")

    # Resolved once, before closing the connection below: the display name
    # used to fill a draft's "[Author Name]"/"[Owner Name]" sign-off
    # placeholder deterministically (see draft_workspace.apply_author_placeholder)
    # rather than leaving it to the LLM, which does not reliably fill it in.
    author_name = (
        db.query(User.full_name).filter(User.user_id == identity.user_id).scalar()
        or identity.email
    )

    # Close this request's DB connection before the slow LLM turn below —
    # same reasoning as search_message's identical fix just above: this
    # session (shared with get_current_user via _request_db) would otherwise
    # sit open and idle-in-transaction for the whole run_draft_turn() call,
    # doubling the pool connections pinned per concurrent Draft request.
    db.close()

    try:
        turn = run_draft_turn(
            safe_id,
            body.message,
            user_id=identity.user_id,
            project_id=body.project_id,
            tenant_id=identity.tenant_id,
            layout=[s.model_dump() for s in body.layout] if body.layout else None,
            author_name=author_name,
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


class LayoutOut(BaseModel):
    id: str
    label: str
    document_type: str
    sections: list[LayoutSection]


@router.get("/draft/layouts", response_model=list[LayoutOut])
def list_draft_layouts(identity: ResolvedIdentity = Depends(get_current_user)):
    """Master Plan v2, item 14: the built-in standard section layouts for the
    chat-drafting template picker. No storage — authored once in draft_layouts.py."""
    return list_builtin_layouts()


class ExtractOutlineResponse(BaseModel):
    sections: list[LayoutSection]


@router.post("/draft/extract-outline", response_model=ExtractOutlineResponse)
async def extract_outline(
    file: UploadFile = File(...),
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    Master Plan v2, item 14: "Upload template" — the user uploads a real
    reference document (e.g. an old PRD), and this returns ONLY its section
    structure, content stripped (see outline_extraction.py's system prompt
    for the exact guarantee). Nothing is persisted; the caller holds onto
    the returned sections and passes them back as `layout` on the first
    /agents/draft/message call to apply them.
    """
    file_bytes = await read_upload_safely(file)
    if not file_bytes:
        raise HTTPException(status_code=422, detail="The uploaded file is empty")

    try:
        clean_filename = sanitize_filename(file.filename or "template")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not validate_file_magic(file_bytes, clean_filename):
        raise HTTPException(status_code=415, detail="File content does not match claimed file type")

    try:
        content = parse_document_to_markdown(
            file_bytes, file.content_type or "application/octet-stream", clean_filename
        )
    except UnsupportedDocumentTypeError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except DocumentParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        sections = extract_outline_from_reference(content)
    except OutlineExtractionError as exc:
        raise HTTPException(status_code=502, detail=f"Could not extract a template from this file: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — Groq/network failures (rate limit, timeout, etc.)
        raise HTTPException(status_code=502, detail=f"Could not extract a template from this file: {exc}") from exc

    if not sections:
        raise HTTPException(
            status_code=422,
            detail="This document doesn't have a clear section structure to use as a template.",
        )

    return ExtractOutlineResponse(sections=sections)


class SearchMessageRequest(BaseModel):
    # Omit on the first turn; pass the session_id from the previous response
    # to continue the same conversation.
    session_id: str | None = Field(default=None, max_length=200)
    project_id: uuid.UUID
    message: str = Field(min_length=1, max_length=20000)


class SearchMessageResponse(BaseModel):
    reply: str
    tools_called: list[str] = []
    session_id: str  # canonical id — echo it back on the next turn
    timing: dict = {}


@router.post("/search/message", response_model=SearchMessageResponse)
def search_message(
    body: SearchMessageRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    One turn of the merged Search tab — content questions (grounded in
    indexed documents) and read-only metadata questions (status, versions,
    approvals) in one conversation. Replaces the former separate /rag and
    /query endpoints. Project-scoped: project access is checked, and
    conversation ownership is enforced per (user, project).
    """
    if body.session_id is not None and Path(body.session_id).name != body.session_id:
        raise HTTPException(status_code=422, detail="Invalid session_id")

    project = db.get(Project, body.project_id)
    if project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")
    if not has_any_project_access(db, identity.user_id, body.project_id):
        raise HTTPException(status_code=403, detail="You don't have access to this project")

    # Close this request's DB connection now, before the slow part. This
    # endpoint's session is shared with get_current_user (see
    # api/dependencies.py's _request_db) and would otherwise sit open and
    # idle-in-transaction for the entire run_search_turn() call below — a
    # multi-second-to-multi-minute Groq LLM turn that itself opens its own
    # separate SessionLocal() (in search_chat.py), plus one more per RAG/
    # Query tool call. Holding this connection open the whole time doubled
    # (or worse) the pool connections pinned per concurrent Search request;
    # under real traffic that exhausted the pool (pool_size=5+max_overflow=5)
    # and starved unrelated requests, some of which then failed ABAC/RLS
    # checks in ways that looked like access bugs but were actually
    # pool-exhaustion-induced failures. db.close() here is safe: nothing
    # below uses `db` or a live object bound to it again.
    db.close()

    try:
        turn = run_search_turn(
            user_id=identity.user_id,
            project_id=body.project_id,
            tenant_id=identity.tenant_id,
            session_id=body.session_id,
            message=body.message,
        )
    except SessionScopeError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except SearchTurnError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"The Search agent could not complete this turn: {exc}",
        ) from exc
    except Exception as exc:  # noqa: BLE001 — Groq / rate-limit / retrieval failures
        raise HTTPException(
            status_code=502,
            detail=f"The Search agent could not complete this turn: {exc}",
        ) from exc

    return SearchMessageResponse(
        reply=turn["reply"],
        tools_called=turn["tools_called"],
        session_id=turn["session_id"],
        timing=turn.get("timing", {}),
    )


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
    db: Session = Depends(get_db_with_tenant),
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
