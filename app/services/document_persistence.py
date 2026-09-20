"""
Document persistence — real DB writes, gated by has_permission() AND
has_stage_access() (Phase A Part 3: a team also needs a team_stage_access
grant for the target stage, org_admin/project_admin bypass both).

create_document() / create_document_from_file() take the acting identity
(user_id / team_id / project_id / role) as EXPLICIT parameters. Callers
decide where "who is doing this" comes from:
  - the CLI draft flow passes session_context values (app/tools/draft_tools.py)
  - the HTTP upload endpoints pass the authenticated ResolvedIdentity
    (app/routers/documents.py, app/routers/document_review.py)

Every new document version starts at status=pending_review — nothing is
indexed at upload time. A version only reaches `indexed` via the
document-review finalize step (app/services/document_finalize.py), after a
passing/unflagged scan.

The caller owns the SQLAlchemy Session's lifecycle; these functions commit
their unit of work but never close the session.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models.document import (
    Document,
    DocumentScan,
    DocumentVersion,
    DocumentTeamVisibility,
    SensitivityLevel,
)
from app.models.stage import Stage
from app.models.user import User
from app.models.workflow import WorkflowState, WorkflowStatus
from app.models.team import GrantTier
from app.services.access_control import has_permission, has_stage_access, resolve_sensitivity, resolve_stage_grant
from app.services.audit import record_audit
from app.services.document_finalize import failed_criteria, _meets_quality_bar
from app.services.document_parser import parse_document_to_markdown

from pathlib import Path
import re
import magic
from fastapi import HTTPException, UploadFile

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

def sanitize_filename(name: str) -> str:
    """Strip path components and remove any character not alphanumeric, whitespace, dot, dash, or underscore."""
    # Strip directory components (path traversal prevention)
    base = Path(name).name
    cleaned = re.sub(r"[^\w\s\-\.]", "", base).strip()
    if not cleaned:
        raise ValueError("Filename contains only invalid characters")
    return cleaned[:255]

def validate_file_magic(file_bytes: bytes, claimed_filename: str) -> bool:
    ext = ("." + claimed_filename.rsplit(".", 1)[-1]).lower() if "." in claimed_filename else ""
    allowed_ext_mimes = {
        ".pdf": ["application/pdf"],
        ".docx": ["application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/zip"],
        ".txt": ["text/plain"],
        ".md": ["text/plain", "text/markdown"],
    }
    if ext not in allowed_ext_mimes:
        return False
    detected_mime = magic.from_buffer(file_bytes, mime=True)
    return detected_mime in allowed_ext_mimes[ext]

async def read_upload_safely(file: UploadFile) -> bytes:
    content = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds 10 MB limit")
    return content


class PermissionDeniedError(Exception):
    pass



class StageNotFoundError(Exception):
    pass


@dataclass
class CreatedDocument:
    document_id: uuid.UUID
    version_id: uuid.UUID
    stage_id: uuid.UUID
    stage_name: str
    sensitivity_level: SensitivityLevel
    # "draft" when the stage requires approval (a WorkflowState row was created),
    # None when it does not (no row — approval is not applicable).
    workflow_state: str | None


@dataclass
class CreatedDocumentFromFile(CreatedDocument):
    # The parsed Markdown (via Docling for PDF/DOCX, direct decode for TXT/MD)
    # — what the Scanner and the review chat operate on. file_data on the
    # DocumentVersion row is the RAW uploaded bytes, untouched.
    parsed_content: str = ""


def _check_upload_access(
    db: Session, *, user_id: uuid.UUID, team_id: uuid.UUID, project_id: uuid.UUID, stage_id: uuid.UUID,
) -> Stage:
    """
    Shared gate for both creation paths: has_permission + has_stage_access
    (native team-role path), OR an active contributor/contributor_confidential
    stage grant whose OWN routing team_id matches the team_id being uploaded
    as (spec §3.2 — tier and team_id are one entitlement, checked together;
    a grant never authorizes uploading as an arbitrary team).
    """
    stage = db.get(Stage, stage_id)
    if stage is None or stage.project_id != project_id or stage.deleted_at is not None:
        raise StageNotFoundError(
            f"Stage {stage_id} does not exist in this project (or has been deleted)."
        )

    native_ok = (
        has_permission(db, user_id, "upload", team_id, project_id)
        and has_stage_access(db, user_id, team_id, stage_id, project_id)
    )
    if not native_ok:
        grant = resolve_stage_grant(db, user_id, stage_id)
        grant_ok = (
            grant is not None
            and grant.tier in (GrantTier.contributor, GrantTier.contributor_confidential)
            and grant.team_id == team_id
        )
        if not grant_ok:
            raise PermissionDeniedError(
                f"You do not have permission to upload to the '{stage.name}' stage as this team."
            )

    return stage


def _persist_new_document(
    db: Session,
    *,
    user_id: uuid.UUID,
    team_id: uuid.UUID,
    project_id: uuid.UUID,
    stage: Stage,
    final_sensitivity: SensitivityLevel,
    original_filename: str,
    mime_type: str,
    file_data: bytes,
) -> CreatedDocument:
    """Shared DB-write core: Document + DocumentVersion (v1, pending_review) +
    DocumentTeamVisibility + WorkflowState (if the stage requires approval) +
    audit. Access checks are the caller's responsibility (_check_upload_access)."""
    user = db.get(User, user_id)
    if user is None:
        raise ValueError(f"Unknown user: {user_id}")

    document_id = uuid.uuid4()
    version_id = uuid.uuid4()

    doc = Document(
        document_id=document_id,
        tenant_id=user.tenant_id,  # denormalized onto Document per design
        project_id=project_id,
        stage_id=stage.stage_id,
        uploaded_by=user_id,
        uploaded_as_team_id=team_id,
        sensitivity_level=final_sensitivity,
        original_filename=original_filename,
        mime_type=mime_type,
    )
    version = DocumentVersion(
        version_id=version_id,
        document_id=document_id,
        # No version_number: unassigned until approve_document() promotes
        # this (or a later revision of it) to actually approved — see
        # DocumentVersion's docstring.
        file_data=file_data,
        file_size_bytes=len(file_data),
        uploaded_by=user_id,
        # No status= override: every new upload starts pending_review (the
        # model's own default) — nothing is indexed until it passes the
        # document-review finalize step (document_finalize.py).
    )
    db.add_all([doc, version])
    db.flush()  # so document_id / version_id are usable before commit
    doc.current_version_id = version_id

    db.add(DocumentTeamVisibility(document_id=document_id, team_id=team_id))

    # Approval workflow (MERGE_DECISIONS §3/4): a WorkflowState row is created
    # ONLY if this document's stage requires sign-off. No row == not applicable.
    #
    # BUGFIXES_2026-09-15.md: this used to auto-approve here (immediately, at
    # creation) whenever the uploader already held 'approve' permission
    # (team_lead/admin) — but that ran BEFORE the Structure/Injection Scanner
    # (upload_and_scan(), the caller of create_document_from_file, runs the
    # scan afterward). A v1 DocumentVersion always starts at pending_review
    # regardless of score (by design — see the comment on `version` above),
    # so should_index() could never become true for that auto-approved
    # document: it looked fully approved yet was never actually indexed, and
    # a low-scoring upload was approved anyway with no scan gate at all. The
    # real approve-or-not decision now happens in upload_and_scan(), AFTER
    # the scan result is known, using the real DocumentStatus it computes —
    # every version starts here as plain draft.
    workflow_state: str | None = None
    if stage.requires_approval:
        db.add(WorkflowState(document_id=document_id, state=WorkflowStatus.draft))
        workflow_state = WorkflowStatus.draft.value

    upload_audit = record_audit(
        db, actor_id=user_id, action="UPLOAD_DOCUMENT", resource_type="document",
        resource_id=document_id,
        details={
            "stage_id": str(stage.stage_id),
            "team_id": str(team_id),
            "version_id": str(version_id),
            "sensitivity": final_sensitivity.name,
            "workflow_state": workflow_state or "none",
        },
    )
    version.provenance_event_id = upload_audit.log_id
    if workflow_state == WorkflowStatus.approved.value:
        record_audit(
            db,
            actor_id=user_id,
            action="APPROVE_DOCUMENT",
            resource_type="document",
            resource_id=document_id,
            details={"state": "approved", "auto_approved": True},
        )
    db.commit()

    return CreatedDocument(
        document_id=document_id,
        version_id=version_id,
        stage_id=stage.stage_id,
        stage_name=stage.name,
        sensitivity_level=final_sensitivity,
        workflow_state=workflow_state,
    )


