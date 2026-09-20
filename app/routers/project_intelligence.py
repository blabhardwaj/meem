import uuid
from typing import Any, Dict, List, Optional, Set
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db_with_tenant
from app.models.graph import AuditFinding, AuditRun, Edge, Node, ProjectMetricSnapshot
from app.models.project import Project
from app.models.stage import Stage
from app.schemas.graph import (
    AdvisoryDismissalDTO,
    AuditRunResponse,
    CompletionDTO,
    DismissFindingRequest,
    FindingDTO,
    FindingsResponse,
    GapsResponse,
    GraphEdgeDTO,
    GraphNodeDTO,
    NeighborhoodResponse,
    ProgressHistoryResponse,
    ProjectMetricDTO,
    ProjectMetricsResponse,
    StageMetricDTO,
    TimelineItemDTO,
    TimelineResponse,
    TriggerAuditRequest,
)
from app.models.required_document import RequiredDocument
from app.models.document import Document, DocumentVersion
from app.services.access_control import (
    classify_documents_visibility,
    DocumentVisibility,
    get_accessible_stages_for_user,
    has_any_project_access,
    can_view_document,
)
from app.services.graph.audit_rules import _resolve_coherence_evidence_document
from app.services.graph.advisory_dismissal_service import (
    compute_finding_fingerprint,
    dismiss_advisory_finding,
    get_active_dismissals_map,
    restore_advisory_finding,
)
from app.services.authorization_context import build_authorization_context
from app.services.audit import record_audit
from app.services.auth import ResolvedIdentity
from app.services.graph.audit_engine import execute_project_audit, sync_and_audit_project
from app.services.graph.completion import mark_project_complete, reopen_project_if_stale
from app.services.graph.metrics_service import (
    get_latest_project_health,
    get_project_progress_history,
    get_project_timeline,
)

router = APIRouter(prefix="/projects/{project_id}/intelligence", tags=["project-intelligence"])


def _check_project_access(db: Session, identity: ResolvedIdentity, project_id: uuid.UUID) -> Set[uuid.UUID]:
    project = db.query(Project).filter(Project.project_id == project_id).first()
    if not project or project.tenant_id != identity.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found",
        )

    if not has_any_project_access(db, identity.user_id, project_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this project",
        )

    accessible_stages = set(get_accessible_stages_for_user(db, identity.user_id, project_id))
    return accessible_stages


def _require_project_admin(identity: ResolvedIdentity, project_id: uuid.UUID) -> None:
    if identity.is_org_admin or project_id in identity.project_admin_project_ids:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Only project admins (or organization admins) can mark a project complete.",
    )


@router.get("/metrics", response_model=ProjectMetricsResponse)
def get_metrics(
    project_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db_with_tenant),
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    Returns the latest project health metrics, readiness status, and stage-by-stage health.
    Stages outside the caller's team access are redacted.

    Master Plan v2, item 5: previously ran a full synchronous audit inline
    (blocking the response for seconds) whenever no cached health existed —
    and did so WITHOUT ever syncing the graph first, so on a real
    (non-seeded) project the audit had no graph data to evaluate at all. Now
    schedules sync_and_audit_project() in the background and returns
    immediately with audit_pending=True; the frontend polls.
    """
    accessible_stages = _check_project_access(db, identity, project_id)
    project = db.get(Project, project_id)
    completion = CompletionDTO(
        is_complete=project.completed_at is not None,
        completed_at=project.completed_at.isoformat() if project.completed_at else None,
        completed_by=str(project.completed_by) if project.completed_by else None,
    )
    health = get_latest_project_health(db, project_id)

    if not health or not health["project_metric"]:
        background_tasks.add_task(
            sync_and_audit_project, project_id, project.tenant_id, identity.user_id
        )
        return ProjectMetricsResponse(
            project_id=str(project_id), project_metric=None, stages=[], audit_pending=True,
            completion=completion,
        )

    pm = health["project_metric"]
    project_metric_dto = ProjectMetricDTO(**pm)

    # Redact stages not accessible to user
    visible_stages = [
        StageMetricDTO(**s)
        for s in health["stages"]
        if uuid.UUID(s["stage_id"]) in accessible_stages
    ]

    return ProjectMetricsResponse(
        project_id=str(project_id),
        project_metric=project_metric_dto,
        stages=visible_stages,
        completion=completion,
    )


@router.get("/gaps", response_model=GapsResponse)
def get_gaps(
    project_id: uuid.UUID,
    stage_id: Optional[uuid.UUID] = Query(None, description="Optional target stage scope"),
    db: Session = Depends(get_db_with_tenant),
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    Returns current project blockers and gaps partitioned by gap type
    (missing requirements, broken dependencies, reference breaches, gate approvals, contradictions).
    """
    accessible_stages = _check_project_access(db, identity, project_id)
    if stage_id and stage_id not in accessible_stages:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access to specified stage is denied",
        )

