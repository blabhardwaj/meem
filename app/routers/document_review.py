"""
The "upload + scan + revise-in-chat + index" flow's HTTP surface.

  POST /documents/upload-file          upload a REAL file (PDF/DOCX/TXT/MD),
                                        auto-scan it, seed a review chat session
  POST /documents/review/message       one turn of that review conversation —
                                        revise via the drafting agent, or finalize
                                        (writes a new DocumentVersion, indexes on
                                        a passing/unflagged scan)
  POST /documents/{id}/upload-version  re-upload an existing document; seeds
                                        the version-diff-review chat against
                                        the new file's content (409 if it's
                                        identical to the current version —
                                        UI_FIXES_2026-09-15.md)
  POST /documents/{id}/start-edit      edit the CURRENT version via chat, no
                                        file re-upload — UI_FIXES_2026-09-15.md
  POST /documents/review/version-message  one turn of either version-review
                                        session above (same chat loop, same
                                        finalize)

Distinct from POST /documents/upload (pasted text, Phase 3) and from
/agents/draft/* (Phase 4 chat-drafting, decoupled from persistence,
completely untouched by this work).
"""

import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.api.dependencies import get_current_user, get_db_with_tenant
from app.database import get_db
from sqlalchemy.orm import Session


from app.models.document import Document, DocumentVersion
from app.models.project import Project
from app.models.team import Team
from app.services.access_control import can_edit_document, can_view_document, has_permission
from app.services.auth import ResolvedIdentity
from app.services.document_parser import DocumentParseError, UnsupportedDocumentTypeError
from app.services.document_persistence import (
    DocumentNotFoundError,
    PermissionDeniedError,
    StageNotFoundError,
    get_document_view_data,
    read_upload_safely,
    sanitize_filename,
    validate_file_magic,
)
from app.services.document_diff import diff_summary
from app.services.document_finalize import GrantOnlyAccessError
from app.services.document_review_chat import run_review_turn
from app.services.document_upload_review import upload_and_scan
from app.services.document_version_review import (
    DocumentNotFoundError as VersionReviewDocumentNotFoundError,
    NoChangesError,
    PermissionDeniedError as VersionReviewPermissionDeniedError,
    run_version_review_turn,
    start_version_review,
    start_version_review_from_current,
)
from app.services.graph.audit_engine import extract_sync_and_audit_document
from app.services.indexing import resolve_grounding_version
from app.services.document_delete import (
    DocumentNotFoundError as DeleteDocumentNotFoundError,
    PermissionDeniedError as DeletePermissionDeniedError,
    VersionNotDeletableError,
    VersionNotFoundError as DeleteVersionNotFoundError,
    can_delete_version,
    delete_document,
    delete_version,
)


router = APIRouter(prefix="/documents", tags=["document-review"])


# --- models ------------------------------------------------------------

class ScanOut(BaseModel):
    overall_score: int
    criteria: list[dict]
    summary: str


class UploadFileResponse(BaseModel):
    document_id: str
    version_id: str
    stage_id: str
    stage_name: str
    sensitivity_level: str
    uploaded_as_team_id: str
    status: str  # the version's real DocumentStatus — "pending_review" for
                 # an ordinary upload; "indexed" (or "needs_attention") if
                 # the uploader could auto-approve and the scan already
                 # decided the outcome (BUGFIXES_2026-09-15.md Bug 3)
    session_id: str  # review chat session id, pass to /review/message
    scan: ScanOut | None
    scan_error: str | None
    scan_skipped: bool  # True when the content-hash marker matched (reused score)
    reformed_content: str | None
    injection_flagged: bool
    injection_findings: list[dict]
    # Names of criteria scoring below PER_CRITERION_MINIMUM=8 (Master Plan v2,
    # item 6) — [] if none failed or the scan itself didn't run.
    failed_criteria: list[str] = []
    reply: str  # initial chat message summarizing the auto-scan result


class DocumentViewResponse(BaseModel):
    document_id: str
    filename: str
    version_id: str
    version_number: int | None
    version_status: str
    content_markdown: str
    sensitivity_level: str
    workflow_state: str | None
    created_at: str
    scan_overall_score: int | None
    scan_criteria: list[dict] | None
    scan_passed: bool | None
    injection_flagged: bool | None
    failed_criteria: list[str] = []
    # Item 10: cached R010 document-coherence issues (contradiction/duplicate/
    # unmet_requirement), already confidence-filtered by get_document_view_data.
    coherence_issues: list[dict] = []