def create_document(
    db: Session,
    *,
    user_id: uuid.UUID,
    team_id: uuid.UUID,
    project_id: uuid.UUID,
    role: str,
    document_type: str,
    stage_id: uuid.UUID,
    content: str,
    sensitivity: "SensitivityLevel | int | str" = SensitivityLevel.internal,
) -> CreatedDocument:
    """
    Persist a pasted-text document as real Document + DocumentVersion rows.

    Args:
        db: caller-owned session (committed here, not closed here)
        user_id / team_id / project_id: the acting context
        role: the acting role on that team ("viewer" / "contributor" /
            "team_lead" / "org_admin" / "project_admin") — only used to cap
            requested sensitivity via resolve_sensitivity()
        document_type: used as the original_filename base
        stage_id: a real Stage UUID; must belong to project_id and not be
            soft-deleted
        content: document body (stored as UTF-8 bytes, mime text/markdown)
        sensitivity: requested level (enum / int / name); capped by role

    Raises:
        PermissionDeniedError: acting context lacks upload rights
        StageNotFoundError: stage_id missing / in another project / deleted
    """
    stage = _check_upload_access(
        db, user_id=user_id, team_id=team_id, project_id=project_id, stage_id=stage_id
    )
    final_sensitivity = resolve_sensitivity(sensitivity, role)

    return _persist_new_document(
        db,
        user_id=user_id, team_id=team_id, project_id=project_id, stage=stage,
        final_sensitivity=final_sensitivity,
        original_filename=f"{document_type}.md",
        mime_type="text/markdown",
        file_data=content.encode("utf-8"),
    )