def _build_and_filter_finding_dtos(
    db: Session,
    identity: ResolvedIdentity,
    project_id: uuid.UUID,
    findings: List[AuditFinding],
    accessible_stages: Set[uuid.UUID],
) -> List[FindingDTO]:
    """
    Applies Attribute-Based Access Control (ABAC) to audit findings:
    1. Scope check: Finding must belong to an accessible stage (or project-wide).
    2. Affected document check: If affected_entity_type == 'document', caller must have clearance
       via can_view_document(). If not cleared, the finding is completely suppressed.
    3. Evidence sources check: If finding references evidence documents, each evidence document is checked
       via can_view_document(). Any document the caller cannot view is omitted from evidence_sources.
    4. Legacy / on-the-fly resolution: If R009 finding has empty evidence_sources, dynamically resolve them
       and filter by ABAC.
    """
    # Preload docs for project to build cache
    all_project_docs = (
        db.query(Document)
        .filter(Document.project_id == project_id)
        .all()
    )
    doc_map = {d.document_id: d for d in all_project_docs}

    # Text cache for on-the-fly R009 resolution if legacy findings have empty evidence_sources
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

    active_dismissals = get_active_dismissals_map(db, project_id)

    dtos = []
    for f in findings:
        # 1. Stage access
        if f.target_stage_id is not None and f.target_stage_id not in accessible_stages:
            continue

        # 2. ABAC on affected document
        if f.affected_entity_type == "document" and f.affected_entity_id:
            aff_uuid = None
            try:
                aff_uuid = uuid.UUID(str(f.affected_entity_id))
            except (ValueError, TypeError):
                pass
            if aff_uuid:
                aff_doc = doc_map.get(aff_uuid)
                if aff_doc and not can_view_document(db, identity.user_id, aff_doc):
                    # Caller cannot view the affected document; suppress finding under ABAC
                    continue

        # 3. Evidence sources
        ev_sources = f.evidence_sources
        if (not ev_sources or len(ev_sources) == 0) and f.rule_code == "R009" and f.affected_entity_id:
            try:
                aff_uuid = uuid.UUID(str(f.affected_entity_id))
                aff_doc = doc_map.get(aff_uuid)
                if aff_doc:
                    ev_sources = [
                        {
                            "document_id": str(aff_doc.document_id),
                            "filename": aff_doc.original_filename,
                            "role": "affected",
                        }
                    ]
                    issue_mock = {
                        "related_context": (f.details or {}).get("related_context", ""),
                        "description": f.description or "",
                    }
                    conflicting_info = _resolve_coherence_evidence_document(
                        db, project_id, issue_mock, aff_doc.document_id, project_docs_cache
                    )
                    if conflicting_info:
                        cid, cfname = conflicting_info
                        ev_sources.append({
                            "document_id": str(cid),
                            "filename": cfname,
                            "role": "conflicting" if (f.details or {}).get("issue_type") == "contradiction" else "reference",
                        })
            except Exception:
                pass

        # 4. ABAC filter on evidence sources
        filtered_ev_sources = []
        if ev_sources and isinstance(ev_sources, list):
            for src in ev_sources:
                if not isinstance(src, dict):
                    continue
                d_id_raw = src.get("document_id")
                if not d_id_raw:
                    continue
                try:
                    d_uuid = uuid.UUID(str(d_id_raw))
                except (ValueError, TypeError):
                    continue
                d_obj = doc_map.get(d_uuid)
                if d_obj and can_view_document(db, identity.user_id, d_obj):
                    filtered_ev_sources.append(src)
                # If caller lacks clearance for d_obj, it is omitted!

        # 5. Deterministic fingerprint & durable dismissal resolution
        fingerprint = compute_finding_fingerprint(
            rule_code=f.rule_code,
            affected_entity_id=f.affected_entity_id,
            details=f.details,
            evidence_sources=filtered_ev_sources or ev_sources,
        )

        # Dismissal Safety Invariant:
        # An AdvisoryDismissal may only affect a finding while the current AuditFinding
        # has is_blocker == False. If a later audit classifies the same fingerprint
        # as a gate blocker, the dismissal is ignored for that run and the blocker remains fully active.
        dismissal_obj = None
        is_dismissed = False
        dismissal_info = None

        if not f.is_blocker:
            dismissal_obj = active_dismissals.get(fingerprint)
            if dismissal_obj:
                is_dismissed = True
                dismissal_info = {
                    "dismissal_id": str(dismissal_obj.dismissal_id),
                    "reason": dismissal_obj.reason,
                    "dismissed_by": str(dismissal_obj.dismissed_by),
                    "dismissed_at": dismissal_obj.dismissed_at.isoformat() if dismissal_obj.dismissed_at else None,
                }

        dtos.append(
            FindingDTO(
                finding_id=str(f.finding_id),
                rule_code=f.rule_code,
                severity=f.severity,
                is_blocker=f.is_blocker,
                title=f.title,
                description=f.description,
                affected_entity_type=f.affected_entity_type,
                affected_entity_id=str(f.affected_entity_id),
                target_stage_id=str(f.target_stage_id) if f.target_stage_id else None,
                evidence_sources=filtered_ev_sources,
                details=f.details,
                created_at=f.created_at.isoformat() if f.created_at else None,
                finding_fingerprint=fingerprint,
                is_dismissed=is_dismissed,
                dismissal=dismissal_info,
            )
        )

    return dtos


