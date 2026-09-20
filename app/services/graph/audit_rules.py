import logging
import uuid
from typing import Any, Dict, List, Optional, Set, Tuple
from qdrant_client import models as qm
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.models.graph import DocumentCoherenceCheck, Edge, Node
from app.models.stage import Stage, TeamStageAccess
from app.models.document import Document, DocumentScan, DocumentVersion, DocumentStatus
from app.models.required_document import RequiredDocument
from app.models.workflow import WorkflowState, WorkflowStatus
from app.services.graph.llm_extraction import DIRECT_FACT_CONFIDENCE_FLOOR
from app.services.rag.collection_setup import DENSE_VECTOR_NAME, collection_name_for_tenant, get_qdrant_client
from app.services.rag.embedding import embed_dense

logger = logging.getLogger(__name__)

# Master Plan v2, item 7: cosine similarity a document chunk must clear
# against a requirement's name+description to count as semantic evidence.
# Starting value per the plan — tune from real audit data once there's
# enough of it to calibrate against.
SEMANTIC_EVIDENCE_SIMILARITY_THRESHOLD = 0.72


def _resolve_requirement_evidence(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    req_node: Node,
    req: RequiredDocument,
    stage_ids: Set[uuid.UUID],
) -> Optional[Tuple[uuid.UUID, str]]:
    """
    The SINGLE source of truth for "does evidence exist for this
    requirement, and from which document" — supersedes the old
    _has_semantic_evidence (which only returned a bool). Two independent
    consumers read this per audit run via execute_project_audit's
    precomputed evidence_by_requirement map: evaluate_r001_missing_
    mandatory_requirements (which only needs presence/absence) and
    RequirementSatisfaction persistence (which needs the document_id too).
    The two can never disagree about what counts as evidence, because
    neither computes it independently anymore.

    Callers MUST have already confirmed a Node exists for this requirement
    (source_table="required_documents") before calling this — a requirement
    with no node yet (sync_project_graph hasn't run since it was created) is
    a "not yet evaluable" state, distinct from "evaluated, no evidence
    found," and is the caller's responsibility to handle (mirrors R001's own
    pre-existing "skip silently" behavior for this case, unchanged by this
    refactor).

    Returns (document_id, matched_via) for the deterministically-chosen
    evidence, or None if no evidence exists. matched_via is "graph_edge" or
    "semantic".

    Graph-edge branch priority (highest first):
      1. Deterministic/title-matched edges (properties.source == "regex",
         relationship_extractor.py's title_or_content_evidence rule) over
         LLM-inferred edges (properties.source == "llm") at the same
         confidence tier. A document whose filename/title IS the
         requirement (e.g. "03_product_concept_and_differentiation.md" for
         a "Product concept" requirement) is a stronger, more deliberate
         evidence claim than an LLM noticing a related section inside a
         document about something else (e.g. a "Product Concept" heading
         inside a Business Case doc) — the latter is real evidence but
         should not outrank a document purpose-built for the requirement.
      2. Confidence, descending, within a priority tier.
      3. created_at, ascending (oldest first) — the final tiebreak only
         once neither of the above distinguishes two edges (e.g. two
         same-tier LLM edges at equal confidence).
    This ordering was never previously given an explicit, meaningful
    definition; recency alone (the pre-existing tiebreak) let an old,
    tangentially-related document keep "winning" over a new, purpose-built
    one forever, since nothing about a later regex/LLM pass could ever
    outrank an equal-or-higher-confidence edge already on record.

    Semantic branch (Master Plan v2, item 7): reuses the EXISTING RAG Qdrant
    infrastructure (same collection, same dense embedding model) rather than
    adding pgvector or a second embedding pipeline. This is what actually
    fixes "1 doc vs 100 docs shows the same 100%": a stage with a placeholder
    file has no chunk that's actually ABOUT the requirement, so nothing
    clears the threshold, and the requirement still shows missing. Qdrant's
    own similarity ranking (highest cosine score, limit=1) is already the
    correct deterministic "first hit" for this branch.

    No separate workflow-state/approval check is needed for the semantic
    branch: index_document() (app/services/indexing.py) only ever runs after
    should_index() has confirmed the version is Scanner-passed AND approved-
    if-the-stage-requires-it — the exact same "counts as evidence" bar the
    graph-edge branch checks explicitly. A chunk existing in Qdrant at all
    already proves its document cleared that bar.
    """
    evidence_edges = (
        db.query(Edge)
        .filter(
            Edge.tenant_id == tenant_id,
            Edge.project_id == project_id,
            Edge.target_node_id == req_node.node_id,
            Edge.edge_type.in_(["ESTABLISHES", "IMPLEMENTS", "VALIDATES", "EVIDENCES"]),
            # Item 9: regex-found edges are always 0.85-0.95 (unaffected).
            # A low-confidence LLM-found edge (0.5-0.7) must not count as
            # direct evidence — only surfaced for manual review elsewhere.
            Edge.confidence >= DIRECT_FACT_CONFIDENCE_FLOOR,
        )
        .all()
    )
    evidence_edges.sort(
        key=lambda e: (
            0 if (e.properties or {}).get("source") == "regex" else 1,
            -e.confidence,
            e.created_at,
        )
    )

    for edge in evidence_edges:
        doc_node = db.query(Node).filter(Node.node_id == edge.source_node_id).first()
        if not doc_node or doc_node.source_table != "documents":
            continue
        doc = db.query(Document).filter(Document.document_id == doc_node.source_id).first()
        if not doc:
            continue
        wf = db.query(WorkflowState).filter(WorkflowState.document_id == doc.document_id).first()
        stage = db.query(Stage).filter(Stage.stage_id == doc.stage_id).first()
        current_wf_state = wf.state.value if (wf and hasattr(wf.state, "value")) else (wf.state if wf else "draft")
        if (stage and not stage.requires_approval) or current_wf_state == "approved":
            return (doc.document_id, "graph_edge")

    # Semantic fallback: no regex/pattern-derived graph edge names a
    # document as evidence, but a document's CONTENT can still satisfy the
    # requirement without ever mentioning its filename/UUID (which is all
    # relationship_extractor.py can currently detect). Scoped to the same
    # evaluated_stage_ids as the rest of this audit run, matching the
    # graph-edge branch's own lack of per-requirement stage scoping for
    # evidence docs.
    if not stage_ids:
        return None

    query_text = req.name
    if req.description:
        query_text = f"{req.name}\n{req.description}"

    try:
        query_vector = embed_dense([query_text])[0]
    except Exception:
        logger.exception("Evidence resolution: embedding failed, treating as no match")
        return None

    client = get_qdrant_client()
    collection = collection_name_for_tenant(tenant_id)
    if not client.collection_exists(collection):
        return None  # nothing indexed yet for this tenant at all

    response = client.query_points(
        collection_name=collection,
        query=query_vector,
        using=DENSE_VECTOR_NAME,
        query_filter=qm.Filter(
            must=[
                qm.FieldCondition(key="tenant_id", match=qm.MatchValue(value=str(tenant_id))),
                qm.FieldCondition(key="project_id", match=qm.MatchValue(value=str(project_id))),
                qm.FieldCondition(key="stage_id", match=qm.MatchAny(any=[str(s) for s in stage_ids])),
            ]
        ),
        limit=1,
        score_threshold=SEMANTIC_EVIDENCE_SIMILARITY_THRESHOLD,
        with_payload=True,
    )
    if not response.points:
        return None
    doc_id_raw = response.points[0].payload.get("document_id")
    if not doc_id_raw:
        return None
    return (uuid.UUID(doc_id_raw), "semantic")