def create_document_from_file(
    db: Session,
    *,
    user_id: uuid.UUID,
    team_id: uuid.UUID,
    project_id: uuid.UUID,
    role: str,
    stage_id: uuid.UUID,
    original_filename: str,
    mime_type: str,
    file_data: bytes,
    sensitivity: "SensitivityLevel | int | str" = SensitivityLevel.internal,
) -> CreatedDocumentFromFile:
    """
    Persist a REAL uploaded file (PDF/DOCX/TXT/MD). Stores the raw bytes in
    DocumentVersion.file_data untouched, and separately parses them into
    Markdown (via document_parser.py, Docling for PDF/DOCX) for the returned
    `parsed_content` — what the Scanner and the review chat work on.

    Raises:
        PermissionDeniedError: acting context lacks upload rights
        StageNotFoundError: stage_id missing / in another project / deleted
        UnsupportedDocumentTypeError: mime_type isn't PDF/DOCX/TXT/MD
        DocumentParseError: Docling failed to parse a supported type
    """
    stage = _check_upload_access(
        db, user_id=user_id, team_id=team_id, project_id=project_id, stage_id=stage_id
    )
    final_sensitivity = resolve_sensitivity(sensitivity, role)

    # Parse BEFORE writing anything — a parse failure must not leave a
    # half-created Document/DocumentVersion behind.
    parsed_content = parse_document_to_markdown(file_data, mime_type, original_filename)

    created = _persist_new_document(
        db,
        user_id=user_id, team_id=team_id, project_id=project_id, stage=stage,
        final_sensitivity=final_sensitivity,
        original_filename=original_filename,
        mime_type=mime_type,
        file_data=file_data,
    )

    return CreatedDocumentFromFile(
        document_id=created.document_id,
        version_id=created.version_id,
        stage_id=created.stage_id,
        stage_name=created.stage_name,
        sensitivity_level=created.sensitivity_level,
        workflow_state=created.workflow_state,
        parsed_content=parsed_content,
    )


class DocumentNotFoundError(Exception):
    """document_id doesn't exist, or has no version yet."""


