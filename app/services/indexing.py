"""
Indexing trigger — combines the two INDEPENDENT status systems that gate
whether a document's current version is ready for RAG indexing:

  - DocumentVersion.status (Scanner-driven: pending_review / needs_attention /
    indexed) — set by document_finalize.py's finalize_document_revision() on
    EVERY finalize, for EVERY stage, regardless of that stage's approval
    policy. `indexed` here means "the Scanner is satisfied" — nothing more.

  - WorkflowState.state (Phase 5, human approval) — exists at all ONLY if the
    document's stage has requires_approval=True. `approved` here means "a
    human signed off" — nothing about content quality.

should_index() is the single place these two axes are combined into an
actual "ready to index" verdict. Call it after any event that could flip
either axis: document_finalize.py's finalize step, and the end of
approve_document() in app/services/workflow.py.

index_document() chunks the document's current version, embeds each chunk
(dense + sparse), and upserts into its tenant's Qdrant collection
(app/services/rag/collection_setup.py) — replacing older points for the
same document_id first, so a re-indexed document is never simultaneously
searchable under two versions.
"""

import logging
import uuid

from qdrant_client import models as qm
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentStatus, DocumentVersion, VersionApprovalOutcome
from app.models.project import Project
from app.models.stage import Stage
from app.models.workflow import WorkflowState, WorkflowStatus
from app.services.rag.chunking import chunk_document
from app.services.rag.collection_setup import (
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    ensure_tenant_collection,
    get_qdrant_client,
)
from app.services.rag.embedding import embed_dense, embed_sparse

logger = logging.getLogger(__name__)


class DocumentNotIndexableError(Exception):
    pass


def should_index(db: Session, document_id: uuid.UUID) -> bool:
    """
    True iff `document_id`'s CURRENT version is ready for RAG indexing:
      1. DocumentVersion.status == indexed — ALWAYS required.
      2. IF the document's stage has requires_approval=True, ALSO require
         WorkflowState.state == approved.

    A version with status pending_review or needs_attention never satisfies
    this (needs_attention is used for both a failed structural scan and a
    flagged injection scan — neither is ever "indexed").
    """
    document = db.get(Document, document_id)
    if document is None or document.current_version_id is None:
        return False

    version = db.get(DocumentVersion, document.current_version_id)
    if version is None or version.status != DocumentStatus.indexed:
        return False

    stage = db.get(Stage, document.stage_id)
    if stage is not None and stage.requires_approval:
        workflow = (
            db.query(WorkflowState)
            .filter(WorkflowState.document_id == document_id)
            .one_or_none()
        )
        if workflow is None or workflow.state != WorkflowStatus.approved:
            return False

    return True


def resolve_grounding_version(db: Session, document_id: uuid.UUID) -> DocumentVersion | None:
    """
    The version whose content is safe to hand an LLM as "this document's real
    content" — the same version that would currently be sitting in Qdrant, if
    the document has ever been indexed. NOT necessarily
    document.current_version_id: a document's current version can be a newer
    revision that failed the Scanner (needs_attention) or is still awaiting
    human approval, sitting on top of an older version that DID pass and is
    still the one actually indexed. Reviewers/uploaders are meant to see that
    newer, not-yet-good version (document_version_review.py's edit flow does,
    correctly) — but nothing that grounds an answer/summary should.

    Walks this document's versions newest-first and returns the first one
    that would satisfy should_index() were it the current version (Scanner-
    indexed AND, if the stage requires it, human-approved) — this is
    intentionally the same two-axis check should_index() applies to the
    actual current version, just evaluated version-by-version instead of
    only against document.current_version_id. Returns None if no version of
    this document has ever cleared that bar (nothing has ever been
    genuinely indexed for it).
    """
    document = db.get(Document, document_id)
    if document is None:
        return None

    stage = db.get(Stage, document.stage_id)
    requires_approval = stage is not None and stage.requires_approval
    if requires_approval:
        workflow = (
            db.query(WorkflowState)
            .filter(WorkflowState.document_id == document_id)
            .one_or_none()
        )
        if workflow is None or workflow.state != WorkflowStatus.approved:
            # Approval is per-document, not per-version — if the document
            # isn't currently approved, no version of it can be grounded in,
            # even an old one that once passed the scan (reset_to_draft_if_approved
            # already un-indexes Qdrant in exactly this situation).
            return None

    # Ordered by created_at, not version_number: version_number is now only
    # assigned on approval (None until then), so it no longer reflects
    # chronological order — and NULLs would sort ahead of real numbers in
    # a naive DESC order, walking unresolved versions before ever reaching
    # an approved one.
    versions = (
        db.query(DocumentVersion)
        .filter(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.created_at.desc())
        .all()
    )
    for version in versions:
        if version.status == DocumentStatus.indexed and version.approval_outcome == VersionApprovalOutcome.approved:
            return version
    return None