class FindingSpec:
    def __init__(
        self,
        rule_code: str,
        severity: str,
        is_blocker: bool,
        title: str,
        description: str,
        affected_entity_type: str,
        affected_entity_id: uuid.UUID,
        target_stage_id: Optional[uuid.UUID] = None,
        evidence_sources: Optional[List[Any]] = None,
        details: Optional[Dict[str, Any]] = None,
    ):
        self.rule_code = rule_code
        self.severity = severity
        self.is_blocker = is_blocker
        self.title = title
        self.description = description
        self.affected_entity_type = affected_entity_type
        self.affected_entity_id = affected_entity_id
        self.target_stage_id = target_stage_id
        self.evidence_sources = evidence_sources or []
        self.details = details or {}


def evaluate_r001_missing_mandatory_requirements(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evaluated_stage_ids: List[uuid.UUID],
    stage_name_map: Dict[uuid.UUID, str],
    evidence_by_requirement: Dict[uuid.UUID, Optional[Tuple[uuid.UUID, str]]],
) -> List[FindingSpec]:
    """
    R001: Missing Mandatory Requirement Evidence.
    Evaluates requirements applicable to evaluated stages.
    Considers both originating stage and explicit APPLIES_TO edges in the graph.
    Prevents downstream contamination (a requirement originating downstream cannot contaminate upstream).
    Flags when an applicable mandatory requirement has zero approved satisfying/evidentiary documents.

    Evidence resolution itself (graph-edge or Master Plan v2 item 7's Qdrant
    semantic fallback) is no longer done inline here — this function is now
    purely a CONSUMER of `evidence_by_requirement`, precomputed once per
    requirement by execute_project_audit via the shared
    _resolve_requirement_evidence() (app/services/graph/audit_rules.py),
    which is also what populates the RequirementSatisfaction table. The two
    can never disagree about what counts as evidence, because neither
    computes it independently anymore. A requirement_id absent from the map
    (vs. present with a None-equivalent "no evidence" value) means its graph
    Node doesn't exist yet — treated as "not yet evaluable," exactly as this
    function has always silently skipped that case (see the `continue` below).

    This IS the one deliberate exception to every other RULE_REGISTRY entry
    sharing an identical signature (see RULE_REGISTRY's own comment in
    audit_engine.py) — R001 is the only rule with a second consumer needing
    its evaluation-time byproduct.
    """
    findings: List[FindingSpec] = []
    if not evaluated_stage_ids:
        return findings

    # Active stages to determine topological ceiling (no downstream contamination)
    stages = (
        db.query(Stage)
        .filter(Stage.project_id == project_id, Stage.deleted_at.is_(None))
        .order_by(Stage.order_index.asc())
        .all()
    )
    stage_order = {s.stage_id: s.order_index for s in stages}
    max_evaluated_order = max([stage_order.get(sid, -1) for sid in evaluated_stage_ids], default=-1)

    # 1. Requirements originating in evaluated stages
    originating_reqs = (
        db.query(RequiredDocument)
        .filter(
            RequiredDocument.stage_id.in_(evaluated_stage_ids),
            RequiredDocument.is_mandatory.is_(True),
        )
        .all()
    )

    # 2. Cross-stage requirements: Requirements that have an explicit APPLIES_TO edge pointing to an evaluated stage
    eval_stage_nodes = (
        db.query(Node)
        .filter(
            Node.tenant_id == tenant_id,
            Node.project_id == project_id,
            Node.source_table == "stages",
            Node.source_id.in_(evaluated_stage_ids),
        )
        .all()
    )
    eval_stage_node_map = {n.node_id: n.source_id for n in eval_stage_nodes}

    applies_to_edges = (
        db.query(Edge)
        .filter(
            Edge.tenant_id == tenant_id,
            Edge.project_id == project_id,
            Edge.target_node_id.in_(list(eval_stage_node_map.keys())),
            Edge.edge_type == "APPLIES_TO",
        )
        .all()
    ) if eval_stage_node_map else []

    req_node_to_stage_ids: Dict[uuid.UUID, Set[uuid.UUID]] = {}
    for e in applies_to_edges:
        st_id = eval_stage_node_map.get(e.target_node_id)
        if st_id:
            req_node_to_stage_ids.setdefault(e.source_node_id, set()).add(st_id)

    cross_req_nodes = (
        db.query(Node)
        .filter(
            Node.tenant_id == tenant_id,
            Node.project_id == project_id,
            Node.source_table == "required_documents",
            Node.node_id.in_(list(req_node_to_stage_ids.keys())),
        )
        .all()
    ) if req_node_to_stage_ids else []

    node_to_req_id = {n.node_id: n.source_id for n in cross_req_nodes}
    req_target_stages: Dict[uuid.UUID, Set[uuid.UUID]] = {}
    for node_id, stage_ids in req_node_to_stage_ids.items():
        if node_id in node_to_req_id:
            req_target_stages.setdefault(node_to_req_id[node_id], set()).update(stage_ids)

    cross_reqs = (
        db.query(RequiredDocument)
        .filter(
            RequiredDocument.requirement_id.in_(list(req_target_stages.keys())),
            RequiredDocument.is_mandatory.is_(True),
        )
        .all()
    ) if req_target_stages else []

    # Combine unique requirements with target evaluated stages
    all_candidate_reqs: Dict[uuid.UUID, Tuple[RequiredDocument, Set[uuid.UUID]]] = {}
    for req in originating_reqs:
        all_candidate_reqs.setdefault(req.requirement_id, (req, set()))[1].add(req.stage_id)

    for req in cross_reqs:
        orig_order = stage_order.get(req.stage_id, 999999)
        if orig_order <= max_evaluated_order:
            entry = all_candidate_reqs.setdefault(req.requirement_id, (req, set()))
            entry[1].update(req_target_stages.get(req.requirement_id, set()))

    for req_id, (req, target_stages) in all_candidate_reqs.items():
        target_stages_in_scope = target_stages.intersection(set(evaluated_stage_ids))
        if not target_stages_in_scope:
            continue

        req_node = (
            db.query(Node)
            .filter(
                Node.tenant_id == tenant_id,
                Node.project_id == project_id,
                Node.source_table == "required_documents",
                Node.source_id == req.requirement_id,
            )
            .first()
        )
        if not req_node:
            continue

        has_approved_evidence = evidence_by_requirement.get(req.requirement_id) is not None

        if not has_approved_evidence:
            # Unapproved-but-graph-linked documents, shown to a human
            # reviewing the finding as "here's what exists but isn't
            # sufficient" — a display-only re-query of the same edges (no
            # LLM/Qdrant call), independent of the shared evidence-resolution
            # decision above. Preserved from the pre-refactor behavior;
            # AuditFindingDrawer.jsx renders this field.
            evidence_doc_titles = []
            evidence_edges = (
                db.query(Edge)
                .filter(
                    Edge.tenant_id == tenant_id,
                    Edge.project_id == project_id,
                    Edge.target_node_id == req_node.node_id,
                    Edge.edge_type.in_(["ESTABLISHES", "IMPLEMENTS", "VALIDATES", "EVIDENCES"]),
                    Edge.confidence >= DIRECT_FACT_CONFIDENCE_FLOOR,
                )
                .all()
            )
            for edge in evidence_edges:
                doc_node = db.query(Node).filter(Node.node_id == edge.source_node_id).first()
                if doc_node and doc_node.source_table == "documents":
                    doc = db.query(Document).filter(Document.document_id == doc_node.source_id).first()
                    if doc:
                        evidence_doc_titles.append(doc.original_filename)

            for t_stage_id in target_stages_in_scope:
                stage_name = stage_name_map.get(t_stage_id, "Unknown Stage")
                findings.append(
                    FindingSpec(
                        rule_code="R001",
                        severity="CRITICAL",
                        is_blocker=True,
                        title="Missing Mandatory Requirement Evidence",
                        description=f"Mandatory requirement '{req.name}' applicable to stage '{stage_name}' has no approved evidence documents.",
                        affected_entity_type="requirement",
                        affected_entity_id=req.requirement_id,
                        target_stage_id=t_stage_id,
                        details={
                            "stage_name": stage_name,
                            "entity_label": req.name,
                            "originating_stage_id": str(req.stage_id),
                            "is_mandatory": True,
                            "existing_unapproved_evidence": evidence_doc_titles,
                        },
                    )
                )

    return findings