class ReviewMessageRequest(BaseModel):
    document_id: uuid.UUID
    session_id: str
    message: str = ""


class ReviewMessageResponse(BaseModel):
    reply: str
    drafted: bool = False
    finalized: bool = False
    version_id: str | None = None
    version_number: int | None = None
    status: str | None = None
    scan: ScanOut | None = None
    scan_error: str | None = None
    reformed_content: str | None = None
    injection_flagged: bool | None = None
    injection_findings: list[dict] | None = None
    should_index: bool | None = None
    failed_criteria: list[str] = []


class DiffOut(BaseModel):
    added_lines: int
    removed_lines: int
    has_changes: bool
    unified_diff: str


class UploadVersionResponse(BaseModel):
    document_id: str
    session_id: str  # pass to /review/version-message
    content: str
    diff: DiffOut
    scan: ScanOut | None
    scan_error: str | None
    reformed_content: str | None
    injection_flagged: bool
    injection_findings: list[dict]
    failed_criteria: list[str] = []
    reply: str


class VersionReviewMessageRequest(BaseModel):
    document_id: uuid.UUID
    session_id: str
    message: str = ""


class VersionReviewMessageResponse(BaseModel):
    reply: str
    revised: bool = False
    finalized: bool = False
    diff: DiffOut | None = None
    content: str | None = None
    version_id: str | None = None
    version_number: int | None = None
    status: str | None = None
    scan: ScanOut | None = None
    scan_error: str | None = None
    reformed_content: str | None = None
    injection_flagged: bool | None = None
    injection_findings: list[dict] | None = None
    should_index: bool | None = None
    failed_criteria: list[str] = []
    workflow_reset: bool = False


class VersionOut(BaseModel):
    version_id: str
    version_number: int | None
    approval_outcome: str | None
    uploaded_by: str
    # Two distinct notions, shown separately — see list_versions() below for
    # why they're not collapsed into one "current" flag.
    is_latest: bool   # document.current_version_id — newest, for review/edit
    is_live: bool      # resolve_grounding_version() — what search/audits use
    # Per-version delete eligibility for the CALLING user — see
    # app/services/document_delete.py: never the live version, and a
    # non-admin only sees this true on their own version.
    can_delete: bool
    status: str
    file_size_bytes: int
    created_at: str


class VersionContentResponse(BaseModel):
    version_id: str
    version_number: int | None
    content_markdown: str


# --- helpers -------------------------------------------------------------

def _scan_summary_reply(outcome: dict, stage_name: str) -> str:
    scan = outcome["scan"]
    lines = []
    if outcome["scan_skipped"]:
        lines.append(
            f"Uploaded to **{stage_name}**. This file matches a document already scanned "
            f"during chat-drafting — reusing that score instead of re-scanning."
        )
    else:
        lines.append(f"Uploaded to **{stage_name}**. Running the Structure Scanner...")

    if scan is not None:
        lines.append(f"\n**Structure Scanner: {scan['overall_score']}/60**")
        if scan.get("summary"):
            lines.append(scan["summary"])
    elif outcome["scan_error"]:
        lines.append(f"\nThe Structure Scanner could not run: {outcome['scan_error']}")

    if outcome["reformed_content"]:
        if outcome.get("failed_criteria"):
            lines.append(
                f"\nBelow the minimum on: {', '.join(outcome['failed_criteria'])}. "
                "A suggested reform was generated. Ask me to revise the document, or tell me if "
                "the reform looks good and I'll fold it in."
            )
        else:
            lines.append(
                "\nThe score was below the quality threshold — a suggested reform was generated. "
                "Ask me to revise the document, or tell me if the reform looks good and I'll fold it in."
            )

    if outcome["injection_flagged"]:
        lines.append(
            "\n⚠️ **This document was flagged by the Injection Scanner** for prompt-injection-style "
            "content and needs human review before it can be indexed, regardless of its structural score."
        )

    # BUGFIXES_2026-09-15.md Bug 3: an uploader who already holds approve
    # rights and whose document passed the scan is auto-approved AND
    # indexed immediately — telling them to "finalize" here would be
    # actively wrong (implying it isn't done yet, when it already is).
    if outcome.get("auto_approved"):
        lines.append(
            "\n✅ **Approved and indexed** — you already have approval rights on this team and the "
            "document passed the Structure and Injection scans, so no further review step is needed. "
            "You can still make changes here in chat if you'd like; finalizing again would just save "
            "another version."
        )
    else:
        lines.append(
            "\nMake any changes you'd like here in chat, then say it looks good to finalize — that writes "
            "a new version of this document and, on a passing scan, indexes it."
        )
    return "\n".join(lines)