def get_document_view_data(db: Session, document_id: uuid.UUID) -> dict:
    """
    Everything a reviewer needs to actually look at a document before
    approving/rejecting it (Master Plan v2, item 2): the current version's
    content, its latest scan result, and workflow state.

    Callers MUST run their own tenant + can_view_document() ABAC check
    BEFORE calling this — it does no authorization itself, only assembly.
    Raises DocumentNotFoundError if the document has no current version
    (e.g. between document creation and its first successful upload/finalize,
    which in practice shouldn't happen via the real upload flow, but is
    checked rather than assumed).
    """
    document = db.get(Document, document_id)
    if document is None or document.current_version_id is None:
        raise DocumentNotFoundError(f"Document {document_id} has no version to view")

    version = db.get(DocumentVersion, document.current_version_id)
    if version is None:
        raise DocumentNotFoundError(f"Document {document_id}'s current version is missing")

    scan = (
        db.query(DocumentScan)
        .filter(DocumentScan.version_id == version.version_id)
        .order_by(DocumentScan.created_at.desc())
        .first()
    )

    workflow = db.query(WorkflowState).filter(WorkflowState.document_id == document_id).one_or_none()
    stage = db.get(Stage, document.stage_id) if document.stage_id else None
    stage_requires_approval = stage.requires_approval if stage else False

    # Item 10: surface the cached document-coherence assessment here too —
    # the same place the review modal already fetches scan results, per the
    # plan's own instruction not to bury this in the separate Intelligence
    # page. Only issues at/above DIRECT_FACT_CONFIDENCE_FLOOR are shown,
    # matching R009's own gating (a low-confidence issue isn't shown as a
    # concern here either — it's not "fact" anywhere in the app).
    from app.models.graph import DocumentCoherenceCheck
    from app.models.required_document import RequiredDocument
    from app.models.requirement_satisfaction import RequirementSatisfaction
    from app.services.graph.llm_extraction import DIRECT_FACT_CONFIDENCE_FLOOR

    coherence_check = (
        db.query(DocumentCoherenceCheck)
        .filter(
            DocumentCoherenceCheck.document_id == document_id,
            DocumentCoherenceCheck.version_id == version.version_id,
            DocumentCoherenceCheck.status == "completed",
        )
        .order_by(DocumentCoherenceCheck.completed_at.desc())
        .first()
    )
    raw_issues = [
        issue for issue in (coherence_check.issues if coherence_check else [])
        if isinstance(issue, dict) and issue.get("confidence", 0) >= DIRECT_FACT_CONFIDENCE_FLOOR
    ]

    # Same suppression evaluate_r009_document_coherence applies during an
    # audit sweep: a cached "unmet_requirement" issue is a historical fact
    # about THIS document's content, but it should stop being shown as a
    # live concern once a different, currently-selected document has
    # superseded it as that requirement's evidence (see
    # audit_rules.evaluate_r009_document_coherence's docstring for the full
    # reasoning). Without this, a document's own review view could keep
    # showing "doesn't meet the requirement" forever after a better
    # document elsewhere already fixed the gap -- exactly what the audit
    # findings list no longer does, making the two views disagree.
    # Reads the already-persisted RequirementSatisfaction snapshot (last
    # audit run) rather than recomputing evidence resolution live -- same
    # freshness the rest of the app already accepts for this table.
    coherence_issues = []
    reqs_by_name = None
    for issue in raw_issues:
        if issue.get("type") != "unmet_requirement" or document.stage_id is None:
            coherence_issues.append(issue)
            continue
        if reqs_by_name is None:
            reqs_by_name = {
                req.name: req.requirement_id
                for req in db.query(RequiredDocument)
                .filter(RequiredDocument.stage_id == document.stage_id)
                .all()
            }
        related_context = issue.get("related_context", "") or ""
        matched_req_id = next((rid for name, rid in reqs_by_name.items() if name in related_context), None)
        if matched_req_id is not None:
            satisfaction = (
                db.query(RequirementSatisfaction)
                .filter(RequirementSatisfaction.requirement_id == matched_req_id)
                .first()
            )
            if satisfaction is not None and satisfaction.document_id != document_id:
                continue  # superseded -- a different document now serves as this requirement's evidence
        coherence_issues.append(issue)

    content = version.file_data
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")

    uploader_user = db.get(User, version.uploaded_by)
    uploader_info = {
        "user_id": str(uploader_user.user_id) if uploader_user else str(version.uploaded_by),
        "name": uploader_user.full_name or uploader_user.email if uploader_user else "Unknown",
        "email": uploader_user.email if uploader_user else "",
        "timestamp": version.created_at.isoformat(),
    }

    approver_info = None
    approver_id = version.approved_by or (workflow.approved_by if workflow and workflow.state == WorkflowStatus.approved else None)
    if approver_id is not None:
        approver_user = db.get(User, approver_id)
        approved_time = version.approved_at or (workflow.approval_timestamp if workflow else None)
        approver_info = {
            "user_id": str(approver_id),
            "name": approver_user.full_name or approver_user.email if approver_user else "Unknown",
            "email": approver_user.email if approver_user else "",
            "timestamp": approved_time.isoformat() if approved_time else None,
        }

    return {
        "document_id": str(document_id),
        "filename": document.original_filename,
        "version_id": str(version.version_id),
        "version_number": version.version_number,
        "version_status": version.status.value,
        "content_markdown": content,
        "sensitivity_level": document.sensitivity_level.name,
        "workflow_state": workflow.state.value if workflow else None,
        "stage_requires_approval": stage_requires_approval,
        "created_at": version.created_at.isoformat(),
        "uploader": uploader_info,
        "approver": approver_info,
        "provenance_event_id": str(version.provenance_event_id) if version.provenance_event_id else None,
        "scan_overall_score": scan.overall_score if scan else None,
        "scan_criteria": scan.criteria if scan else None,
        # Recomputed from the persisted score/criteria, NOT from
        # version.status == indexed — a version only becomes indexed AFTER
        # approval, so reading status here would make every not-yet-approved
        # document look scan-failed regardless of its actual score, forcing
        # "Override & approve" even when the real quality bar was met.
        "scan_passed": (
            _meets_quality_bar({"overall_score": scan.overall_score, "criteria": scan.criteria})
            and not scan.injection_flagged
        ) if scan else None,
        "injection_flagged": scan.injection_flagged if scan else None,
        "failed_criteria": failed_criteria(scan.criteria) if scan else [],
        "coherence_issues": coherence_issues,
    }