def evaluate_r002_unapproved_documents_in_gate_stages(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evaluated_stage_ids: List[uuid.UUID],
    stage_name_map: Dict[uuid.UUID, str],
) -> List[FindingSpec]:
    """
    R002: Unapproved Document in Gate Stage.
    For stages where requires_approval = True, documents must have state 'approved'.
    Flags 'draft' or 'rejected' documents ONLY.

    Master Plan v2, item 13: previously checked `current_state != "approved"`,
    which also matched 'pending_review' — contradicting this function's own
    docstring ("documents in pending_review... are flagged by the pending-
    workflow-blocker rule, now R008 after UI_FIXES_2026-09-15.md's
    renumbering, then re-renumbered from R009 to R008 when the Cross-Stage
    Reference Violation rule that had briefly occupied R006 was retired")
    and producing two findings for the same document on every single
    pending-review case in a gate stage. Fixed to explicitly exclude
    pending_review, matching that rule's exact scope with no overlap.
    """
    findings: List[FindingSpec] = []
    gate_stages = (
        db.query(Stage)
        .filter(
            Stage.stage_id.in_(evaluated_stage_ids),
            Stage.requires_approval.is_(True),
            Stage.deleted_at.is_(None),
        )
        .all()
    )

    for stage in gate_stages:
        docs = (
            db.query(Document)
            .filter(Document.project_id == project_id, Document.stage_id == stage.stage_id)
            .all()
        )
        for doc in docs:
            wf = (
                db.query(WorkflowState)
                .filter(WorkflowState.document_id == doc.document_id)
                .first()
            )
            current_state = wf.state.value if (wf and hasattr(wf.state, "value")) else (wf.state if wf else "draft")
            if current_state in ("draft", "rejected"):
                stage_name = stage_name_map.get(stage.stage_id, stage.name)
                findings.append(
                    FindingSpec(
                        rule_code="R002",
                        severity="HIGH",
                        is_blocker=True,
                        title="Unapproved Document in Gate Stage",
                        description=f"Document '{doc.original_filename}' in approval-required stage '{stage_name}' has status '{current_state}' instead of 'approved'.",
                        affected_entity_type="document",
                        affected_entity_id=doc.document_id,
                        target_stage_id=stage.stage_id,
                        details={
                            "stage_name": stage_name,
                            "entity_label": doc.original_filename,
                            "current_state": str(current_state),
                        },
                    )
                )

    return findings



