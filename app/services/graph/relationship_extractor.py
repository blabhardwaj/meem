import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy.orm import Session

from app.config import GROQ_MODEL
from app.models.document import Document, DocumentVersion
from app.models.graph import Edge, ExtractionRun, Node
from app.models.project import Project
from app.models.required_document import RequiredDocument
from app.models.stage import Stage
from app.services.graph.llm_extraction import (
    DIRECT_FACT_CONFIDENCE_FLOOR,
    REFERENCE_RELATIONSHIP_TYPES,
    extract_references_llm,
)
from app.services.graph.sync import _upsert_edge, _upsert_node

logger = logging.getLogger(__name__)

# Bumped from 1.0.0: the LLM-augmented pass changes what a "completed"
# extraction run actually covers, so a cached 1.0.0 run must not be treated
# as equivalent to a 2.0.0 one — see extract_document_relationships's
# idempotency check.
#
# Bumped again, 2.0.0 -> 2.1.0: the requirement-evidence title match (step 4
# below) only matched a requirement's title against a filename/content using
# a literal-space word boundary (`\bBusiness Case\b`), which never matches a
# real filename like "01-instant-payouts-business-case.md" (hyphen-
# separated, not space-separated) — a correctly-named, correctly-present
# document was silently never linked to its requirement, showing as
# "missing" in every downstream reader (R001, RequirementSatisfaction, the
# requirements checklist). Fixed to accept any run of whitespace/hyphen/
# underscore/dot between the title's words on either side of the match. A
# cached 2.0.0 run reflects the OLD, buggy matching and must not be treated
# as equivalent — this bump is what makes existing documents get
# re-extracted with the fix instead of silently keeping their stale (wrong)
# edges forever.
#
# Bumped again, 2.1.0 -> 2.2.0: this function never deleted an edge it had
# previously created — every pass only ever added or upserted-overwrote an
# edge when a NEW match fired. A document revised to remove the text that
# originally triggered an EVIDENCES/REFERENCES/etc. edge (e.g. a master
# lending agreement's boilerplate that happened to name a specific
# disclosure document by title) kept that edge FOREVER, wrongly satisfying
# a requirement the new content no longer actually addresses. Fixed by
# deleting every edge this extractor owns (REFERENCE_RELATIONSHIP_TYPES,
# imported from llm_extraction.py so the two can never drift apart) sourced
# from this document, before re-extracting — mirrors claims_analyzer.py's
# persist_claims()/persist_llm_claims() delete-then-insert pattern, just
# scoped to the document's graph node (edges aren't version-tagged) instead
# of a version_id. A cached 2.1.0 run predates this fix and may still be
# carrying a stale edge from an earlier version's content — this bump
# forces re-extraction so the cleanup actually runs once per document.
EXTRACTOR_VERSION = "2.2.0"

ALLOWED_EDGE_TYPES: Set[str] = {
    "PRECEDES",
    "ALLOWED_REFERENCE",
    "CONTAINS_STAGE",
    "OWNED_BY",
    "ASSIGNED_TEAM",
    "MANAGES_PROJECT",
    "REQUIRES",
    "ORIGINATED_IN",
    "APPLIES_TO",
    "ESTABLISHES",
    "IMPLEMENTS",
    "VALIDATES",
    "EVIDENCES",
    "REFERENCES",
    "DEPENDS_ON",
    "BLOCKS",
    "CONFLICTS_WITH",
}