def _version_diff_reply(outcome: dict) -> str:
    diff = outcome["diff"]
    lines = []
    if not diff["has_changes"]:
        lines.append("This new file is identical to the current version — no changes detected.")
    else:
        lines.append(
            f"Compared to the current version: **+{diff['added_lines']} lines added, "
            f"-{diff['removed_lines']} lines removed**."
        )

    scan = outcome["scan"]
    if scan is not None:
        lines.append(f"\n**Structure Scanner: {scan['overall_score']}/60**")
    elif outcome["scan_error"]:
        lines.append(f"\nThe Structure Scanner could not run: {outcome['scan_error']}")

    if outcome["reformed_content"]:
        if outcome.get("failed_criteria"):
            lines.append(f"\nBelow the minimum on: {', '.join(outcome['failed_criteria'])}.")
        else:
            lines.append("\nThe score was below the quality threshold — a suggested reform was generated.")

    if outcome["injection_flagged"]:
        lines.append(
            "\n⚠️ **This file was flagged by the Injection Scanner** and needs human review before it "
            "can be indexed, regardless of its structural score."
        )

    lines.append("\nWould you like to make further changes, or should this be finalized?")
    return "\n".join(lines)


# --- endpoints -----------------------------------------------------------

@router.post("/upload-file", response_model=UploadFileResponse, status_code=201)
async def upload_file(
    file: UploadFile = File(...),
    stage_id: uuid.UUID = Form(...),
    team_id: uuid.UUID = Form(...),
    sensitivity_level: str = Form("internal"),
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):

    team = db.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")

    project = db.get(Project, team.project_id)
    if project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=403, detail="That team is not in your organization")

    role = identity.role_on_team(team_id, project.project_id)
    file_bytes = await read_upload_safely(file)
    if not file_bytes:
        raise HTTPException(status_code=422, detail="The uploaded file is empty")

    try:
        clean_filename = sanitize_filename(file.filename or "upload")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not validate_file_magic(file_bytes, clean_filename):
        raise HTTPException(status_code=415, detail="File content does not match claimed file type")

    session_id = str(uuid.uuid4())

    try:
        outcome = upload_and_scan(
            db,
            user_id=identity.user_id, team_id=team_id, project_id=project.project_id, role=role,
            stage_id=stage_id, original_filename=clean_filename,
            mime_type=file.content_type or "application/octet-stream",
            file_data=file_bytes, session_id=session_id, sensitivity=sensitivity_level,
        )

    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except StageNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except UnsupportedDocumentTypeError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except DocumentParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:  # bad sensitivity name, unknown user, etc.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    created = outcome["created"]
    return UploadFileResponse(
        document_id=str(created.document_id),
        version_id=str(created.version_id),
        stage_id=str(created.stage_id),
        stage_name=created.stage_name,
        sensitivity_level=created.sensitivity_level.name,
        uploaded_as_team_id=str(team_id),
        status=outcome["status"],
        session_id=session_id,
        scan=outcome["scan"],
        scan_error=outcome["scan_error"],
        scan_skipped=outcome["scan_skipped"],
        reformed_content=outcome["reformed_content"],
        injection_flagged=outcome["injection_flagged"],
        injection_findings=outcome["injection_findings"],
        failed_criteria=outcome["failed_criteria"],
        reply=_scan_summary_reply(outcome, created.stage_name),
    )