def evaluate_r003_broken_stage_dependencies(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evaluated_stage_ids: List[uuid.UUID],
    stage_name_map: Dict[uuid.UUID, str],
) -> List[FindingSpec]:
    """
    R003: Broken Stage Dependency.
    Respects directional traversal. When a document in stage S has a functional dependency
    (DEPENDS_ON) on an upstream document U, but U is unapproved or in rejected state.
    (Strictly distinct from lifecycle sequencing PRECEDES).
    """
    findings: List[FindingSpec] = []
    # Find all DEPENDS_ON edges where source document is in evaluated stages
    docs_in_scope = (
        db.query(Document)
        .filter(Document.project_id == project_id, Document.stage_id.in_(evaluated_stage_ids))
        .all()
    )
    doc_ids_in_scope = {d.document_id: d for d in docs_in_scope}

    if not doc_ids_in_scope:
        return findings

    doc_nodes = (
        db.query(Node)
        .filter(
            Node.project_id == project_id,
            Node.source_table == "documents",
            Node.source_id.in_(list(doc_ids_in_scope.keys())),
        )
        .all()
    )
    node_to_doc = {n.node_id: doc_ids_in_scope[n.source_id] for n in doc_nodes}

    if not node_to_doc:
        return findings

    dep_edges = (
        db.query(Edge)
        .filter(
            Edge.source_node_id.in_(list(node_to_doc.keys())),
            Edge.edge_type == "DEPENDS_ON",
        )
        .all()
    )

    for edge in dep_edges:
        target_node = db.query(Node).filter(Node.node_id == edge.target_node_id).first()
        if target_node and target_node.source_table == "documents":
            upstream_doc = db.query(Document).filter(Document.document_id == target_node.source_id).first()
            if upstream_doc:
                # Check upstream document workflow approval
                wf = (
                    db.query(WorkflowState)
                    .filter(WorkflowState.document_id == upstream_doc.document_id)
                    .first()
                )
                upstream_stage = db.query(Stage).filter(Stage.stage_id == upstream_doc.stage_id).first()
                requires_appr = upstream_stage.requires_approval if upstream_stage else False
                state = wf.state.value if (wf and hasattr(wf.state, "value")) else (wf.state if wf else "draft")

                if (requires_appr and state != "approved") or state == "rejected":
                    consumer_doc = node_to_doc[edge.source_node_id]
                    stage_name = stage_name_map.get(consumer_doc.stage_id, "Unknown Stage")
                    findings.append(
                        FindingSpec(
                            rule_code="R003",
                            severity="HIGH",
                            is_blocker=True,
                            title="Broken Upstream Dependency",
                            description=f"Document '{consumer_doc.original_filename}' depends on upstream document '{upstream_doc.original_filename}', which is not approved (status: '{state}').",
                            affected_entity_type="document",
                            affected_entity_id=consumer_doc.document_id,
                            target_stage_id=consumer_doc.stage_id,
                            details={
                                "stage_name": stage_name,
                                "entity_label": consumer_doc.original_filename,
                                "upstream_document": upstream_doc.original_filename,
                                "upstream_status": state,
                            },
                        )
                    )

    return findings