def _delete_existing_points(client, collection: str, document_id: uuid.UUID) -> None:
    """
    Remove every existing point for this document_id, unconditionally — runs
    even on first index (a harmless no-op then), so a re-indexed document is
    never simultaneously searchable under two versions. document_id is payload-
    indexed (see collection_setup.py) to support efficient document-level
    filtering/deletion operations and satisfy Qdrant payload-index requirements.
    """
    client.delete(
        collection_name=collection,
        points_selector=qm.FilterSelector(
            filter=qm.Filter(
                must=[qm.FieldCondition(key="document_id", match=qm.MatchValue(value=str(document_id)))]
            )
        ),
    )


def unindex_document(db: Session, document_id: uuid.UUID) -> None:
    """
    Removes this document's points from its tenant's Qdrant collection, if
    any exist — the missing counterpart to index_document(). Call this
    whenever a document that may have been indexed becomes NOT should_index()
    again: specifically, when a prior approval is reset (a new version
    finalized on a previously-approved document — see
    workflow.reset_to_draft_if_approved()). Without this, a document's OLD,
    now-superseded and no-longer-approved content stays fully searchable via
    RAG, silently, with nothing in the search result to indicate it's stale.

    A no-op if the document was never indexed (Qdrant delete-by-filter on a
    document_id with no points is harmless), so this is safe to call
    unconditionally rather than trying to first determine whether an index
    entry actually exists.
    """
    document = db.get(Document, document_id)
    if document is None:
        return
    client = get_qdrant_client()
    collection = ensure_tenant_collection(client, document.tenant_id)
    _delete_existing_points(client, collection, document_id)
    logger.info("unindex_document(%s): removed any existing points from %s", document_id, collection)


def index_document(db: Session, document_id: uuid.UUID) -> dict:
    """
    Chunks the document's CURRENT version content (already-parsed Markdown —
    no re-parsing; see finalize_document_revision/create_document_from_file,
    the only two places DocumentVersion.file_data is ever written), embeds
    each chunk (dense + sparse), and upserts into the tenant's Qdrant
    collection. Deletes any of this document's existing points first.

    Only ever called after should_index() has confirmed the version is
    ready (DocumentVersion.status == indexed, and approved if the stage
    requires it) — this function does not re-check that itself.

    Returns:
        {"collection": str, "chunks_indexed": int, "chunks": [chunk dicts]}

    Raises:
        DocumentNotIndexableError: no current version, or its content isn't
            valid UTF-8 text (shouldn't happen for an `indexed` version).
    """
    # Document indexing serialization:
    # Acquire a row lock on the Document row so concurrent indexing attempts
    # on the same document are serialized within the database transaction.
    # This prevents concurrent delete-then-upsert operations from interleaving
    # and creating mixed-version points in Qdrant.
    document = (
        db.query(Document)
        .filter(Document.document_id == document_id)
        .with_for_update()
        .one_or_none()
    )
    if document is None or document.current_version_id is None:
        raise DocumentNotIndexableError(f"Document {document_id} has no current version")

    version = db.get(DocumentVersion, document.current_version_id)
    if version is None:
        raise DocumentNotIndexableError(f"Document {document_id}'s current version is missing")
    try:
        content = version.file_data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentNotIndexableError(
            f"Document {document_id}'s current version is not valid UTF-8 text"
        ) from exc

    project = db.get(Project, document.project_id)
    stage = db.get(Stage, document.stage_id)

    chunks = chunk_document(content)
    if not chunks:
        logger.info("index_document(%s): nothing to index (empty content)", document_id)
        chunks = []

    # Contextual header — baked into the EMBEDDED text only. The stored
    # chunk_text payload field stays the clean original (see chunk_document's
    # own docstring) so it displays cleanly wherever it's read back later.
    embed_texts = [
        f"Project: {project.name if project else ''} | "
        f"Stage: {stage.name if stage else ''} | "
        f"Section: {chunk['section_title']}\n\n{chunk['chunk_text']}"
        for chunk in chunks
    ]

    dense_vectors = embed_dense(embed_texts)
    sparse_vectors = embed_sparse(embed_texts)

    client = get_qdrant_client()
    collection = ensure_tenant_collection(client, document.tenant_id)

    _delete_existing_points(client, collection, document_id)

    if chunks:
        points = [
            qm.PointStruct(
                id=str(uuid.uuid4()),
                vector={
                    DENSE_VECTOR_NAME: dense_vectors[i],
                    SPARSE_VECTOR_NAME: sparse_vectors[i],
                },
                payload={
                    "document_id": str(document_id),
                    "version_id": str(version.version_id),
                    "version_number": version.version_number,
                    "project_id": str(document.project_id),
                    "tenant_id": str(document.tenant_id),
                    "stage_id": str(document.stage_id),
                    "section_title": chunk["section_title"],
                    "chunk_text": chunk["chunk_text"],
                },
            )
            for i, chunk in enumerate(chunks)
        ]
        client.upsert(collection_name=collection, points=points)

    db.commit()
    logger.info(
        "index_document(%s): indexed %d chunk(s) into %s", document_id, len(chunks), collection
    )

    return {"collection": collection, "chunks_indexed": len(chunks), "chunks": chunks}
