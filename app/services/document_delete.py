"""
Document/version deletion — SESSION_HANDOFF_2026-09-16.md's confirmed design.

Two distinct operations, deliberately not unified into one endpoint:

  delete_version() — a contributor deleting their OWN unresolved version,
    only if it sits AFTER the document's current LIVE (approved+indexed)
    version. Never touches the live version itself, never renumbers
    anything else. Available to the version's own uploader, or
    project_admin/org_admin (via has_permission's team_lead+ bypass — see
    can_delete_version below for the exact rule).

  delete_document() — wipes the entire Document and every DocumentVersion
    under it. project_admin/org_admin only. Un-indexes from Qdrant first
    (a deleted document must never remain searchable), then removes rows
    from every table that FKs to documents/document_versions WITHOUT
    ondelete=CASCADE (document_scans, document_team_visibility,
    document_stage_references, workflow_state) before deleting the
    versions and the document itself. Claim/ExtractionRun/
    DocumentCoherenceCheck (app/models/graph.py) DO cascade at the DB level
    via ondelete=CASCADE on document_id/version_id. Node/Edge do NOT —
    Node.source_id is a bare UUID with no FK to documents at all (by
    design: a Node can represent any entity type), so a document's own
    knowledge-graph Node (and every Edge touching it, which DOES cascade
    on node_id) is deleted explicitly here instead. audit_log/notifications
    intentionally untouched — both store a bare resource_id with no FK, by
    design, so they survive.

Neither function ever renumbers a surviving version_number — gaps in the
approved sequence (v1, v3 with v2 deleted... except v2 can never be deleted,
since only versions AFTER the live one are deletable, and version_number is
only ever assigned to something that reached approved) are not possible
today, but the code makes no assumption that version_number values are
contiguous.

No "revert to an older version" path exists here — the live version is
simply never deletable through this service. Keep it that way; the revert
feature was tried and rolled back (see project notes) for making the
approval/audit/RAG-grounding interaction too complex for now.
"""

import uuid

from sqlalchemy.orm import Session

from app.models.document import (
    Document,
    DocumentScan,
    DocumentStageReference,
    DocumentTeamVisibility,
    DocumentVersion,
)
from app.models.graph import Node
from app.models.workflow import WorkflowState
from app.services.audit import record_audit
from app.services.indexing import resolve_grounding_version, unindex_document


class DocumentNotFoundError(Exception):
    pass


class VersionNotFoundError(Exception):
    pass


class PermissionDeniedError(Exception):
    pass


class VersionNotDeletableError(Exception):
    """The target version is the live version, or predates/equals it — not deletable."""


def can_delete_version(user_id: uuid.UUID, version: DocumentVersion, *, is_admin: bool) -> bool:
    """
    Per SESSION_HANDOFF_2026-09-16.md: a contributor may delete only their
    OWN version, and only one that sits chronologically after the document's
    current live (approved+indexed) version — the live version and anything
    at or before it is history and off-limits to a regular contributor.
    project_admin/org_admin may delete any such qualifying version
    regardless of who uploaded it (same admin bypass used everywhere else),
    but never the live version itself — there is no revert/replace path,
    only plain deletion of non-live versions.
    """
    return is_admin or version.uploaded_by == user_id


def _version_is_after_live(db: Session, document: Document, version: DocumentVersion) -> bool:
    live = resolve_grounding_version(db, document.document_id)
    if live is None:
        return True  # nothing is live yet — every version is fair game
    if version.version_id == live.version_id:
        return False
    return version.created_at > live.created_at


def delete_version(
    db: Session,
    *,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    actor_id: uuid.UUID,
    is_admin: bool,
) -> None:
    """
    Deletes exactly one DocumentVersion row — never the document, never any
    other version. Raises DocumentNotFoundError / VersionNotFoundError /
    PermissionDeniedError / VersionNotDeletableError; caller maps these to
    HTTP status codes.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise DocumentNotFoundError(f"Document {document_id} not found")

    version = db.get(DocumentVersion, version_id)
    if version is None or version.document_id != document_id:
        raise VersionNotFoundError(f"Version {version_id} not found on document {document_id}")

    if not can_delete_version(actor_id, version, is_admin=is_admin):
        raise PermissionDeniedError("You can only delete your own draft/rejected versions.")

    if not _version_is_after_live(db, document, version):
        raise VersionNotDeletableError(
            "Only versions uploaded after the current live version can be deleted this way."
        )

    if document.current_version_id == version.version_id:
        # The deleted row was the newest — document.current_version_id must
        # point somewhere else afterward. Fall back to the next-newest
        # surviving version (or None if this was the only one).
        next_latest = (
            db.query(DocumentVersion)
            .filter(DocumentVersion.document_id == document_id, DocumentVersion.version_id != version_id)
            .order_by(DocumentVersion.created_at.desc())
            .first()
        )
        document.current_version_id = next_latest.version_id if next_latest else None

    db.query(DocumentScan).filter(DocumentScan.version_id == version_id).delete()

    record_audit(
        db, actor_id=actor_id, action="DELETE_VERSION", resource_type="document_version",
        resource_id=version_id, details={"document_id": str(document_id)},
    )
    db.delete(version)
    db.commit()


def delete_document(db: Session, *, document_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    """
    Wipes the entire document — every version, and every row in a
    non-cascading table that FKs to it. Admin-only; the router enforces that
    before calling this. Un-indexes from Qdrant first.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise DocumentNotFoundError(f"Document {document_id} not found")

    unindex_document(db, document_id)

    db.query(DocumentScan).filter(
        DocumentScan.version_id.in_(
            db.query(DocumentVersion.version_id).filter(DocumentVersion.document_id == document_id)
        )
    ).delete(synchronize_session=False)
    db.query(WorkflowState).filter(WorkflowState.document_id == document_id).delete()
    db.query(DocumentTeamVisibility).filter(DocumentTeamVisibility.document_id == document_id).delete()
    db.query(DocumentStageReference).filter(DocumentStageReference.document_id == document_id).delete()

    # Node.source_id has no FK to documents (a Node can represent any entity
    # type), so this document's knowledge-graph Node — and every Edge
    # touching it, which DOES cascade on node_id — must be deleted
    # explicitly. Without this, audit rules that resolve a Node back to a
    # Document (R001/R003/R006) silently skip the dangling Node forever
    # (harmless but permanent graph-table bloat with no cleanup path
    # otherwise) — see app/services/graph/sync.py's own stale-node cleanup
    # for the same pattern applied to required_documents/teams/users nodes.
    db.query(Node).filter(
        Node.tenant_id == document.tenant_id,
        Node.project_id == document.project_id,
        Node.source_table == "documents",
        Node.source_id == document_id,
    ).delete()

    # Break the documents.current_version_id -> document_versions FK before
    # deleting the versions themselves.
    document.current_version_id = None
    db.flush()

    db.query(DocumentVersion).filter(DocumentVersion.document_id == document_id).delete()

    record_audit(
        db, actor_id=actor_id, action="DELETE_DOCUMENT", resource_type="document",
        resource_id=document_id, details={"filename": document.original_filename},
    )
    db.delete(document)
    db.commit()