def evaluate_r004_stale_document_references(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evaluated_stage_ids: List[uuid.UUID],
    stage_name_map: Dict[uuid.UUID, str],
) -> List[FindingSpec]:
    """
    R004: Stale Document Reference.
    Detects when a document in an evaluated stage references an older version of another document,
    and a newer finalized/approved version of that referenced document exists.

    The finding identifies:
    - referencing document
    - referenced document
    - referenced version
    - newer available version
    - relevant stage/context
    - why the reference is stale

    Master Plan v2, item 13 — STRUCTURALLY INACTIVE TODAY, documented rather
    than deleted or hidden: this rule can only fire on an edge whose
    properties carry "referenced_version_number" or "referenced_version_id"
    (a reference pinned to a SPECIFIC version, not just the document).
    Verified: neither extraction path (relationship_extractor.py's regex
    pass, nor llm_extraction.py's LLM pass) ever writes either key — every
    REFERENCES/DEPENDS_ON edge today only names a target DOCUMENT, never a
    specific version of one. Given that, `referenced_version_number` is
    always None below, so every edge hits `continue` and this rule produces
    zero findings, unconditionally, under the current architecture. Kept
    registered (not deleted) because the logic itself is legitimate and
    becomes real the moment either extractor is extended to capture a
    version-pinned reference (e.g. "see Design Doc v2") — at which point this
    rule needs no changes to start working. Severity kept LOW/non-blocking
    per the plan's policy call: staleness is informational even when real.
    """
    findings: List[FindingSpec] = []
    docs_in_scope = (
        db.query(Document)
        .filter(Document.project_id == project_id, Document.stage_id.in_(evaluated_stage_ids))
        .all()
    )
    doc_map = {d.document_id: d for d in docs_in_scope}
    if not doc_map:
        return findings

    doc_nodes = (
        db.query(Node)
        .filter(
            Node.project_id == project_id,
            Node.source_table == "documents",
            Node.source_id.in_(list(doc_map.keys())),
        )
        .all()
    )
    node_to_doc = {n.node_id: doc_map[n.source_id] for n in doc_nodes}
    if not node_to_doc:
        return findings

    # Check REFERENCES and DEPENDS_ON edges originating from these documents
    ref_edges = (
        db.query(Edge)
        .filter(
            Edge.source_node_id.in_(list(node_to_doc.keys())),
            Edge.edge_type.in_(["REFERENCES", "DEPENDS_ON"]),
        )
        .all()
    )

    for edge in ref_edges:
        target_node = db.query(Node).filter(Node.node_id == edge.target_node_id).first()
        if not target_node:
            continue

        target_doc = None
        referenced_version_number: Optional[int] = None

        if target_node.source_table == "documents":
            target_doc = db.query(Document).filter(Document.document_id == target_node.source_id).first()
            props = edge.properties or {}
            if "referenced_version_number" in props:
                referenced_version_number = int(props["referenced_version_number"])
            elif "referenced_version_id" in props:
                ref_ver = db.query(DocumentVersion).filter(DocumentVersion.version_id == uuid.UUID(str(props["referenced_version_id"]))).first()
                if ref_ver:
                    referenced_version_number = ref_ver.version_number
        elif target_node.source_table == "document_versions":
            ref_ver = db.query(DocumentVersion).filter(DocumentVersion.version_id == target_node.source_id).first()
            if ref_ver:
                referenced_version_number = ref_ver.version_number
                target_doc = db.query(Document).filter(Document.document_id == ref_ver.document_id).first()

        if not target_doc or referenced_version_number is None:
            continue

        # Check if newer finalized/approved versions of target_doc exist
        # Finalized status is DocumentStatus.indexed
        newer_versions = (
            db.query(DocumentVersion)
            .filter(
                DocumentVersion.document_id == target_doc.document_id,
                DocumentVersion.version_number > referenced_version_number,
                DocumentVersion.status == DocumentStatus.indexed,
            )
            .order_by(DocumentVersion.version_number.desc())
            .all()
        )

        if newer_versions:
            latest_newer = newer_versions[0]
            consumer_doc = node_to_doc[edge.source_node_id]
            stage_name = stage_name_map.get(consumer_doc.stage_id, "Unknown Stage")
            findings.append(
                FindingSpec(
                    rule_code="R004",
                    severity="LOW",
                    is_blocker=False,
                    title="Stale Document Reference",
                    description=(
                        f"Document '{consumer_doc.original_filename}' in stage '{stage_name}' references "
                        f"version {referenced_version_number} of '{target_doc.original_filename}', but a newer "
                        f"finalized version (v{latest_newer.version_number}) is available."
                    ),
                    affected_entity_type="document",
                    affected_entity_id=consumer_doc.document_id,
                    target_stage_id=consumer_doc.stage_id,
                    details={
                        "stage_name": stage_name,
                        "referencing_document": consumer_doc.original_filename,
                        "referenced_document": target_doc.original_filename,
                        "referenced_version": referenced_version_number,
                        "newer_available_version": latest_newer.version_number,
                        "reason": f"Target document has been updated to finalized version {latest_newer.version_number}.",
                    },
                )
            )

    return findings


def evaluate_r005_dependency_cycles(

    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evaluated_stage_ids: List[uuid.UUID],
    stage_name_map: Dict[uuid.UUID, str],
) -> List[FindingSpec]:
    """
    R005: Dependency Cycle Detection across documents using Tarjan / DFS.
    """
    findings: List[FindingSpec] = []
    dep_edges = (
        db.query(Edge)
        .filter(Edge.project_id == project_id, Edge.edge_type == "DEPENDS_ON")
        .all()
    )
    if not dep_edges:
        return findings

    # Build adjacency graph
    adj: Dict[uuid.UUID, List[uuid.UUID]] = {}
    for edge in dep_edges:
        adj.setdefault(edge.source_node_id, []).append(edge.target_node_id)

    visited: Set[uuid.UUID] = set()
    rec_stack: Set[uuid.UUID] = set()
    cycle_nodes: Set[uuid.UUID] = set()

    def dfs(node_id: uuid.UUID):
        visited.add(node_id)
        rec_stack.add(node_id)

        for neighbor in adj.get(node_id, []):
            if neighbor not in visited:
                dfs(neighbor)
            elif neighbor in rec_stack:
                cycle_nodes.add(node_id)
                cycle_nodes.add(neighbor)

        rec_stack.remove(node_id)

    for node in list(adj.keys()):
        if node not in visited:
            dfs(node)

    for c_node_id in cycle_nodes:
        node = db.query(Node).filter(Node.node_id == c_node_id).first()
        if node and node.source_table == "documents":
            doc = db.query(Document).filter(Document.document_id == node.source_id).first()
            if doc and doc.stage_id in evaluated_stage_ids:
                stage_name = stage_name_map.get(doc.stage_id, "Unknown Stage")
                findings.append(
                    FindingSpec(
                        rule_code="R005",
                        severity="CRITICAL",
                        is_blocker=True,
                        title="Cyclic Document Dependency",
                        description=f"Document '{doc.original_filename}' is part of a circular DEPENDS_ON cycle.",
                        affected_entity_type="document",
                        affected_entity_id=doc.document_id,
                        target_stage_id=doc.stage_id,
                        details={
                            "stage_name": stage_name,
                            "entity_label": doc.original_filename,
                        },
                    )
                )

    return findings


