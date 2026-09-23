"""Change 4 (PRODUCTION_READINESS_PLAN.md): per-tenant AI usage limiting.

One row per tenant holding a running call count against a configurable
limit. Deliberately minimal — a single rolling counter, no time-bucketed
reset logic — the smallest thing that satisfies "reject when exhausted."
Backfills one row per existing tenant so no tenant is left without a row
(the application-side check also lazily creates one on first use, as a
second line of defense for any tenant created between this migration and
that backfill, or in a differently-ordered deploy).

Revision ID: 8c7b6a5d4e3f
Revises: 9d1e2f3a4b5c
Create Date: 2026-09-23 20:05:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8c7b6a5d4e3f"
down_revision: Union[str, None] = "9d1e2f3a4b5c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Generous on purpose: this is a same-day safety net against runaway usage,
# not a real billing plan — a demo tenant should never realistically hit
# this. Raise/lower per-tenant later via a direct UPDATE once real plans
# exist.
DEFAULT_CALLS_LIMIT = 2000


def upgrade() -> None:
    op.create_table(
        "ai_usage",
        sa.Column("tenant_id", sa.UUID(), nullable=False),
        sa.Column("calls_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("calls_limit", sa.Integer(), nullable=False, server_default=str(DEFAULT_CALLS_LIMIT)),
        sa.Column("period_started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    # Backfill one row per existing tenant.
    op.execute(
        f"""
        INSERT INTO ai_usage (tenant_id, calls_used, calls_limit, period_started_at)
        SELECT tenant_id, 0, {DEFAULT_CALLS_LIMIT}, now() FROM tenants
        ON CONFLICT (tenant_id) DO NOTHING;
        """
    )

    # Same RLS convention as every other tenant-scoped table (see e.g.
    # f6a7b8c9d0e1_add_notifications_table.py) — here keyed directly on
    # tenant_id rather than joined through users, since this table's PK IS
    # the tenant.
    op.execute("""
    ALTER TABLE ai_usage ENABLE ROW LEVEL SECURITY;
    ALTER TABLE ai_usage FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON ai_usage;
    CREATE POLICY tenant_isolation ON ai_usage
        USING (tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid);
    """)


def downgrade() -> None:
    op.execute("""
    DROP POLICY IF EXISTS tenant_isolation ON ai_usage;
    ALTER TABLE ai_usage NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE ai_usage DISABLE ROW LEVEL SECURITY;
    """)
    op.drop_table("ai_usage")
