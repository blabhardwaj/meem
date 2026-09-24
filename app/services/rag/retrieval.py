"""
Phase C retrieval — the RAG pipeline up through reranking. Generation and
conversation history are separate, later pieces; this module only answers
"which chunks, in what order, is this user actually allowed to see for
this query."

Pipeline (retrieve()):
  1. Zero-access check — hard reject before any Qdrant call.
  2. Stage scope resolution — resolve_stage_scope() (Phase A Part 1)
     INTERSECTED with get_accessible_stages_for_user() (Phase A Part 3). A
     referenced stage the user's team can't access never enters scope.
  3. Coarse Qdrant hybrid search (dense + sparse, RRF-fused), filtered to
     tenant/project/resolved-stages -> COARSE_LIMIT raw candidates.
  4. Live per-candidate-document Postgres access check via
     access_control.classify_document_visibility() (reused, not
     reimplemented) -> fully_allowed / blocked_by_sensitivity / not_visible.
  5. Keep only fully_allowed chunks. Track which documents were
     blocked_by_sensitivity (for a later "request access" suggestion —
     built elsewhere, not here). not_visible is dropped with no trace.
  6. Cross-encoder rerank of the ENTIRE fully_allowed survivor set (not a
     pre-truncated top-k) — this produces the base ordering.
  7. Relevance floor applied AFTER reranking (see reranking.py), plus a
     document-level "sibling rescue": the document behind the single best
     cross-encoder match gets up to a couple of its other coarse-ranked
     chunks kept even if the cross-encoder individually floors them (see
     _rerank's docstring for the real-data cross-encoder failure this
     fixes — it badly under-scores some enumerated/tabular content).
  8. Final top TOP_K chunks.
"""

import logging
import time
import uuid
from dataclasses import dataclass, field

from qdrant_client import models as qm
from sqlalchemy.orm import Session

from app.models.document import Document
from app.models.project import Project
from app.services.access_control import (
    DocumentVisibility,
    classify_documents_visibility,
    classify_document_visibility,
    get_accessible_stages_for_user,
    has_any_project_access,
)
from app.services.authorization_context import (
    AuthorizationContext,
    build_authorization_context,
)
from app.services.rag.collection_setup import (
    DENSE_VECTOR_NAME,
    SPARSE_VECTOR_NAME,
    collection_name_for_tenant,
    ensure_tenant_collection,
    get_qdrant_client,
)
from app.services.rag.embedding import embed_dense, embed_sparse
from app.services.rag.reranking import RELEVANCE_FLOOR, rerank_scores
from app.services.rag.stage_scope import resolve_stage_scope

logger = logging.getLogger(__name__)

COARSE_LIMIT = 50
# Chunks handed to generation. Kept small on purpose: every chunk is re-sent
# as prompt context on the generate_answer call, and the cross-encoder rerank
# above means the top few are almost always where the answer actually is.
# Was 4 — raised to 6 (matching reranking.py's own docstring, which already
# assumed TOP_K=6) after a real Ragas-caught faithfulness gap: for
# "is <product> ready for go-live", 3 of the top 4 slots were near-duplicate
# generic "1. Purpose" boilerplate sections from different documents (they
# score high because they repeat "go-live"/"launch" keywords), crowding out
# the one chunk — an actual "Status" section carrying a real caveat
# ("...pending final Business and Compliance sign-off") — which ranked #6.
TOP_K = 6


class NoProjectAccessError(Exception):
    """Raised by retrieve() when the user has no relationship to the project at all."""


@dataclass
class RetrievedChunk:
    document_id: uuid.UUID
    section_title: str
    chunk_text: str
    stage_id: uuid.UUID
    score: float  # final cross-encoder relevance score


@dataclass
class RetrievalResult:
    chunks: list[RetrievedChunk] = field(default_factory=list)
    # True if ANY candidate document was blocked_by_sensitivity for this
    # user (visible team, insufficient clearance) — generation uses this
    # to offer a "request access" suggestion.
    blocked_by_sensitivity: bool = False
    blocked_document_ids: list[uuid.UUID] = field(default_factory=list)
    timing: dict = field(default_factory=dict)


def _validate_point_payload(point) -> bool:
    """Validate that required payload fields exist and are well-formed."""
    payload = getattr(point, "payload", None)
    if not isinstance(payload, dict):
        return False
    required = ("document_id", "project_id", "stage_id", "chunk_text", "section_title")
    for k in required:
        if k not in payload or payload[k] is None:
            return False
    try:
        uuid.UUID(str(payload["document_id"]))
        uuid.UUID(str(payload["project_id"]))
        uuid.UUID(str(payload["stage_id"]))
    except (ValueError, TypeError):
        return False
    return True