@router.get("/gaps", response_model=GapsResponse)
def get_gaps(
    project_id: uuid.UUID,
    stage_id: Optional[uuid.UUID] = Query(None, description="Optional target stage scope"),
    db: Session = Depends(get_db_with_tenant),
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    Returns current project blockers and gaps partitioned by gap type
    (missing requirements, broken dependencies, reference breaches, gate approvals, contradictions).
    """
    accessible_stages = _check_project_access(db, identity, project_id)
    if stage_id and stage_id not in accessible_stages:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access to specified stage is denied",
        )

    # Execute deterministic audit evaluation
    audit_run = execute_project_audit(db, project_id, target_stage_id=stage_id, triggered_by=identity.user_id)

    # Apply ABAC stage and document filtering
    visible_dtos = _build_and_filter_finding_dtos(
        db, identity, project_id, audit_run.findings, accessible_stages
    )

    missing_reqs: List[FindingDTO] = []
    broken_deps: List[FindingDTO] = []
    ref_violations: List[FindingDTO] = []
    unapproved_gate: List[FindingDTO] = []
    contradictions: List[FindingDTO] = []

    for dto in visible_dtos:
        if dto.rule_code == "R001":
            missing_reqs.append(dto)
        elif dto.rule_code in ("R003", "R005"):
            broken_deps.append(dto)
        elif dto.rule_code == "R002":
            unapproved_gate.append(dto)
        elif dto.rule_code in ("R007", "R009"):
            contradictions.append(dto)

    total_blockers = len([dto for dto in visible_dtos if dto.is_blocker])

    return GapsResponse(
        project_id=str(project_id),
        target_stage_id=str(stage_id) if stage_id else None,
        readiness_status=audit_run.readiness_status,
        completeness_score=audit_run.completeness_score,
        total_blockers=total_blockers,
        missing_mandatory_requirements=missing_reqs,
        broken_dependencies=broken_deps,
        permitted_reference_violations=ref_violations,
        unapproved_gate_documents=unapproved_gate,
        contradictions=contradictions,
    )


@router.get("/findings", response_model=FindingsResponse)
def get_findings(
    project_id: uuid.UUID,
    severity: Optional[str] = Query(None, description="Filter by severity (HIGH, MEDIUM, LOW)"),
    rule_code: Optional[str] = Query(None, description="Filter by rule code (e.g. R001, R002)"),
    is_blocker: Optional[bool] = Query(None, description="Filter by blocker status"),
    stage_id: Optional[uuid.UUID] = Query(None, description="Filter by target stage"),
    status: Optional[str] = Query("all", description="Status filter: all, active, dismissed"),
    db: Session = Depends(get_db_with_tenant),
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    Returns filtered list of active audit findings.
    """
    accessible_stages = _check_project_access(db, identity, project_id)

    # Find the latest audit run
    latest_run = (
        db.query(AuditRun)
        .filter(AuditRun.project_id == project_id)
        .order_by(AuditRun.started_at.desc())
        .first()
    )
    if not latest_run:
        latest_run = execute_project_audit(db, project_id, triggered_by=identity.user_id)

    query = db.query(AuditFinding).filter(AuditFinding.run_id == latest_run.run_id)

    if severity:
        query = query.filter(AuditFinding.severity == severity.upper())
    if rule_code:
        query = query.filter(AuditFinding.rule_code == rule_code.upper())
    if is_blocker is not None:
        query = query.filter(AuditFinding.is_blocker == is_blocker)
    if stage_id:
        query = query.filter(AuditFinding.target_stage_id == stage_id)

    raw_findings = query.all()

    # Apply ABAC stage, affected document, and evidence sources filtering
    dtos = _build_and_filter_finding_dtos(
        db, identity, project_id, raw_findings, accessible_stages
    )

    # Status filter (all, active, dismissed)
    status_clean = (status or "all").strip().lower()
    if status_clean == "active":
        dtos = [d for d in dtos if not d.is_dismissed]
    elif status_clean == "dismissed":
        dtos = [d for d in dtos if d.is_dismissed]

    blockers_count = len([d for d in dtos if d.is_blocker])

    return FindingsResponse(
        project_id=str(project_id),
        total_findings=len(dtos),
        blockers_count=blockers_count,
        findings=dtos,
    )


@router.post("/findings/{finding_id}/dismiss", response_model=AdvisoryDismissalDTO)
def dismiss_finding_endpoint(
    project_id: uuid.UUID,
    finding_id: uuid.UUID,
    req: DismissFindingRequest,
    db: Session = Depends(get_db_with_tenant),
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    Dismisses an advisory finding (is_blocker == False) with mandatory reason.
    Strictly restricted to Team Lead+ on the project, Project Admin, or Org Admin.
    Rejects any gating blockers (is_blocker == True) with HTTP 400.
    """
    _check_project_access(db, identity, project_id)
    dismissal = dismiss_advisory_finding(
        db=db,
        identity=identity,
        project_id=project_id,
        finding_id=finding_id,
        reason=req.reason,
    )
    return AdvisoryDismissalDTO(
        dismissal_id=str(dismissal.dismissal_id),
        project_id=str(dismissal.project_id),
        finding_fingerprint=dismissal.finding_fingerprint,
        rule_code=dismissal.rule_code,
        affected_entity_type=dismissal.affected_entity_type,
        affected_entity_id=str(dismissal.affected_entity_id),
        initial_finding_id=str(dismissal.initial_finding_id),
        is_active=dismissal.is_active,
        reason=dismissal.reason,
        dismissed_by=str(dismissal.dismissed_by),
        dismissed_at=dismissal.dismissed_at.isoformat() if dismissal.dismissed_at else "",
        restored_by=str(dismissal.restored_by) if dismissal.restored_by else None,
        restored_at=dismissal.restored_at.isoformat() if dismissal.restored_at else None,
    )


@router.post("/findings/{finding_id}/restore", response_model=AdvisoryDismissalDTO)
def restore_finding_endpoint(
    project_id: uuid.UUID,
    finding_id: uuid.UUID,
    db: Session = Depends(get_db_with_tenant),
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    Restores a previously dismissed advisory finding back to active status.
    Strictly restricted to Team Lead+ on the project, Project Admin, or Org Admin.
    """
    _check_project_access(db, identity, project_id)
    dismissal = restore_advisory_finding(
        db=db,
        identity=identity,
        project_id=project_id,
        finding_id=finding_id,
    )
    return AdvisoryDismissalDTO(
        dismissal_id=str(dismissal.dismissal_id),
        project_id=str(dismissal.project_id),
        finding_fingerprint=dismissal.finding_fingerprint,
        rule_code=dismissal.rule_code,
        affected_entity_type=dismissal.affected_entity_type,
        affected_entity_id=str(dismissal.affected_entity_id),
        initial_finding_id=str(dismissal.initial_finding_id),
        is_active=dismissal.is_active,
        reason=dismissal.reason,
        dismissed_by=str(dismissal.dismissed_by),
        dismissed_at=dismissal.dismissed_at.isoformat() if dismissal.dismissed_at else "",
        restored_by=str(dismissal.restored_by) if dismissal.restored_by else None,
        restored_at=dismissal.restored_at.isoformat() if dismissal.restored_at else None,
    )


@router.get("/neighborhood", response_model=NeighborhoodResponse)
def get_neighborhood(
    project_id: uuid.UUID,
    node_id: Optional[uuid.UUID] = Query(None),
    source_table: Optional[str] = Query(None),
    source_id: Optional[uuid.UUID] = Query(None),
    depth: int = Query(1, ge=1, le=3),
    db: Session = Depends(get_db_with_tenant),
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    Returns subgraph neighborhood around a central entity node for interactive visualization.
    """
    accessible_stages = _check_project_access(db, identity, project_id)

    center_node: Optional[Node] = None
    if node_id:
        center_node = (
            db.query(Node)
            .filter(Node.node_id == node_id, Node.project_id == project_id)
            .first()
        )
    elif source_table and source_id:
        center_node = (
            db.query(Node)
            .filter(
                Node.source_table == source_table,
                Node.source_id == source_id,
                Node.project_id == project_id,
            )
            .first()
        )

    if not center_node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Center node not found in project graph",
        )

    visited_nodes: Dict[uuid.UUID, Node] = {center_node.node_id: center_node}
    collected_edges: Dict[uuid.UUID, Edge] = {}
    current_frontier = {center_node.node_id}

    for _ in range(depth):
        if not current_frontier:
            break

        edges = (
            db.query(Edge)
            .filter(
                Edge.project_id == project_id,
                (Edge.source_node_id.in_(current_frontier) | Edge.target_node_id.in_(current_frontier)),
            )
            .all()
        )

        next_frontier = set()
        for e in edges:
            collected_edges[e.edge_id] = e
            for nid in (e.source_node_id, e.target_node_id):
                if nid not in visited_nodes:
                    next_frontier.add(nid)

        if next_frontier:
            new_nodes = (
                db.query(Node)
                .filter(Node.node_id.in_(next_frontier), Node.project_id == project_id)
                .all()
            )
            for n in new_nodes:
                visited_nodes[n.node_id] = n
            current_frontier = next_frontier
        else:
            break

    # Apply full ABAC filtering across stages, documents (sensitivity clearance), and requirements
    auth_context = build_authorization_context(db, user_id=identity.user_id, project_id=project_id)

    doc_source_ids = [n.source_id for n in visited_nodes.values() if n.source_table == "documents" and n.source_id]
    version_parent_doc_ids = []
    for n in visited_nodes.values():
        if n.source_table == "document_versions" and n.properties:
            doc_id_str = n.properties.get("document_id")
            if doc_id_str:
                try:
                    version_parent_doc_ids.append(uuid.UUID(doc_id_str))
                except (ValueError, TypeError):
                    pass
    all_doc_ids = list(set(doc_source_ids + version_parent_doc_ids))
    doc_visibility = classify_documents_visibility(db, auth_context, all_doc_ids) if all_doc_ids else {}

    req_source_ids = [n.source_id for n in visited_nodes.values() if n.source_table == "required_documents" and n.source_id]
    req_stage_map: Dict[uuid.UUID, Optional[uuid.UUID]] = {}
    if req_source_ids:
        req_rows = (
            db.query(RequiredDocument.requirement_id, RequiredDocument.stage_id)
            .filter(RequiredDocument.requirement_id.in_(req_source_ids))
            .all()
        )
        req_stage_map = {r.requirement_id: r.stage_id for r in req_rows}

    def is_visible(node: Node) -> bool:
        if node.source_table == "stages":
            return node.source_id in accessible_stages
        if node.source_table == "documents":
            # Must satisfy both stage access AND document sensitivity/confidentiality clearance
            return doc_visibility.get(node.source_id) == DocumentVisibility.fully_allowed
        if node.source_table == "document_versions":
            doc_id_str = node.properties.get("document_id") if node.properties else None
            if doc_id_str:
                try:
                    parent_doc_id = uuid.UUID(doc_id_str)
                    return doc_visibility.get(parent_doc_id) == DocumentVisibility.fully_allowed
                except (ValueError, TypeError):
                    return False
            return False
        if node.source_table == "required_documents":
            stg_id = req_stage_map.get(node.source_id)
            if stg_id is not None:
                return stg_id in accessible_stages
            prop_stage_id = node.properties.get("stage_id") if node.properties else None
            if prop_stage_id:
                try:
                    return uuid.UUID(prop_stage_id) in accessible_stages
                except (ValueError, TypeError):
                    pass
            return True
        return True

    if not is_visible(center_node):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to view this entity's graph neighborhood",
        )

    filtered_node_ids = {nid for nid, n in visited_nodes.items() if is_visible(n)}

    node_dtos = [
        GraphNodeDTO(
            node_id=str(n.node_id),
            entity_type=n.entity_type,
            source_table=n.source_table,
            source_id=str(n.source_id),
            label=n.label,
            properties=n.properties,
        )
        for nid, n in visited_nodes.items()
        if nid in filtered_node_ids
    ]

    edge_dtos = [
        GraphEdgeDTO(
            edge_id=str(e.edge_id),
            source_node_id=str(e.source_node_id),
            target_node_id=str(e.target_node_id),
            edge_type=e.edge_type,
            confidence=e.confidence,
            properties=e.properties,
        )
        for e in collected_edges.values()
        if e.source_node_id in filtered_node_ids and e.target_node_id in filtered_node_ids
    ]

    center_dto = None
    if center_node.node_id in filtered_node_ids:
        center_dto = GraphNodeDTO(
            node_id=str(center_node.node_id),
            entity_type=center_node.entity_type,
            source_table=center_node.source_table,
            source_id=str(center_node.source_id),
            label=center_node.label,
            properties=center_node.properties,
        )

    return NeighborhoodResponse(
        center_node=center_dto,
        nodes=node_dtos,
        edges=edge_dtos,
    )


@router.get("/progress-history", response_model=ProgressHistoryResponse)
def get_progress_history(
    project_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db_with_tenant),
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    Returns time-series history of project progress metrics.
    """
    _check_project_access(db, identity, project_id)
    history = get_project_progress_history(db, project_id, limit=limit)
    return ProgressHistoryResponse(project_id=str(project_id), history=history)


@router.get("/timeline", response_model=TimelineResponse)
def get_timeline(
    project_id: uuid.UUID,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db_with_tenant),
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    Returns synthesized project lifecycle events (audit runs, approvals, activities).
    """
    _check_project_access(db, identity, project_id)
    items = get_project_timeline(db, project_id, limit=limit)
    timeline_dtos = [TimelineItemDTO(**item) for item in items]
    return TimelineResponse(project_id=str(project_id), timeline=timeline_dtos)


@router.post("/audit", response_model=AuditRunResponse)
def trigger_audit(
    project_id: uuid.UUID,
    body: TriggerAuditRequest = TriggerAuditRequest(),
    db: Session = Depends(get_db_with_tenant),
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    Triggers an on-demand deterministic audit run for the project or target stage.
    """
    accessible_stages = _check_project_access(db, identity, project_id)
    if body.target_stage_id and body.target_stage_id not in accessible_stages:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access to specified stage is denied",
        )

    audit_run = execute_project_audit(
        db,
        project_id,
        target_stage_id=body.target_stage_id,
        triggered_by=identity.user_id,
    )

    return AuditRunResponse(
        run_id=str(audit_run.run_id),
        project_id=str(audit_run.project_id),
        target_stage_id=str(audit_run.target_stage_id) if audit_run.target_stage_id else None,
        readiness_status=audit_run.readiness_status,
        completeness_score=audit_run.completeness_score,
        rules_evaluated=audit_run.rules_evaluated,
        blockers_count=audit_run.summary.get("blockers_count", len([f for f in audit_run.findings if f.is_blocker])),
        findings_count=audit_run.findings_count,
        started_at=audit_run.started_at.isoformat(),
        completed_at=audit_run.completed_at.isoformat() if audit_run.completed_at else None,
    )


@router.post("/mark-complete", response_model=CompletionDTO)
def mark_complete(
    project_id: uuid.UUID,
    db: Session = Depends(get_db_with_tenant),
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    Human decision (Layer 4): project_admin/org_admin only. Readiness is
    advisory input here, not a precondition -- an admin can mark a project
    complete even while GATED, e.g. to record a deliberate override. The
    completion snapshot is taken from the latest AuditRun (running one now
    if none exists yet) so the auto-reopen check has something real to
    compare against.
    """
    project = db.get(Project, project_id)
    if project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    _require_project_admin(identity, project_id)

    latest_run = (
        db.query(AuditRun)
        .filter(AuditRun.project_id == project_id, AuditRun.target_stage_id.is_(None))
        .order_by(AuditRun.started_at.desc())
        .first()
    )
    if latest_run is None:
        latest_run = execute_project_audit(db, project_id, triggered_by=identity.user_id)

    mark_project_complete(db, project=project, actor_id=identity.user_id, latest_run=latest_run)
    record_audit(
        db, actor_id=identity.user_id, action="MARK_PROJECT_COMPLETE",
        resource_type="project", resource_id=project_id,
        details={"audit_run_id": str(latest_run.run_id)},
    )
    db.commit()
    db.refresh(project)

    return CompletionDTO(
        is_complete=True,
        completed_at=project.completed_at.isoformat(),
        completed_by=str(project.completed_by),
    )


@router.post("/reopen", response_model=CompletionDTO)
def reopen_project(
    project_id: uuid.UUID,
    db: Session = Depends(get_db_with_tenant),
    identity: ResolvedIdentity = Depends(get_current_user),
):
    """
    Manual override to clear a completion mark without waiting for the
    automatic audit-difference reopen (app/services/graph/completion.py's
    reopen_project_if_stale, which also runs after every background
    sync_and_audit_project). project_admin/org_admin only, same as marking
    complete in the first place.
    """
    project = db.get(Project, project_id)
    if project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    _require_project_admin(identity, project_id)

    if project.completed_at is not None:
        project.completed_at = None
        project.completed_by = None
        project.completion_snapshot_score = None
        project.completion_snapshot_blockers = None
        project.completion_snapshot_signature = None
        record_audit(
            db, actor_id=identity.user_id, action="REOPEN_PROJECT",
            resource_type="project", resource_id=project_id, details=None,
        )
        db.commit()

    return CompletionDTO(is_complete=False, completed_at=None, completed_by=None)