def evaluate_r006_unassigned_stage_requirements(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evaluated_stage_ids: List[uuid.UUID],
    stage_name_map: Dict[uuid.UUID, str],
) -> List[FindingSpec]:
    """
    R006: Unassigned Stage Requirement.
    Mandatory requirement exists in a stage where team_stage_access provides 0 teams with write access.
    """
    findings: List[FindingSpec] = []
    for stage_id in evaluated_stage_ids:
        # Check if stage has write-capable teams
        has_team_access = (
            db.query(TeamStageAccess)
            .filter(TeamStageAccess.stage_id == stage_id)
            .first()
        )
        if not has_team_access:
            mandatory_reqs = (
                db.query(RequiredDocument)
                .filter(RequiredDocument.stage_id == stage_id, RequiredDocument.is_mandatory.is_(True))
                .all()
            )
            for req in mandatory_reqs:
                stage_name = stage_name_map.get(stage_id, "Unknown Stage")
                findings.append(
                    FindingSpec(
                        rule_code="R006",
                        severity="MEDIUM",
                        is_blocker=False,
                        title="Unassigned Stage Requirement",
                        description=f"Mandatory requirement '{req.name}' is in stage '{stage_name}' which has no team assigned with write/upload access.",
                        affected_entity_type="requirement",
                        affected_entity_id=req.requirement_id,
                        target_stage_id=stage_id,
                        details={
                            "stage_name": stage_name,
                            "entity_label": req.name,
                        },
                    )
                )

    return findings


def evaluate_r007_document_contradictions(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evaluated_stage_ids: List[uuid.UUID],
    stage_name_map: Dict[uuid.UUID, str],
) -> List[FindingSpec]:
    """
    R007: Contradictory Statements Across Documents.
    Evaluates semantic claims extracted into knowledge.claims for documents within evaluated stages.
    Flags direct contradictions (differing values or opposing polarities) as high-severity blockers.

    Master Plan v2, item 9: combines the exact-match deterministic pass with
    the additive embedding+LLM semantic pass (detect_semantic_contradictions)
    — the latter catches contradictions phrased differently across documents
    that exact (subject, predicate) grouping structurally cannot pair.
    """
    try:
        from app.services.graph.claims_analyzer import (
            detect_project_contradictions,
            detect_semantic_contradictions,
        )
        exact = detect_project_contradictions(
            db=db, tenant_id=tenant_id, project_id=project_id,
            evaluated_stage_ids=evaluated_stage_ids,
        )
        semantic = detect_semantic_contradictions(
            db=db, tenant_id=tenant_id, project_id=project_id,
            evaluated_stage_ids=evaluated_stage_ids,
        )
        return exact + semantic
    except (ImportError, ModuleNotFoundError):
        return []


def evaluate_r008_pending_workflow_blockers(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evaluated_stage_ids: List[uuid.UUID],
    stage_name_map: Dict[uuid.UUID, str],
) -> List[FindingSpec]:
    """
    R008: Pending Workflow Blocker.
    For stages where requires_approval = True, documents that are still awaiting approval
    (status 'pending_review') generate an R008 blocker preventing the stage from exiting.
    Clearly distinct from R002 (which specifically flags unapproved draft or rejected documents).
    """
    findings: List[FindingSpec] = []
    gate_stages = (
        db.query(Stage)
        .filter(
            Stage.stage_id.in_(evaluated_stage_ids),
            Stage.requires_approval.is_(True),
            Stage.deleted_at.is_(None),
        )
        .all()
    )

    for stage in gate_stages:
        docs = (
            db.query(Document)
            .filter(Document.project_id == project_id, Document.stage_id == stage.stage_id)
            .all()
        )
        for doc in docs:
            wf = (
                db.query(WorkflowState)
                .filter(WorkflowState.document_id == doc.document_id)
                .first()
            )
            current_state = wf.state.value if (wf and hasattr(wf.state, "value")) else (wf.state if wf else "draft")
            if current_state in (WorkflowStatus.pending_review.value, "pending_review"):
                stage_name = stage_name_map.get(stage.stage_id, stage.name)
                findings.append(
                    FindingSpec(
                        rule_code="R008",
                        severity="HIGH",
                        is_blocker=True,
                        title="Pending Workflow Blocker",
                        description=f"Document '{doc.original_filename}' in approval-required stage '{stage_name}' is currently pending review and actively blocking stage exit.",
                        affected_entity_type="document",
                        affected_entity_id=doc.document_id,
                        target_stage_id=stage.stage_id,
                        details={
                            "stage_name": stage_name,
                            "entity_label": doc.original_filename,
                            "workflow_state": str(current_state),
                            "blocks_stage_exit": True,
                        },
                    )
                )

    return findings


