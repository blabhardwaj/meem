"""
Document-level coherence check (Master Plan v2, item 10) — the feature this
project was actually missing: something that checks a finalized document's
CONTENT against the rest of the project, not just its own structural
quality (the Scanner) or project-wide deterministic rule violations (the
audit engine's R001-R010). A document can pass the Scanner while flatly
contradicting or duplicating existing project content; nothing caught that
before this.

run_document_coherence_check() is called once per finalize (from
audit_engine.extract_sync_and_audit_document, after extraction/sync, before
the audit sweep) and caches its result in knowledge.document_coherence_checks
— see that model's docstring for why this must be cached rather than
re-run on every audit. evaluate_r009_document_coherence() (audit_rules.py)
reads the cache on every sweep; nothing here calls Groq during a sweep.
"""
import hashlib
import logging
import uuid
from datetime import datetime, timezone

from qdrant_client import models as qm
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentVersion
from app.models.graph import DocumentCoherenceCheck
from app.models.required_document import RequiredDocument
from app.services.graph.llm_extraction import assess_document_coherence
from app.services.rag.collection_setup import DENSE_VECTOR_NAME, collection_name_for_tenant, get_qdrant_client
from app.services.rag.embedding import embed_dense

logger = logging.getLogger(__name__)

CHECKER_VERSION = "1.0.0"

# How many related-document chunks and requirements to hand the LLM as
# context — bounded for cost/latency, same principle as the reference
# extraction's candidate lists in llm_extraction.py.
MAX_RELATED_CHUNKS = 6
CHUNK_SIMILARITY_THRESHOLD = 0.55  # deliberately lower than item 7's evidence
# threshold (0.72) — this is "plausibly related enough to check", not "proves
# a requirement is satisfied". A false-positive context snippet just gives
# the LLM one more (ignorable) thing to look at; a missed one means a real
# contradiction/duplicate goes unchecked, which is the worse failure mode here.


def _compute_content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _gather_related_context(db: Session, document: Document, content: str) -> list[dict]:
    """
    Related context = related document chunks (Qdrant similarity search
    using this document's own content as the query — the same "does
    anything already say something about this" search item 7 uses for
    requirement evidence, here used for the new document itself) +
    mandatory requirements for this document's stage (no graph edge needed —
    matches item 7's point that a document can satisfy a requirement without
    any edge existing yet).
    """
    context: list[dict] = []

    client = get_qdrant_client()
    collection = collection_name_for_tenant(document.tenant_id)
    if client.collection_exists(collection):
        try:
            query_vector = embed_dense([content[:2000]])[0]
            response = client.query_points(
                collection_name=collection,
                query=query_vector,
                using=DENSE_VECTOR_NAME,
                query_filter=qm.Filter(
                    must=[
                        qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=str(document.tenant_id))),
                        qm.FieldCondition(key="project_id", match=qm.MatchValue(value=str(document.project_id))),
                    ],
                    must_not=[
                        qm.FieldCondition(key="document_id", match=qm.MatchValue(value=str(document.document_id))),
                    ],
                ),
                limit=MAX_RELATED_CHUNKS,
                score_threshold=CHUNK_SIMILARITY_THRESHOLD,
                with_payload=True,
            )
            for point in response.points:
                payload = point.payload or {}
                section = payload.get("section_title", "")
                chunk_text = payload.get("chunk_text", "")
                doc_id_str = payload.get("document_id")
                if not doc_id_str:
                    continue
                try:
                    rel_doc = db.get(Document, uuid.UUID(str(doc_id_str)))
                except Exception:
                    rel_doc = None
                if rel_doc is None:
                    # Qdrant and Postgres can drift: a point can outlive the
                    # document row it was indexed for (e.g. a document
                    # deleted or replaced outside index_document()'s own
                    # delete-then-upsert, or a leftover from earlier seed
                    # iterations). Silently falling back to a generic label
                    # here previously fed a real R009 false positive: a
                    # long-deleted document's stale content ("Not ready for
                    # go-live") kept getting handed to the LLM as if it were
                    # live project content, indefinitely contradicting the
                    # actual current document that superseded it. Skip
                    # rather than guess.
                    continue
                doc_filename = rel_doc.original_filename

                if chunk_text:
                    label = f"Document: {doc_filename} ({section})" if section else f"Document: {doc_filename}"
                    context.append({
                        "document_id": doc_id_str,
                        "filename": doc_filename,
                        "label": label,
                        "text": chunk_text,
                    })
        except Exception:
            logger.exception(
                "Coherence check: Qdrant related-chunk search failed for document_id=%s",
                document.document_id,
            )

    if document.stage_id:
        reqs = (
            db.query(RequiredDocument)
            .filter(RequiredDocument.stage_id == document.stage_id, RequiredDocument.is_mandatory.is_(True))
            .all()
        )
        for req in reqs:
            context.append({
                "label": f"Requirement: {req.name}",
                "text": req.description or req.name,
            })

    return context


def run_document_coherence_check(
    db: Session,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    content: str,
) -> DocumentCoherenceCheck:
    """
    Idempotent via (version_id, content_hash, checker_version) — matches
    ExtractionRun's pattern. Returns the cached row unchanged if this exact
    version's content was already checked; only calls the LLM otherwise.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise ValueError(f"Document {document_id} not found")

    content_hash = _compute_content_hash(content or "")

    existing = (
        db.query(DocumentCoherenceCheck)
        .filter(
            DocumentCoherenceCheck.version_id == version_id,
            DocumentCoherenceCheck.content_hash == content_hash,
            DocumentCoherenceCheck.checker_version == CHECKER_VERSION,
            DocumentCoherenceCheck.status == "completed",
        )
        .first()
    )
    if existing:
        return existing

    check = DocumentCoherenceCheck(
        tenant_id=document.tenant_id,
        project_id=document.project_id,
        document_id=document_id,
        version_id=version_id,
        content_hash=content_hash,
        checker_version=CHECKER_VERSION,
        status="pending",
    )
    db.add(check)
    db.flush()

    try:
        related_context = _gather_related_context(db, document, content or "")
        issues = assess_document_coherence(content or "", related_context) if related_context else []

        # Correlate evidence document info onto issues
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            ctx_ref = str(issue.get("related_context") or "").lower()
            desc_ref = str(issue.get("description") or "").lower()
            for c in related_context:
                c_doc_id = c.get("document_id")
                c_label = str(c.get("label") or "").lower()
                c_fname = str(c.get("filename") or "").lower()
                c_text = str(c.get("text") or "").lower()
                if not c_doc_id:
                    continue
                # Match if label or filename appears in related_context or description, or if snippet is quoted
                if (c_label and c_label in ctx_ref) or (c_fname and (c_fname in ctx_ref or c_fname in desc_ref)) or (len(ctx_ref) > 20 and ctx_ref in c_text):
                    issue["evidence_document_id"] = str(c_doc_id)
                    issue["evidence_filename"] = c.get("filename")
                    break

        check.issues = issues
        check.status = "completed"
        check.completed_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:
        db.rollback()
        check.status = "failed"
        check.error_message = str(exc)
        check.completed_at = datetime.now(timezone.utc)
        db.commit()
        logger.exception("Document coherence check failed for document_id=%s", document_id)

    return check
