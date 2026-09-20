import uuid
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ProjectMetricDTO(BaseModel):
    snapshot_id: str
    completeness_score: float
    readiness_status: str
    mandatory_requirement_coverage: float
    approval_health: float
    dependency_health: float
    document_health: float
    version_reference_health: float
    conflict_health: float
    open_findings_by_severity: Dict[str, Any]
    blockers_count: int
    snapshot_at: str


class StageMetricDTO(BaseModel):
    stage_id: str
    stage_name: str
    completeness_score: float
    readiness_status: str
    mandatory_requirements_total: int
    mandatory_requirements_satisfied: int
    mandatory_requirements_missing: int
    blockers_count: int
    # Master Plan v2, item 8: needed to render the stage pipeline in real
    # project order and compute "Stage N of M" — None if the live Stage row
    # was since hard-deleted (StageMetricSnapshot.stage_id is deliberately
    # not FK'd, to preserve history past a stage's deletion).
    order_index: Optional[int] = None
    requires_approval: Optional[bool] = None


class CompletionDTO(BaseModel):
    """
    Layer 4 (AUDIT_RULE_TAXONOMY_MATRIX.md): a persisted human decision, not
    a computed detector. is_complete mirrors Project.completed_at != None.
    """
    is_complete: bool
    completed_at: Optional[str] = None
    completed_by: Optional[str] = None


class ProjectMetricsResponse(BaseModel):
    project_id: str
    project_metric: Optional[ProjectMetricDTO] = None
    stages: List[StageMetricDTO] = Field(default_factory=list)
    # True when no audit has ever run for this project and one was just
    # scheduled in the background (Master Plan v2, item 5) — metrics/stages
    # are empty, not necessarily "clean". Frontend should poll rather than
    # treat this as "no issues found".
    audit_pending: bool = False
    completion: CompletionDTO = Field(default_factory=lambda: CompletionDTO(is_complete=False))


class FindingDTO(BaseModel):
    finding_id: Optional[str] = None
    rule_code: str
    severity: str
    is_blocker: bool
    title: str
    description: str
    affected_entity_type: str
    affected_entity_id: str
    target_stage_id: Optional[str] = None
    target_stage_name: Optional[str] = None
    evidence_sources: List[Any] = Field(default_factory=list)
    details: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    finding_fingerprint: Optional[str] = None
    is_dismissed: bool = False
    dismissal: Optional[Dict[str, Any]] = None


class DismissFindingRequest(BaseModel):
    reason: str = Field(..., min_length=5, description="Substantive rationale for dismissing this advisory finding")


class AdvisoryDismissalDTO(BaseModel):
    dismissal_id: str
    project_id: str
    finding_fingerprint: str
    rule_code: str
    affected_entity_type: str
    affected_entity_id: str
    initial_finding_id: str
    is_active: bool
    reason: str
    dismissed_by: str
    dismissed_at: str
    restored_by: Optional[str] = None
    restored_at: Optional[str] = None


class FindingsResponse(BaseModel):
    project_id: str
    total_findings: int
    blockers_count: int
    findings: List[FindingDTO]


class GapsResponse(BaseModel):
    project_id: str
    target_stage_id: Optional[str] = None
    readiness_status: str
    completeness_score: float
    total_blockers: int
    missing_mandatory_requirements: List[FindingDTO] = Field(default_factory=list)
    broken_dependencies: List[FindingDTO] = Field(default_factory=list)
    permitted_reference_violations: List[FindingDTO] = Field(default_factory=list)
    unapproved_gate_documents: List[FindingDTO] = Field(default_factory=list)
    contradictions: List[FindingDTO] = Field(default_factory=list)


class GraphNodeDTO(BaseModel):
    node_id: str
    entity_type: str
    source_table: str
    source_id: str
    label: str
    properties: Dict[str, Any] = Field(default_factory=dict)


class GraphEdgeDTO(BaseModel):
    edge_id: str
    source_node_id: str
    target_node_id: str
    edge_type: str
    confidence: float
    properties: Dict[str, Any] = Field(default_factory=dict)


class NeighborhoodResponse(BaseModel):
    center_node: Optional[GraphNodeDTO] = None
    nodes: List[GraphNodeDTO] = Field(default_factory=list)
    edges: List[GraphEdgeDTO] = Field(default_factory=list)


class ProgressHistoryResponse(BaseModel):
    project_id: str
    history: List[Dict[str, Any]] = Field(default_factory=list)


class TimelineItemDTO(BaseModel):
    event_type: str
    timestamp: str
    title: str
    description: str
    actor_user_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TimelineResponse(BaseModel):
    project_id: str
    timeline: List[TimelineItemDTO] = Field(default_factory=list)


class TriggerAuditRequest(BaseModel):
    target_stage_id: Optional[uuid.UUID] = None


class AuditRunResponse(BaseModel):
    run_id: str
    project_id: str
    target_stage_id: Optional[str] = None
    readiness_status: str
    completeness_score: float
    rules_evaluated: int
    blockers_count: int
    findings_count: int
    started_at: str
    completed_at: Optional[str] = None