def evaluate_r009_document_coherence(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evaluated_stage_ids: List[uuid.UUID],
    stage_name_map: Dict[uuid.UUID, str],
    evidence_by_requirement: Optional[Dict[uuid.UUID, Optional[Tuple[uuid.UUID, str]]]] = None,
) -> List[FindingSpec]:
    """
    R009: Document-Level Coherence (Master Plan v2, item 10) — the actual
    "does this document's content make sense against the rest of the
    project" check. Deliberately separate from R007 (cross-document claim
    contradictions via the claims table) so the two don't get confused in
    the UI: R007 is a claims-table sweep across all documents; R009 is one
    document's content checked against retrieved related context.

    Reads CACHED knowledge.document_coherence_checks rows only — never
    calls the LLM during a sweep (see coherence.py's module docstring for
    why: an audit can be triggered far more often than a document is
    actually re-finalized). The cache is populated once per finalize by
    coherence.run_document_coherence_check(), called from
    audit_engine.extract_sync_and_audit_document().

    evidence_by_requirement (same precomputed map R001/RequirementSatisfaction
    use, see execute_project_audit) lets an "unmet_requirement" issue be
    checked against reality: the cached issue is historical (it was true of
    THIS document's content when it was checked) and is never deleted, but
    it should only surface as a live, actionable finding while this document
    is STILL the one currently selected as evidence for that requirement. If
    a better document has since been uploaded and now serves as the
    requirement's evidence (see _resolve_requirement_evidence's priority
    ordering), the old document's coherence complaint about a requirement
    it's no longer being relied on for is suppressed — it would otherwise
    block the project forever on a document nobody is treating as the
    answer to that requirement anymore. A coherence issue on the document
    that IS currently selected as evidence still fires exactly as before;
    this only prevents a stale, superseded complaint from blocking.

    Matching an issue to a requirement is done by name lookup (the cached
    issue's "related_context" field is a free-form string, most commonly
    "Requirement: <name>" per coherence.py's _gather_related_context — there
    is no requirement_id stored on older or current issues). This is a
    best-effort match: an issue whose related_context doesn't identify a
    requirement in scope (contradiction/duplicate issues; requirements
    outside evaluated_stage_ids) is never suppressed by this check, only
    ever a name-matched unmet_requirement issue that resolves to a
    requirement whose CURRENT evidence document differs from this one.
    """
    findings: List[FindingSpec] = []
    if not evaluated_stage_ids:
        return findings

def _resolve_coherence_evidence_document(
    db: Session,
    project_id: uuid.UUID,
    issue: dict,
    current_doc_id: uuid.UUID,
    project_docs_cache: Dict[uuid.UUID, Tuple[str, str]],
) -> Optional[Tuple[uuid.UUID, str]]:
    """
    Resolves the conflicting or evidence document for an R009 issue.
    First checks issue.get("evidence_document_id").
    Falls back to matching document filenames, parenthesized section titles,
    quoted text, or text snippets in project documents.
    """
    ev_id = issue.get("evidence_document_id")
    if ev_id:
        try:
            ev_uuid = uuid.UUID(str(ev_id))
            if ev_uuid != current_doc_id and ev_uuid in project_docs_cache:
                return ev_uuid, project_docs_cache[ev_uuid][0]
            elif ev_uuid != current_doc_id:
                d = db.get(Document, ev_uuid)
                if d:
                    return d.document_id, d.original_filename
        except Exception:
            pass

    related_ctx = str(issue.get("related_context") or "")
    desc = str(issue.get("description") or "")
    combined_text = f"{related_ctx} {desc}".lower()

    # 1. Match other document filenames or recognizable stem
    import re
    for d_id, (fname, body) in project_docs_cache.items():
        if d_id == current_doc_id:
            continue
        fname_lower = fname.lower()
        if fname_lower in combined_text:
            return d_id, fname
        stem = re.sub(r"^\d+_", "", fname).replace(".md", "").replace("_", " ").strip().lower()
        if len(stem) >= 8 and stem in combined_text:
            return d_id, fname

    # 2. Match parenthesized sections, quotes, or snippets
    patterns = [
        r'\(([\d\w\s\.\-]+)\)', # e.g. (1. Executive Summary)
        r'"([^"]+)"',           # quotes
        r'“([^”]+)”',
    ]
    candidates = []
    for pat in patterns:
        for match in re.findall(pat, related_ctx):
            m = match.strip()
            if len(m) >= 5:
                candidates.append(m)

    for line in related_ctx.split("\n"):
        line = line.strip()
        if len(line) >= 15 and not line.startswith("Requirement:"):
            candidates.append(line)

    for cand in candidates:
        cand_lower = cand.lower()
        for d_id, (fname, body) in project_docs_cache.items():
            if d_id == current_doc_id:
                continue
            if cand_lower in body.lower():
                return d_id, fname

    return None


