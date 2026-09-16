"""create knowledge graph schema and tables

Revision ID: e1f2a3b4c5d6
Revises: d1e2f3a4b5c6
Create Date: 2026-09-13 03:20:00.000000

Creates the PostgreSQL 'knowledge' schema and 8 core tables:
1. knowledge.nodes
2. knowledge.edges
3. knowledge.claims
4. knowledge.extraction_runs
5. knowledge.audit_runs
6. knowledge.audit_findings
7. knowledge.project_metric_snapshots
8. knowledge.stage_metric_snapshots
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create schema
    op.execute("CREATE SCHEMA IF NOT EXISTS knowledge;")

    # 2. knowledge.nodes
    op.create_table(
        "nodes",
        sa.Column("node_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("source_table", sa.String(length=64), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["public.tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["public.projects.project_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("node_id"),
        sa.UniqueConstraint("tenant_id", "project_id", "source_table", "source_id", name="uq_knowledge_nodes_source"),
        schema="knowledge",
    )
    op.create_index(
        "idx_knowledge_nodes_project",
        "nodes",
        ["tenant_id", "project_id", "entity_type"],
        unique=False,
        schema="knowledge",
    )
    op.create_index(
        "idx_knowledge_nodes_gin",
        "nodes",
        ["properties"],
        unique=False,
        postgresql_using="gin",
        schema="knowledge",
    )

    # 3. knowledge.edges
    op.create_table(
        "edges",
        sa.Column("edge_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_node_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("edge_type", sa.String(length=64), nullable=False),
        sa.Column("properties", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("confidence", sa.Float(), server_default=sa.text("1.0"), nullable=False),
        sa.Column("provenance", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["public.tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["public.projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_node_id"], ["knowledge.nodes.node_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["target_node_id"], ["knowledge.nodes.node_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("edge_id"),
        sa.UniqueConstraint("source_node_id", "target_node_id", "edge_type", name="uq_knowledge_edges_unique"),
        schema="knowledge",
    )
    op.create_index(
        "idx_knowledge_edges_lookup",
        "edges",
        ["tenant_id", "project_id", "edge_type"],
        unique=False,
        schema="knowledge",
    )
    op.create_index(
        "idx_knowledge_edges_source",
        "edges",
        ["source_node_id", "edge_type"],
        unique=False,
        schema="knowledge",
    )
    op.create_index(
        "idx_knowledge_edges_target",
        "edges",
        ["target_node_id", "edge_type"],
        unique=False,
        schema="knowledge",
    )

    # 4. knowledge.claims
    op.create_table(
        "claims",
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("predicate", sa.Text(), nullable=False),
        sa.Column("object", sa.Text(), nullable=False),
        sa.Column("polarity", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("source_locator", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("confidence", sa.Float(), server_default=sa.text("1.0"), nullable=False),
        sa.Column("extraction_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["public.tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["public.projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["public.documents.document_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["public.document_versions.version_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("claim_id"),
        schema="knowledge",
    )
    op.create_index(
        "idx_knowledge_claims_doc",
        "claims",
        ["tenant_id", "project_id", "document_id", "version_id"],
        unique=False,
        schema="knowledge",
    )

    # 5. knowledge.extraction_runs
    op.create_table(
        "extraction_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("extractor_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("extracted_edges_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("extracted_claims_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["public.tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["public.projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["public.documents.document_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["public.document_versions.version_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("version_id", "content_hash", "extractor_version", name="uq_knowledge_extraction_idempotency"),
        schema="knowledge",
    )

    # 6. knowledge.audit_runs
    op.create_table(
        "audit_runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_stage_id", postgresql.UUID(as_uuid=True), nullable=True),  # historical stage UUID reference
        sa.Column("lifecycle_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("rules_version", sa.String(length=32), nullable=False),
        sa.Column("triggered_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("rules_evaluated", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("findings_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("readiness_status", sa.String(length=20), nullable=False),
        sa.Column("completeness_score", sa.Float(), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["public.tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["public.projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["triggered_by"], ["public.users.user_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("run_id"),
        schema="knowledge",
    )
    op.create_index(
        "idx_knowledge_audit_runs",
        "audit_runs",
        ["tenant_id", "project_id", "target_stage_id", "started_at"],
        unique=False,
        schema="knowledge",
    )

    # 7. knowledge.audit_findings
    op.create_table(
        "audit_findings",
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_stage_id", postgresql.UUID(as_uuid=True), nullable=True),  # historical stage UUID reference
        sa.Column("rule_code", sa.String(length=16), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False),
        sa.Column("is_blocker", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("affected_entity_type", sa.String(length=64), nullable=False),
        sa.Column("affected_entity_id", postgresql.UUID(as_uuid=True), nullable=False),  # unconstrained historical reference UUID
        sa.Column("evidence_sources", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["knowledge.audit_runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["public.tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["public.projects.project_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("finding_id"),
        schema="knowledge",
    )
    op.create_index(
        "idx_knowledge_audit_findings_run",
        "audit_findings",
        ["run_id", "rule_code"],
        unique=False,
        schema="knowledge",
    )
    op.create_index(
        "idx_knowledge_audit_findings_entity",
        "audit_findings",
        ["tenant_id", "project_id", "affected_entity_id"],
        unique=False,
        schema="knowledge",
    )

    # 8. knowledge.project_metric_snapshots
    op.create_table(
        "project_metric_snapshots",
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("audit_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completeness_score", sa.Float(), nullable=False),
        sa.Column("readiness_status", sa.String(length=20), nullable=False),
        sa.Column("mandatory_requirement_coverage", sa.Float(), nullable=False),
        sa.Column("approval_health", sa.Float(), nullable=False),
        sa.Column("dependency_health", sa.Float(), nullable=False),
        sa.Column("document_health", sa.Float(), nullable=False),
        sa.Column("version_reference_health", sa.Float(), nullable=False),
        sa.Column("conflict_health", sa.Float(), nullable=False),
        sa.Column("open_findings_by_severity", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("blockers_count", sa.Integer(), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["public.tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["public.projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["audit_run_id"], ["knowledge.audit_runs.run_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("snapshot_id"),
        schema="knowledge",
    )
    op.create_index(
        "idx_project_metric_snapshots",
        "project_metric_snapshots",
        ["tenant_id", "project_id", "snapshot_at"],
        unique=False,
        schema="knowledge",
    )

    # 9. knowledge.stage_metric_snapshots
    op.create_table(
        "stage_metric_snapshots",
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stage_id", postgresql.UUID(as_uuid=True), nullable=False),  # historical stage UUID reference (no cascade delete!)
        sa.Column("stage_name", sa.String(length=255), nullable=False),  # historical stage name snapshot
        sa.Column("audit_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("completeness_score", sa.Float(), nullable=False),
        sa.Column("readiness_status", sa.String(length=20), nullable=False),
        sa.Column("mandatory_requirements_total", sa.Integer(), nullable=False),
        sa.Column("mandatory_requirements_satisfied", sa.Integer(), nullable=False),
        sa.Column("mandatory_requirements_missing", sa.Integer(), nullable=False),
        sa.Column("mandatory_requirements_partial", sa.Integer(), nullable=False),
        sa.Column("mandatory_requirements_blocked", sa.Integer(), nullable=False),
        sa.Column("upstream_requirements_applicable", sa.Integer(), nullable=False),
        sa.Column("upstream_requirements_satisfied", sa.Integer(), nullable=False),
        sa.Column("evidence_coverage", sa.Float(), nullable=False),
        sa.Column("document_health", sa.Float(), nullable=False),
        sa.Column("approvals_satisfied", sa.Boolean(), nullable=False),
        sa.Column("blockers_count", sa.Integer(), nullable=False),
        sa.Column("snapshot_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["public.tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["public.projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["audit_run_id"], ["knowledge.audit_runs.run_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("snapshot_id"),
        schema="knowledge",
    )
    op.create_index(
        "idx_stage_metric_snapshots",
        "stage_metric_snapshots",
        ["tenant_id", "project_id", "stage_id", "snapshot_at"],
        unique=False,
        schema="knowledge",
    )


def downgrade() -> None:
    op.drop_table("stage_metric_snapshots", schema="knowledge")
    op.drop_table("project_metric_snapshots", schema="knowledge")
    op.drop_table("audit_findings", schema="knowledge")
    op.drop_table("audit_runs", schema="knowledge")
    op.drop_table("extraction_runs", schema="knowledge")
    op.drop_table("claims", schema="knowledge")
    op.drop_table("edges", schema="knowledge")
    op.drop_table("nodes", schema="knowledge")
    op.execute("DROP SCHEMA IF EXISTS knowledge CASCADE;")
