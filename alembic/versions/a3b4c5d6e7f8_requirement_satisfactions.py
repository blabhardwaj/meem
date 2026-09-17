"""requirement satisfactions

Revision ID: a3b4c5d6e7f8
Revises: f9a8b7c6d5e4
Create Date: 2026-09-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, None] = "f9a8b7c6d5e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "requirement_satisfactions",
        sa.Column("satisfaction_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("requirement_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("matched_via", sa.String(length=32), nullable=False),
        sa.Column("audit_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["requirement_id"], ["required_documents.requirement_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["document_id"], ["documents.document_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["audit_run_id"], ["knowledge.audit_runs.run_id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "idx_requirement_satisfactions_document_id",
        "requirement_satisfactions",
        ["document_id"],
    )
    op.create_index(
        "idx_requirement_satisfactions_audit_run_id",
        "requirement_satisfactions",
        ["audit_run_id"],
    )

    # RLS: same 2-hop pattern as document_scans (see
    # a1b2c3d4e5f6_enable_rls_tenant_isolation.py) — scope through
    # documents.tenant_id via a join. requirement_satisfactions is a
    # current-state projection (see the model's own docstring), not
    # immutable history, but it still needs the same tenant-isolation
    # guarantee every other RLS table in this schema has.
    op.execute("""
    ALTER TABLE requirement_satisfactions ENABLE ROW LEVEL SECURITY;
    ALTER TABLE requirement_satisfactions FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON requirement_satisfactions;
    CREATE POLICY tenant_isolation ON requirement_satisfactions
        USING (EXISTS (
            SELECT 1 FROM documents d
            WHERE d.document_id = requirement_satisfactions.document_id
              AND d.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));
    """)


def downgrade() -> None:
    op.execute("""
    DROP POLICY IF EXISTS tenant_isolation ON requirement_satisfactions;
    ALTER TABLE requirement_satisfactions NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE requirement_satisfactions DISABLE ROW LEVEL SECURITY;
    """)
    op.drop_index("idx_requirement_satisfactions_audit_run_id", table_name="requirement_satisfactions")
    op.drop_index("idx_requirement_satisfactions_document_id", table_name="requirement_satisfactions")
    op.drop_table("requirement_satisfactions")