def _resolve_scope(db: Session, user_id: uuid.UUID, project_id: uuid.UUID, stage_id: uuid.UUID | None) -> set[uuid.UUID]:
    """
    INTERSECTS resolve_stage_scope() with get_accessible_stages_for_user() —
    never just one or the other. A stage `stage_id` references that the
    user's team(s) can't access is excluded, even though resolve_stage_scope
    on its own would include it.
    """
    accessible = set(get_accessible_stages_for_user(db, user_id, project_id))
    if stage_id is None:
        return accessible
    raw_scope = set(resolve_stage_scope(db, stage_id))
    return raw_scope & accessible


def _coarse_search(
    client, collection: str, *, tenant_id: uuid.UUID, project_id: uuid.UUID,
    stage_ids: set[uuid.UUID], query: str, limit: int = COARSE_LIMIT,
) -> list:
    """RRF-fused hybrid (dense + sparse) search, scoped by payload filter. Returns Qdrant ScoredPoints, RRF order."""
    dense_vec = embed_dense([query])[0]
    sparse_vec = embed_sparse([query])[0]

    payload_filter = qm.Filter(
        must=[
            qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=str(tenant_id))),
            qm.FieldCondition(key="project_id", match=qm.MatchValue(value=str(project_id))),
            qm.FieldCondition(key="stage_id", match=qm.MatchAny(any=[str(s) for s in stage_ids])),
        ]
    )

    response = client.query_points(
        collection_name=collection,
        prefetch=[
            qm.Prefetch(query=dense_vec, using=DENSE_VECTOR_NAME, filter=payload_filter, limit=limit),
            qm.Prefetch(query=sparse_vec, using=SPARSE_VECTOR_NAME, filter=payload_filter, limit=limit),
        ],
        query=qm.FusionQuery(fusion=qm.Fusion.RRF),
        query_filter=payload_filter,
        limit=limit,
        with_payload=True,
    )
    return response.points


def _access_filter_candidates(
    db: Session, auth_context: AuthorizationContext, points: list,
) -> tuple[list, list[uuid.UUID]]:
    """
    Batch ABAC document access check using request-scoped AuthorizationContext.
    Executes in 1-2 queries total for all candidates rather than N queries.
    Returns (fully_allowed_points, blocked_by_sensitivity_document_ids).
    """
    valid_points = [p for p in points if _validate_point_payload(p)]
    if not valid_points:
        return [], []

    candidate_doc_ids = {uuid.UUID(p.payload["document_id"]) for p in valid_points}
    outcome_by_doc = classify_documents_visibility(db, auth_context, candidate_doc_ids)

    allowed = []
    blocked_ids: list[uuid.UUID] = []
    for p in valid_points:
        doc_id = uuid.UUID(p.payload["document_id"])
        outcome = outcome_by_doc.get(doc_id, DocumentVisibility.not_visible)
        if outcome == DocumentVisibility.fully_allowed:
            allowed.append(p)
        elif outcome == DocumentVisibility.blocked_by_sensitivity:
            if doc_id not in blocked_ids:
                blocked_ids.append(doc_id)
        # not_visible: silently dropped, no trace.

    return allowed, blocked_ids


# How many of the top cross-encoder document's OTHER coarse-ranked chunks
# to rescue even if they individually fail RELEVANCE_FLOOR — see _rerank.
_SIBLING_RESCUE_LIMIT = 2


def _rerank(query: str, points: list) -> list[RetrievedChunk]:
    """
    Cross-encoder rerank of the FULL candidate set passed in (never
    pre-truncated), floor applied after, sorted descending by score — plus
    a document-level "sibling rescue" for a real cross-encoder failure mode
    found on live data.

    A real query ("What eligibility criteria does an SME need to meet for
    FlexCredit, and how is the credit limit decided?") showed the
    cross-encoder can badly under-score jargon-heavy enumerated/tabular
    content: the chunk that actually lists the eligibility criteria scored
    deep in negative territory and was floored out entirely, while a vague
    intro sentence from the SAME document — which merely echoes the
    query's wording without answering it — scored highest of the whole
    50-candidate set. Splitting the query into single-intent sub-questions
    did not fix the cross-encoder's score for that chunk either (tested
    directly); this is the model's actual weakness on this content style,
    not a compound-query artifact. The coarse (dense+sparse) stage,
    unaffected by this, had already ranked the correct chunk #4 of 50.

    So: once the single best cross-encoder match is known, its source
    document is treated as confirmed relevant, and up to
    _SIBLING_RESCUE_LIMIT of that SAME document's other coarse-ranked
    chunks are kept even if the cross-encoder individually floors them —
    picked by coarse rank (points arrives in coarse RRF order), since that
    signal is what actually found them. This only engages for chunks
    sharing a document with the top match, so it can't inject unrelated
    noise from a different document; the tradeoff is displacing the
    lowest-ranked ordinary floor-passing chunks to make room within TOP_K.
    """
    if not points:
        return []

    texts = [p.payload["chunk_text"] for p in points]
    ce_scores = rerank_scores(query, texts)

    if max(ce_scores) <= RELEVANCE_FLOOR:
        return []

    ce_order = sorted(range(len(points)), key=lambda i: ce_scores[i], reverse=True)
    floor_passing = [i for i in ce_order if ce_scores[i] > RELEVANCE_FLOOR]

    top_doc_id = points[ce_order[0]].payload["document_id"]
    already_included = set(floor_passing)
    sibling_idxs = [
        i for i in range(len(points))
        if i not in already_included and points[i].payload["document_id"] == top_doc_id
    ]
    rescued = sibling_idxs[:_SIBLING_RESCUE_LIMIT]

    budget = max(TOP_K - len(rescued), 0)
    final_idx = floor_passing[:budget] + rescued if rescued else floor_passing

    return [
        RetrievedChunk(
            document_id=uuid.UUID(points[i].payload["document_id"]),
            section_title=points[i].payload["section_title"],
            chunk_text=points[i].payload["chunk_text"],
            stage_id=uuid.UUID(points[i].payload["stage_id"]),
            score=ce_scores[i],
        )
        for i in final_idx
    ]