def compute_content_hash(text: str) -> str:
    """Computes SHA-256 hash of document text content."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ExtractionResult:
    def __init__(self, run_id: uuid.UUID, is_cached: bool = False):
        self.run_id = run_id
        self.is_cached = is_cached
        self.edges_created: int = 0
        self.claims_created: int = 0
        self.errors: List[str] = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "is_cached": self.is_cached,
            "edges_created": self.edges_created,
            "claims_created": self.claims_created,
            "errors": self.errors,
        }


def extract_document_relationships(
    db: Session,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    content: str,
    extractor_version: str = EXTRACTOR_VERSION,
) -> ExtractionResult:
    """
    Extracts semantic document relationships (REFERENCES, DEPENDS_ON, EVIDENCES,
    ESTABLISHES, IMPLEMENTS, VALIDATES) from document version content.
    Fully idempotent via (version_id, content_hash, extractor_version).
    """
    doc = db.query(Document).filter(Document.document_id == document_id).first()
    if not doc:
        raise ValueError(f"Document {document_id} not found")

    content_hash = compute_content_hash(content or "")

    # 1. Idempotency Check
    existing_run = (
        db.query(ExtractionRun)
        .filter(
            ExtractionRun.version_id == version_id,
            ExtractionRun.content_hash == content_hash,
            ExtractionRun.extractor_version == extractor_version,
            ExtractionRun.status == "completed",
        )
        .first()
    )
    if existing_run:
        result = ExtractionResult(existing_run.run_id, is_cached=True)
        result.edges_created = existing_run.extracted_edges_count
        result.claims_created = existing_run.extracted_claims_count
        return result

    # 2. Initialize or reuse pending ExtractionRun
    run = (
        db.query(ExtractionRun)
        .filter(
            ExtractionRun.version_id == version_id,
            ExtractionRun.content_hash == content_hash,
            ExtractionRun.extractor_version == extractor_version,
        )
        .first()
    )
    if not run:
        run = ExtractionRun(
            tenant_id=doc.tenant_id,
            project_id=doc.project_id,
            document_id=doc.document_id,
            version_id=version_id,
            content_hash=content_hash,
            extractor_version=extractor_version,
            status="pending",
        )
        db.add(run)
        db.flush()

    result = ExtractionResult(run.run_id, is_cached=False)

    try:
        # Ensure Document Node exists
        doc_label = getattr(doc, "original_filename", None) or "Untitled Document"
        doc_node = _upsert_node(
            db=db,
            tenant_id=doc.tenant_id,
            project_id=doc.project_id,
            entity_type="document",
            source_table="documents",
            source_id=doc.document_id,
            label=doc_label,
        )

        # Delete every edge this extractor owns, sourced from this document,
        # before re-extracting — see the EXTRACTOR_VERSION 2.2.0 note above.
        # Edges aren't version-tagged (the source node is the DOCUMENT, not
        # a specific version), so this must be scoped by node + owned edge
        # types, not by version_id the way persist_claims() scopes by it.
        db.query(Edge).filter(
            Edge.source_node_id == doc_node.node_id,
            Edge.edge_type.in_(REFERENCE_RELATIONSHIP_TYPES),
        ).delete(synchronize_session=False)

        provenance_metadata = {
            "run_id": str(run.run_id),
            "content_hash": content_hash,
            "extractor_version": extractor_version,
        }

        # 3. Deterministic Extraction of Document References (REFERENCES)
        all_other_docs = (
            db.query(Document)
            .filter(
                Document.project_id == doc.project_id,
                Document.document_id != doc.document_id,
            )
            .all()
        )

        for other_doc in all_other_docs:
            other_name = getattr(other_doc, "original_filename", "") or ""
            # Strip common extensions for flexible reference matching (.md, .pdf, .docx)
            base_name = re.sub(r"\.[a-zA-Z0-9]+$", "", other_name).strip()
            if not base_name or len(base_name) < 3:
                continue

            name_escaped = re.escape(base_name)
            pattern = rf"\b(?:see\s+|refer\s+to\s+|per\s+)?{name_escaped}\b"
            match = re.search(pattern, content, flags=re.IGNORECASE)
            
            uuid_pattern = rf"\b{str(other_doc.document_id)}\b"
            uuid_match = re.search(uuid_pattern, content, flags=re.IGNORECASE)

            if match or uuid_match:
                other_node = _upsert_node(
                    db=db,
                    tenant_id=other_doc.tenant_id,
                    project_id=other_doc.project_id,
                    entity_type="document",
                    source_table="documents",
                    source_id=other_doc.document_id,
                    label=other_name or "Untitled Document",
                )
                
                snippet = match.group(0) if match else str(other_doc.document_id)
                dep_pattern = rf"(?:depends\s+on|requires|prerequisite(?:\s+is)?)\s+[^.\n]*\b{name_escaped}\b"
                is_dependency = bool(re.search(dep_pattern, content, flags=re.IGNORECASE))
                edge_type = "DEPENDS_ON" if is_dependency else "REFERENCES"

                edge_props = {
                    "matched_text": snippet,
                    "is_dependency": is_dependency,
                    "source": "regex",
                }

                _upsert_edge(
                    db=db,
                    tenant_id=doc.tenant_id,
                    project_id=doc.project_id,
                    source_node_id=doc_node.node_id,
                    target_node_id=other_node.node_id,
                    edge_type=edge_type,
                    properties=edge_props,
                    confidence=0.95 if is_dependency else 0.90,
                    provenance=provenance_metadata,
                )
                result.edges_created += 1

        # 4. Deterministic Requirement Evidence Matching (EVIDENCES / SATISFIES)
        if doc.stage_id:
            reqs = (
                db.query(RequiredDocument)
                .filter(RequiredDocument.stage_id == doc.stage_id)
                .all()
            )
            for req in reqs:
                req_title = req.name.strip()
                req_escaped = re.escape(req_title)
                # Filename-vs-title word-separator mismatch: a real filename
                # like "01-instant-payouts-business-case.md" uses hyphens
                # where the requirement's title ("Business Case") uses a
                # literal space — `\bBusiness Case\b` never matches
                # "business-case" as written, so a correctly-named,
                # correctly-present document was silently never linked to
                # its requirement. Build a pattern that accepts any run of
                # non-alphanumeric separators (space, hyphen, underscore,
                # dot) between the title's words, matching either form.
                req_words = [re.escape(w) for w in req_title.split()]
                title_pattern = r"[\s\-_.]+".join(req_words) if req_words else req_escaped
                # `\b` treats "_" as a word character, so it never fires
                # between "concept" and the next word in a filename like
                # "03_product_concept_and_differentiation.md" — the whole
                # match is glued to trailing text and silently never counts
                # as a title match. Use lookarounds keyed on the SAME
                # separator set the pattern above already accepts BETWEEN
                # words, so a title match is only rejected when it's
                # actually fused into a longer alphanumeric run (e.g.
                # "concept" inside "conceptual"), not merely followed by
                # another underscore/hyphen-joined word or a file extension.
                boundary = r"(?:^|[\s\-_.]|$)"
                doc_name_match = bool(re.search(
                    rf"{boundary}{title_pattern}{boundary}", doc_label, re.IGNORECASE
                ))
                content_match = bool(re.search(
                    rf"(?:satisfies|implements|fulfills|validates|establishes)\s+[^.\n]*"
                    rf"{boundary}{title_pattern}{boundary}",
                    content, re.IGNORECASE,
                ))
                if doc_name_match or content_match:
                    req_node = _upsert_node(
                        db=db,
                        tenant_id=doc.tenant_id,
                        project_id=doc.project_id,
                        entity_type="requirement",
                        source_table="required_documents",
                        source_id=req.requirement_id,
                        label=req.name,
                    )

                    # Determine semantic evidentiary role
                    edge_type = "EVIDENCES"
                    if "validat" in content.lower():
                        edge_type = "VALIDATES"
                    elif "implement" in content.lower():
                        edge_type = "IMPLEMENTS"
                    elif "establish" in content.lower() or doc_name_match:
                        edge_type = "ESTABLISHES"

                    _upsert_edge(
                        db=db,
                        tenant_id=doc.tenant_id,
                        project_id=doc.project_id,
                        source_node_id=doc_node.node_id,
                        target_node_id=req_node.node_id,
                        edge_type=edge_type,
                        properties={"matched_requirement": req.name, "rule": "title_or_content_evidence", "source": "regex"},
                        confidence=0.95 if doc_name_match else 0.85,
                        provenance=provenance_metadata,
                    )
                    result.edges_created += 1

        # 4.5. LLM-augmented reference extraction (item 9) — additive on top
        # of the regex passes above. Catches a document that references
        # another document/requirement by DESCRIPTION rather than exact
        # name/filename, which regex structurally cannot.
        all_reqs = (
            db.query(RequiredDocument).filter(RequiredDocument.stage_id == doc.stage_id).all()
            if doc.stage_id else []
        )
        candidate_documents = [
            {"id": str(d.document_id), "filename": d.original_filename} for d in all_other_docs
        ]
        candidate_requirements = [
            {"id": str(r.requirement_id), "name": r.name, "description": r.description}
            for r in all_reqs
        ]
        doc_by_id = {str(d.document_id): d for d in all_other_docs}
        req_by_id = {str(r.requirement_id): r for r in all_reqs}

        llm_results = extract_references_llm(content, candidate_documents, candidate_requirements, doc_label)
        for item in llm_results:
            if item["target_type"] == "document":
                target = doc_by_id.get(item["target_id"])
                if not target:
                    continue
                target_node = _upsert_node(
                    db=db, tenant_id=target.tenant_id, project_id=target.project_id,
                    entity_type="document", source_table="documents",
                    source_id=target.document_id, label=target.original_filename or "Untitled Document",
                )
            else:
                target = req_by_id.get(item["target_id"])
                if not target:
                    continue
                target_node = _upsert_node(
                    db=db, tenant_id=doc.tenant_id, project_id=doc.project_id,
                    entity_type="requirement", source_table="required_documents",
                    source_id=target.requirement_id, label=target.name,
                )

            # Same (source_node_id, target_node_id, edge_type) key as the
            # regex passes above — never let a lower-confidence LLM find
            # silently downgrade a higher-confidence regex-found edge.
            # _upsert_edge() itself always overwrites, so check first.
            existing = (
                db.query(Edge)
                .filter(
                    Edge.source_node_id == doc_node.node_id,
                    Edge.target_node_id == target_node.node_id,
                    Edge.edge_type == item["relationship"],
                )
                .first()
            )
            if existing and existing.confidence is not None and existing.confidence >= item["confidence"]:
                continue

            _upsert_edge(
                db=db,
                tenant_id=doc.tenant_id,
                project_id=doc.project_id,
                source_node_id=doc_node.node_id,
                target_node_id=target_node.node_id,
                edge_type=item["relationship"],
                properties={
                    "source": "llm",
                    "reason": item["reason"],
                    "low_confidence": item["confidence"] < DIRECT_FACT_CONFIDENCE_FLOOR,
                },
                confidence=item["confidence"],
                provenance={**provenance_metadata, "extractor": "llm", "model": GROQ_MODEL},
            )
            result.edges_created += 1

        # 5. Finalize ExtractionRun
        run.status = "completed"
        run.extracted_edges_count = result.edges_created
        run.extracted_claims_count = result.claims_created
        run.completed_at = datetime.now(timezone.utc)
        db.commit()

    except Exception as exc:
        db.rollback()
        run.status = "failed"
        run.error_message = str(exc)
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        result.errors.append(str(exc))

    return result
