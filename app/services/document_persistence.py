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
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.document import (
    Document,
    DocumentVersion,
    DocumentTeamVisibility,
    DocumentStatus,
    SensitivityLevel,
)
from app.models.stage import Stage
from app.models.user import User
from app.models.workflow import WorkflowState, WorkflowStatus
from app.services.access_control import has_permission, has_stage_access, resolve_sensitivity
from app.services.audit import record_audit
from app.services.document_parser import parse_document_to_markdown


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
    """Shared gate for both creation paths: has_permission + has_stage_access + a real, active stage."""
    if not has_permission(db, user_id, "upload", team_id, project_id):
        raise PermissionDeniedError(
            "You do not have permission to upload documents as this team."
        )

    stage = db.get(Stage, stage_id)
    if stage is None or stage.project_id != project_id or stage.deleted_at is not None:
        raise StageNotFoundError(
            f"Stage {stage_id} does not exist in this project (or has been deleted)."
        )

    # THIRD check (Phase A Part 3), alongside the role/team-project check
    # above: does the acting team have a team_stage_access grant for this
    # stage? org_admin/project_admin bypass entirely (has_stage_access()
    # applies the same bypass has_permission() does above).
    if not has_stage_access(db, user_id, team_id, stage_id, project_id):
        raise PermissionDeniedError(
            f"Team does not have access to upload to the '{stage.name}' stage."
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
        version_number=1,
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
    # If the uploader already holds the 'approve' permission (e.g. team_lead or admin),
    # their document is auto-approved upon upload.
    workflow_state: str | None = None
    if stage.requires_approval:
        if has_permission(db, user_id, "approve", team_id, project_id):
            now = datetime.now(timezone.utc)
            db.add(
                WorkflowState(
                    document_id=document_id,
                    state=WorkflowStatus.approved,
                    approved_by=user_id,
                    approval_timestamp=now,
                )
            )
            workflow_state = WorkflowStatus.approved.value
        else:
            db.add(WorkflowState(document_id=document_id, state=WorkflowStatus.draft))
            workflow_state = WorkflowStatus.draft.value

    record_audit(
        db, actor_id=user_id, action="UPLOAD_DOCUMENT", resource_type="document",
        resource_id=document_id,
        details={
            "stage_id": str(stage.stage_id),
            "team_id": str(team_id),
            "sensitivity": final_sensitivity.name,
            "workflow_state": workflow_state or "none",
        },
    )
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