def retrieve(
    db: Session,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    query: str,
    stage_id: uuid.UUID | None = None,
    auth_context: AuthorizationContext | None = None,
) -> RetrievalResult:
    """
    Executes the full retrieval pipeline:
      1. Zero-access check via request-scoped AuthorizationContext.
      2. Stage scope intersection with accessible stages.
      3. Hybrid coarse search in Qdrant (dense + sparse + RRF).
      4. Bounded batch ABAC visibility classification.
      5. Cross-encoder neural rerank + calibrated relevance floor.
      6. Final top-k chunks returned.
    """
    start_total = time.perf_counter()

    if auth_context is None:
        start_auth = time.perf_counter()
        auth_context = build_authorization_context(db, user_id, project_id)
        auth_time_ms = (time.perf_counter() - start_auth) * 1000
    else:
        auth_time_ms = 0.0

    if not auth_context.team_ids and not auth_context.is_admin:
        raise NoProjectAccessError(f"User {user_id} has no access to project {project_id}")

    # Scope resolution: accessible stages intersected with stage_scope if requested
    scope = set(auth_context.accessible_stage_ids)
    if stage_id is not None:
        raw_scope = set(resolve_stage_scope(db, stage_id))
        scope = scope & raw_scope

    if not scope:
        return RetrievalResult(timing={
            "auth_ms": auth_time_ms,
            "qdrant_ms": 0.0,
            "abac_ms": 0.0,
            "rerank_ms": 0.0,
            "retrieval_total_ms": (time.perf_counter() - start_total) * 1000,
        })

    client = get_qdrant_client()
    collection = ensure_tenant_collection(client, auth_context.tenant_id)

    start_qdrant = time.perf_counter()
    coarse_points = _coarse_search(
        client,
        collection,
        tenant_id=auth_context.tenant_id,
        project_id=project_id,
        stage_ids=scope,
        query=query,
    )
    qdrant_time_ms = (time.perf_counter() - start_qdrant) * 1000
    if not coarse_points:
        return RetrievalResult(timing={
            "auth_ms": auth_time_ms,
            "qdrant_ms": qdrant_time_ms,
            "abac_ms": 0.0,
            "rerank_ms": 0.0,
            "retrieval_total_ms": (time.perf_counter() - start_total) * 1000,
        })

    start_abac = time.perf_counter()
    allowed_points, blocked_ids = _access_filter_candidates(db, auth_context, coarse_points)
    abac_time_ms = (time.perf_counter() - start_abac) * 1000

    start_rerank = time.perf_counter()
    final_chunks = _rerank(query, allowed_points)[:TOP_K]
    rerank_time_ms = (time.perf_counter() - start_rerank) * 1000

    total_time_ms = (time.perf_counter() - start_total) * 1000
    timing = {
        "auth_ms": auth_time_ms,
        "qdrant_ms": qdrant_time_ms,
        "abac_ms": abac_time_ms,
        "rerank_ms": rerank_time_ms,
        "retrieval_total_ms": total_time_ms,
    }
    logger.debug(
        "RAG Retrieval Timing: auth_ctx=%.1fms, qdrant=%.1fms, batch_abac=%.1fms, rerank=%.1fms, total=%.1fms (candidates=%d, allowed=%d, final=%d)",
        auth_time_ms,
        qdrant_time_ms,
        abac_time_ms,
        rerank_time_ms,
        total_time_ms,
        len(coarse_points),
        len(allowed_points),
        len(final_chunks),
    )

    return RetrievalResult(
        chunks=final_chunks,
        blocked_by_sensitivity=bool(blocked_ids),
        blocked_document_ids=blocked_ids,
        timing=timing,
    )
