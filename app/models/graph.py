import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Node(Base):
    """
    Unified knowledge graph node representing a project entity
    (project, stage, document, requirement, team, user).
    Source of truth remains in PostgreSQL relational tables.
    """
    __tablename__ = "nodes"
    __table_args__ = (
        UniqueConstraint("tenant_id", "project_id", "source_table", "source_id", name="uq_knowledge_nodes_source"),
        {"schema": "knowledge"},
    )

    node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_table: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


class Edge(Base):
    """
    Directed relationship between two knowledge nodes.
    Supports lifecycle sequencing (PRECEDES), permitted references (ALLOWED_REFERENCE),
    dependencies (DEPENDS_ON), requirements (REQUIRES, APPLIES_TO, ORIGINATED_IN),
    and semantic evidence (ESTABLISHES, IMPLEMENTS, VALIDATES, EVIDENCES, CONFLICTS_WITH).
    """
    __tablename__ = "edges"
    __table_args__ = (
        UniqueConstraint("source_node_id", "target_node_id", "edge_type", name="uq_knowledge_edges_unique"),
        {"schema": "knowledge"},
    )

    edge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    source_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge.nodes.node_id", ondelete="CASCADE"), nullable=False
    )
    target_node_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge.nodes.node_id", ondelete="CASCADE"), nullable=False
    )
    edge_type: Mapped[str] = mapped_column(String(64), nullable=False)
    properties: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    provenance: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class Claim(Base):
    """
    Structured factual claim extracted from a specific document version.
    Supports dual-pointer contradiction and conflict analysis.
    """
    __tablename__ = "claims"
    __table_args__ = {"schema": "knowledge"}

    claim_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=False
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.version_id", ondelete="CASCADE"), nullable=False
    )
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    object: Mapped[str] = mapped_column(Text, nullable=False)
    polarity: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False)
    source_locator: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, nullable=False
    )
    confidence: Mapped[float] = mapped_column(Float, default=1.0, nullable=False)
    extraction_run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class ExtractionRun(Base):
    """
    Telemetry and idempotency tracking for semantic edge/claim extraction.
    """
    __tablename__ = "extraction_runs"
    __table_args__ = (
        UniqueConstraint("version_id", "content_hash", "extractor_version", name="uq_knowledge_extraction_idempotency"),
        {"schema": "knowledge"},
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=False
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.version_id", ondelete="CASCADE"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    extractor_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    extracted_edges_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    extracted_claims_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DocumentCoherenceCheck(Base):
    """
    Master Plan v2, item 10: caches the LLM assessment of one document
    version's content against related project context (contradiction,
    duplication, or unmet requirement). Keyed like ExtractionRun
    (version_id, content_hash) so it's computed once per finalize — the
    audit sweep's R009 rule reads the cached `issues` on every run instead
    of re-calling the LLM per audit (which can be triggered far more often
    than a document is actually re-finalized).
    """
    __tablename__ = "document_coherence_checks"
    __table_args__ = (
        UniqueConstraint("version_id", "content_hash", "checker_version", name="uq_knowledge_coherence_idempotency"),
        {"schema": "knowledge"},
    )

    check_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=False
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.version_id", ondelete="CASCADE"), nullable=False
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    checker_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # [{type: "contradiction"|"duplicate"|"unmet_requirement", description, confidence, related_context}]
    issues: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditRun(Base):
    """
    Immutable historical record of an audit execution run.
    Contains the full lifecycle snapshot of stage topology at execution time.
    """
    __tablename__ = "audit_runs"
    __table_args__ = {"schema": "knowledge"}

    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    target_stage_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    lifecycle_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rules_version: Mapped[str] = mapped_column(String(32), nullable=False)
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    rules_evaluated: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    findings_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    readiness_status: Mapped[str] = mapped_column(String(20), nullable=False)
    completeness_score: Mapped[float] = mapped_column(Float, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    findings: Mapped[list["AuditFinding"]] = relationship(
        "AuditFinding", back_populates="audit_run", cascade="all, delete-orphan"
    )


class AuditFinding(Base):
    """
    Self-contained finding emitted by the deterministic audit engine.
    Stores immutable snapshot details (stage_name, entity_label, path)
    so historical findings remain 100% interpretable even if entities are removed.
    """
    __tablename__ = "audit_findings"
    __table_args__ = {"schema": "knowledge"}

    finding_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge.audit_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    target_stage_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    rule_code: Mapped[str] = mapped_column(String(16), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    is_blocker: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    affected_entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    affected_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    evidence_sources: Mapped[list[Any]] = mapped_column(JSONB, default=list, nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    audit_run: Mapped["AuditRun"] = relationship("AuditRun", back_populates="findings")


class ProjectMetricSnapshot(Base):
    """
    Historical snapshot of project-level completeness, readiness, and dimension scores.
    Enables progress trajectory graphing over time without overwriting.
    """
    __tablename__ = "project_metric_snapshots"
    __table_args__ = {"schema": "knowledge"}

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    audit_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge.audit_runs.run_id", ondelete="SET NULL"), nullable=True
    )
    completeness_score: Mapped[float] = mapped_column(Float, nullable=False)
    readiness_status: Mapped[str] = mapped_column(String(20), nullable=False)
    mandatory_requirement_coverage: Mapped[float] = mapped_column(Float, nullable=False)
    approval_health: Mapped[float] = mapped_column(Float, nullable=False)
    dependency_health: Mapped[float] = mapped_column(Float, nullable=False)
    document_health: Mapped[float] = mapped_column(Float, nullable=False)
    version_reference_health: Mapped[float] = mapped_column(Float, nullable=False)
    conflict_health: Mapped[float] = mapped_column(Float, nullable=False)
    open_findings_by_severity: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    blockers_count: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class StageMetricSnapshot(Base):
    """
    Historical snapshot of stage-level completeness, readiness, and requirement fulfillment.
    """
    __tablename__ = "stage_metric_snapshots"
    __table_args__ = {"schema": "knowledge"}

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    stage_name: Mapped[str] = mapped_column(String(255), nullable=False)
    audit_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge.audit_runs.run_id", ondelete="SET NULL"), nullable=True
    )
    completeness_score: Mapped[float] = mapped_column(Float, nullable=False)
    readiness_status: Mapped[str] = mapped_column(String(20), nullable=False)
    mandatory_requirements_total: Mapped[int] = mapped_column(Integer, nullable=False)
    mandatory_requirements_satisfied: Mapped[int] = mapped_column(Integer, nullable=False)
    mandatory_requirements_missing: Mapped[int] = mapped_column(Integer, nullable=False)
    mandatory_requirements_partial: Mapped[int] = mapped_column(Integer, nullable=False)
    mandatory_requirements_blocked: Mapped[int] = mapped_column(Integer, nullable=False)
    upstream_requirements_applicable: Mapped[int] = mapped_column(Integer, nullable=False)
    upstream_requirements_satisfied: Mapped[int] = mapped_column(Integer, nullable=False)
    evidence_coverage: Mapped[float] = mapped_column(Float, nullable=False)
    document_health: Mapped[float] = mapped_column(Float, nullable=False)
    approvals_satisfied: Mapped[bool] = mapped_column(Boolean, nullable=False)
    blockers_count: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