def evaluate_r009_document_coherence(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evaluated_stage_ids: List[uuid.UUID],
    stage_name_map: Dict[uuid.UUID, str],
    evidence_by_requirement: Optional[Dict[uuid.UUID, Tuple[uuid.UUID, uuid.UUID]]] = None,
) -> List[FindingSpec]:
    """
    R009: Document-Level Coherence Issue.
    Reads cached DocumentCoherenceCheck rows for each document's current version
    and surfaces issues as findings.
    Populates evidence_sources with the affected document and the conflicting/reference evidence document.
    """
    findings: List[FindingSpec] = []
    if not evaluated_stage_ids:
        return findings

    docs = (
        db.query(Document)
        .filter(Document.project_id == project_id, Document.stage_id.in_(evaluated_stage_ids))
        .all()
    )

    all_project_docs = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .all()
    )
    project_docs_cache: Dict[uuid.UUID, Tuple[str, str]] = {}
    for pd in all_project_docs:
        body = ""
        if pd.current_version_id:
            pv = db.get(DocumentVersion, pd.current_version_id)
            if pv and pv.file_data:
                try:
                    body = pv.file_data.decode("utf-8", errors="ignore")
                except Exception:
                    body = ""
        project_docs_cache[pd.document_id] = (pd.original_filename, body)

    # Name -> requirement_id lookup, scoped to requirements in the evaluated
    # stages, for matching an issue's free-form related_context text back to
    # the requirement it's actually about.
    reqs_in_scope = (
        db.query(RequiredDocument)
        .filter(RequiredDocument.stage_id.in_(evaluated_stage_ids))
        .all()
    )
    requirement_id_by_name = {req.name: req.requirement_id for req in reqs_in_scope}

    for doc in docs:
        if not doc.current_version_id:
            continue

        check = (
            db.query(DocumentCoherenceCheck)
            .filter(
                DocumentCoherenceCheck.document_id == doc.document_id,
                DocumentCoherenceCheck.version_id == doc.current_version_id,
                DocumentCoherenceCheck.status == "completed",
            )
            .order_by(DocumentCoherenceCheck.completed_at.desc())
            .first()
        )
        if not check or not check.issues:
            continue

        stage_name = stage_name_map.get(doc.stage_id, "Unknown Stage")

        for issue in check.issues:
            if not isinstance(issue, dict):
                continue
            confidence = issue.get("confidence", 0)
            # Same confidence-gating principle as R001/R007 (item 9): a
            # low-confidence (0.5-0.7) issue never becomes a direct finding.
            if not isinstance(confidence, (int, float)) or confidence < DIRECT_FACT_CONFIDENCE_FLOOR:
                continue

            issue_type = issue.get("type", "unknown")

            if issue_type == "unmet_requirement" and evidence_by_requirement:
                related_context = issue.get("related_context", "") or ""
                matched_req_id = None
                for name, req_id in requirement_id_by_name.items():
                    if name in related_context:
                        matched_req_id = req_id
                        break
                if matched_req_id is not None:
                    current_evidence = evidence_by_requirement.get(matched_req_id)
                    if current_evidence is not None and current_evidence[0] != doc.document_id:
                        # A different, currently-selected document now
                        # serves as this requirement's evidence — this
                        # document's old complaint about failing to meet
                        # the requirement is no longer actionable.
                        continue

            # Duplication is informational (not necessarily wrong), unlike an
            # actual contradiction or a claimed-but-unmet requirement.
            is_blocker = issue_type in ("contradiction", "unmet_requirement")

            evidence_sources = [
                {
                    "document_id": str(doc.document_id),
                    "filename": doc.original_filename,
                    "role": "affected",
                }
            ]
            conflicting_info = _resolve_coherence_evidence_document(
                db, project_id, issue, doc.document_id, project_docs_cache
            )
            if conflicting_info:
                conflicting_id, conflicting_fname = conflicting_info
                evidence_sources.append({
                    "document_id": str(conflicting_id),
                    "filename": conflicting_fname,
                    "role": "conflicting" if issue_type == "contradiction" else "reference",
                })

            findings.append(
                FindingSpec(
                    rule_code="R009",
                    severity="HIGH" if is_blocker else "MEDIUM",
                    is_blocker=is_blocker,
                    title=f"Document Coherence Issue: {issue_type.replace('_', ' ').title()}",
                    description=issue.get("description", ""),
                    affected_entity_type="document",
                    affected_entity_id=doc.document_id,
                    target_stage_id=doc.stage_id,
                    evidence_sources=evidence_sources,
                    details={
                        "stage_name": stage_name,
                        "entity_label": doc.original_filename,
                        "issue_type": issue_type,
                        "related_context": issue.get("related_context", ""),
                        "confidence": confidence,
                        "checked_at": check.completed_at.isoformat() if check.completed_at else None,
                    },
                )
            )

    return findings


def evaluate_r010_scanner_flagged_current_version(
    db: Session,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    evaluated_stage_ids: List[uuid.UUID],
    stage_name_map: Dict[uuid.UUID, str],
) -> List[FindingSpec]:
    """
    R010: Scanner-Flagged Current Version.

    UI_FIXES_2026-09-15.md #28/#30: the Structure Scanner's per-version
    status (DocumentVersion.status == needs_attention, set by
    document_finalize.py on a failed score or a flagged injection check) was
    entirely invisible to the audit/findings system — R002/R008 only ever
    look at the document-level WorkflowState (human approval), a completely
    separate model. A document could show "needs_attention" in the Versions
    panel while Intelligence's "What needs attention" showed nothing at all
    for it. This rule closes that gap: it surfaces the CURRENT version only
    (an old, superseded needs_attention version is not actionable — only
    the current one blocks anything) whenever its status is needs_attention,
    independent of the human-approval WorkflowState (a version can be
    Scanner-flagged whether or not it's also pending/approved — this rule
    and R002/R008 can both fire on the same document for different reasons).
    """
    findings: List[FindingSpec] = []
    if not evaluated_stage_ids:
        return findings

    docs = (
        db.query(Document)
        .filter(Document.project_id == project_id, Document.stage_id.in_(evaluated_stage_ids))
        .all()
    )

    for doc in docs:
        if not doc.current_version_id:
            continue
        version = db.get(DocumentVersion, doc.current_version_id)
        if version is None or version.status != DocumentStatus.needs_attention:
            continue

        stage_name = stage_name_map.get(doc.stage_id, "Unknown Stage")
        scan = (
            db.query(DocumentScan)
            .filter(DocumentScan.version_id == version.version_id)
            .order_by(DocumentScan.scan_id.desc())
            .first()
        )
        score_note = f" (Structure Scanner score: {scan.overall_score}/60)" if scan else ""

        findings.append(
            FindingSpec(
                rule_code="R010",
                severity="HIGH",
                is_blocker=True,
                title="Scanner-Flagged Current Version",
                description=(
                    f"The current version of '{doc.original_filename}' in stage "
                    f"'{stage_name}' was flagged by the Structure Scanner or the "
                    f"injection check{score_note} — separate from its human-approval status."
                ),
                affected_entity_type="document",
                affected_entity_id=doc.document_id,
                target_stage_id=doc.stage_id,
                details={
                    "stage_name": stage_name,
                    "entity_label": doc.original_filename,
                    "version_id": str(version.version_id),
                    "version_number": version.version_number,
                    "scan_score": scan.overall_score if scan else None,
                },
            )
        )

    return findings