@router.get("/{document_id}/view", response_model=DocumentViewResponse)
def view_document(
    document_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    Master Plan v2, item 2: the document viewer. Full current-version
    content + latest scan result, for the Sources panel and the approval
    review modal — previously nothing rendered a document's actual content
    anywhere in the app.
    """
    document = db.get(Document, document_id)
    # Tenant check BEFORE the ABAC check, and a 404 (not 403) either way —
    # a cross-tenant probe must not be able to distinguish "wrong tenant"
    # from "doesn't exist".
    if document is None or document.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    if not can_view_document(db, identity.user_id, document):
        raise HTTPException(status_code=403, detail="You do not have permission to view this document")

    try:
        data = get_document_view_data(db, document_id)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return DocumentViewResponse(**data)


@router.post("/review/message", response_model=ReviewMessageResponse)
def review_message(
    body: ReviewMessageRequest,
    background_tasks: BackgroundTasks,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):

    document = db.get(Document, body.document_id)
    if document is None or document.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    # Same bar as uploading: the acting user must still have "upload" rights
    # on this document's team (org_admin/project_admin bypass, as everywhere
    # else). Deliberately not scoped to "only the original uploader" — any
    # teammate with upload rights on this stage/team can continue the review.
    if not has_permission(db, identity.user_id, "upload", document.uploaded_as_team_id, document.project_id):
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to revise documents for this team.",
        )

    try:
        turn = run_review_turn(
            db, session_id=body.session_id, document_id=document.document_id,
            user_id=identity.user_id, message=body.message,
        )
    except GrantOnlyAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — Groq / rate-limit / parse failures
        raise HTTPException(
            status_code=502,
            detail=f"The document-review agent could not complete this turn: {exc}",
        ) from exc

    # Master Plan v2, items 5 & 9: keep the knowledge graph, extracted
    # relationships/claims, and audit in sync with real usage automatically —
    # a finalize just wrote a new DocumentVersion (and possibly indexed it),
    # which can change R001/R002/R004/R008 findings.
    if turn.get("finalized") and turn.get("version_id"):
        background_tasks.add_task(
            extract_sync_and_audit_document,
            document.document_id, uuid.UUID(turn["version_id"]),
            document.project_id, identity.tenant_id, identity.user_id,
        )

    return ReviewMessageResponse(**turn)


@router.post("/{document_id}/upload-version", response_model=UploadVersionResponse, status_code=201)
async def upload_version(
    document_id: uuid.UUID,
    file: UploadFile = File(...),
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    Master Plan v2, item 12: re-uploading an already-existing document. Never
    silently replaces the current version — computes a real diff against it,
    runs the initial scan, and seeds a review session for the diff-review gate
    (see DocFlow_AI_Version_Check_Spec.md). Fires regardless of the target
    stage's requires_approval setting — independent of the human-approval gate.
    """
    document = db.get(Document, document_id)
    if document is None or document.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    file_bytes = await read_upload_safely(file)
    if not file_bytes:
        raise HTTPException(status_code=422, detail="The uploaded file is empty")

    try:
        clean_filename = sanitize_filename(file.filename or document.original_filename)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not validate_file_magic(file_bytes, clean_filename):
        raise HTTPException(status_code=415, detail="File content does not match claimed file type")

    session_id = str(uuid.uuid4())

    try:
        outcome = start_version_review(
            db, document_id=document_id, user_id=identity.user_id,
            original_filename=clean_filename,
            mime_type=file.content_type or "application/octet-stream",
            file_data=file_bytes, session_id=session_id,
        )
    except VersionReviewDocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VersionReviewPermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except NoChangesError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except UnsupportedDocumentTypeError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except DocumentParseError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return UploadVersionResponse(
        document_id=str(document_id),
        session_id=session_id,
        content=outcome["content"],
        diff=DiffOut(**outcome["diff"]),
        scan=outcome["scan"],
        scan_error=outcome["scan_error"],
        reformed_content=outcome["reformed_content"],
        injection_flagged=outcome["injection_flagged"],
        injection_findings=outcome["injection_findings"],
        failed_criteria=outcome["failed_criteria"],
        reply=_version_diff_reply(outcome),
    )


class StartEditResponse(BaseModel):
    document_id: str
    session_id: str  # pass to /review/version-message
    content: str
    version_number: int | None
    reply: str


@router.post("/{document_id}/start-edit", response_model=StartEditResponse, status_code=201)
def start_edit(
    document_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    UI_FIXES_2026-09-15.md: edit the CURRENT document via chat — no file
    re-upload needed. Seeds a review session from the current version's own
    content; the same /review/version-message chat loop and finalize path as
    upload-version take it from there (revise_document / confirm_revision).
    """
    document = db.get(Document, document_id)
    if document is None or document.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    session_id = str(uuid.uuid4())
    try:
        outcome = start_version_review_from_current(
            db, document_id=document_id, user_id=identity.user_id, session_id=session_id,
        )
    except VersionReviewDocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VersionReviewPermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    return StartEditResponse(
        document_id=str(document_id), session_id=session_id,
        content=outcome["content"], version_number=outcome["version_number"],
        reply=outcome["reply"],
    )


@router.post("/review/version-message", response_model=VersionReviewMessageResponse)
def version_review_message(
    body: VersionReviewMessageRequest,
    background_tasks: BackgroundTasks,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    document = db.get(Document, body.document_id)
    if document is None or document.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    # UI_FIXES_2026-09-15.md: this is the version-DIFF-review chat (editing an
    # existing, already-finalized document — via re-upload or start-edit),
    # not the looser first-upload review above — only the document's
    # uploader, or team_lead+, may continue it.
    if not can_edit_document(db, identity.user_id, document):
        raise HTTPException(
            status_code=403,
            detail="Only the document's uploader, a team lead, or a project/org admin can revise this document.",
        )

    try:
        turn = run_version_review_turn(
            db, session_id=body.session_id, document_id=document.document_id,
            user_id=identity.user_id, message=body.message,
        )
    except GrantOnlyAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 — Groq / rate-limit / parse failures
        raise HTTPException(
            status_code=502,
            detail=f"The version-review agent could not complete this turn: {exc}",
        ) from exc

    if turn.get("diff") is not None:
        turn["diff"] = DiffOut(**turn["diff"])

    if turn.get("finalized") and turn.get("version_id"):
        background_tasks.add_task(
            extract_sync_and_audit_document,
            document.document_id, uuid.UUID(turn["version_id"]),
            document.project_id, identity.tenant_id, identity.user_id,
        )

    return VersionReviewMessageResponse(**turn)


@router.get("/{document_id}/versions", response_model=list[VersionOut])
def list_versions(
    document_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    document = db.get(Document, document_id)
    if document is None or document.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    if not can_view_document(db, identity.user_id, document):
        raise HTTPException(status_code=403, detail="You do not have permission to view this document")

    # Ordered by created_at, not version_number: version_number is now only
    # assigned once a version is APPROVED (None until then), so it no
    # longer reflects true chronological order among unresolved drafts/
    # rejections sitting between approved versions.
    versions = (
        db.query(DocumentVersion)
        .filter(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.created_at.desc())
        .all()
    )
    # Two genuinely different notions of "which version matters right now",
    # deliberately shown separately rather than collapsed into one
    # "current" label (that ambiguity is exactly what UI feedback caught):
    #   - is_latest: document.current_version_id — the newest version,
    #     whatever its state. What a reviewer/uploader should see/act on.
    #   - is_live: resolve_grounding_version()'s answer — the last version
    #     that's genuinely approved+indexed. What Search Agent answers and
    #     audit rules are actually grounded in. Can be an OLDER version than
    #     the latest one, when a newer draft/rejection sits unresolved on
    #     top of it.
    live_version = resolve_grounding_version(db, document_id)
    live_version_id = live_version.version_id if live_version is not None else None
    live_created_at = live_version.created_at if live_version is not None else None
    is_admin = identity.is_org_admin or document.project_id in identity.project_admin_project_ids

    def _can_delete(v: DocumentVersion) -> bool:
        if v.version_id == live_version_id:
            return False
        if live_created_at is not None and v.created_at <= live_created_at:
            return False
        return can_delete_version(identity.user_id, v, is_admin=is_admin)

    return [
        VersionOut(
            version_id=str(v.version_id),
            version_number=v.version_number,
            approval_outcome=v.approval_outcome.value if v.approval_outcome else None,
            uploaded_by=str(v.uploaded_by),
            is_latest=(v.version_id == document.current_version_id),
            is_live=(v.version_id == live_version_id),
            can_delete=_can_delete(v),
            status=v.status.value,
            file_size_bytes=v.file_size_bytes,
            created_at=v.created_at.isoformat(),
        )
        for v in versions
    ]


@router.get("/{document_id}/versions/{version_id}/content", response_model=VersionContentResponse)
def get_version_content(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """Master Plan v2, item 12: view/diff an older version's content — reuses
    the same ABAC check as item 2's document viewer (view_document above)."""
    document = db.get(Document, document_id)
    if document is None or document.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    if not can_view_document(db, identity.user_id, document):
        raise HTTPException(status_code=403, detail="You do not have permission to view this document")

    version = db.get(DocumentVersion, version_id)
    if version is None or version.document_id != document_id:
        raise HTTPException(status_code=404, detail="Version not found")

    content = version.file_data
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")

    return VersionContentResponse(
        version_id=str(version.version_id),
        version_number=version.version_number,
        content_markdown=content,
    )


@router.get("/{document_id}/versions/diff", response_model=DiffOut)
def get_versions_diff(
    document_id: uuid.UUID,
    from_version_id: uuid.UUID,
    to_version_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    Line-level diff between any two versions of this document, for the
    Versions panel's compare view (not tied to the current/uploading-a-new-
    version flow the way the review-chat diffs are). Reuses the exact same
    can_view_document ABAC check and diff_summary() the review-chat diffs
    use — this is purely a read, no session/chat/finalize involved.
    """
    document = db.get(Document, document_id)
    if document is None or document.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    if not can_view_document(db, identity.user_id, document):
        raise HTTPException(status_code=403, detail="You do not have permission to view this document")

    from_version = db.get(DocumentVersion, from_version_id)
    to_version = db.get(DocumentVersion, to_version_id)
    if from_version is None or from_version.document_id != document_id:
        raise HTTPException(status_code=404, detail="'from' version not found")
    if to_version is None or to_version.document_id != document_id:
        raise HTTPException(status_code=404, detail="'to' version not found")

    def _text(version: DocumentVersion) -> str:
        content = version.file_data
        return content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content

    result = diff_summary(_text(from_version), _text(to_version))
    return DiffOut(**result)


# --- delete -------------------------------------------------------------

class DeleteDocumentRequest(BaseModel):
    # Frontend UX gate (type "delete" to confirm) — enforced here too so the
    # API itself can't be used to skip it.
    confirm_text: str


@router.delete("/{document_id}/versions/{version_id}", status_code=204)
def delete_version_endpoint(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    Per-version delete (SESSION_HANDOFF_2026-09-16.md): a contributor may
    delete only their own version, and only one uploaded after the
    document's current live version. project_admin/org_admin may delete any
    such qualifying version. The live version itself is never deletable
    here — no revert/replace path, plain deletion of non-live versions only.
    """
    document = db.get(Document, document_id)
    if document is None or document.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    if not can_view_document(db, identity.user_id, document):
        raise HTTPException(status_code=403, detail="You do not have permission to view this document")

    is_admin = identity.is_org_admin or document.project_id in identity.project_admin_project_ids
    try:
        delete_version(
            db, document_id=document_id, version_id=version_id,
            actor_id=identity.user_id, is_admin=is_admin,
        )
    except DeleteDocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DeleteVersionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DeletePermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except VersionNotDeletableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/{document_id}", status_code=204)
def delete_document_endpoint(
    document_id: uuid.UUID,
    body: DeleteDocumentRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    Whole-document delete — project_admin/org_admin only. Wipes the
    document and every version. Frontend gates this behind typing "delete"
    then a second "are you sure?" confirm; this endpoint re-checks the
    typed confirmation text so the API itself enforces the same bar.
    """
    document = db.get(Document, document_id)
    if document is None or document.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")

    is_admin = identity.is_org_admin or document.project_id in identity.project_admin_project_ids
    if not is_admin:
        raise HTTPException(
            status_code=403, detail="Only a project or organization admin can delete a document."
        )
    if body.confirm_text.strip().lower() != "delete":
        raise HTTPException(status_code=400, detail='Type "delete" to confirm.')

    try:
        delete_document(db, document_id=document_id, actor_id=identity.user_id)
    except DeleteDocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DeletePermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
