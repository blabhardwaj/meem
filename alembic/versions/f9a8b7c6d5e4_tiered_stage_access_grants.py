"""tiered stage access grants

Revision ID: f9a8b7c6d5e4
Revises: d0e1f2a3b4c5
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9a8b7c6d5e4"
down_revision: Union[str, None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # New enum types must exist before add_column can reference them —
    # op.add_column() with sa.Enum(...) does not auto-create the backing
    # Postgres type (same lesson as the c9d0e1f2a3b4 migration).
    op.execute("CREATE TYPE grant_tier AS ENUM ('viewer', 'contributor', 'contributor_confidential')")
    op.execute("CREATE TYPE grant_duration AS ENUM ('hours_72', 'week_1', 'month_1', 'unlimited')")

    # ALTER TYPE ... ADD VALUE must run as its own statement, not batched
    # with anything that uses the new value in the same implicit
    # transaction (Postgres restriction).
    op.execute("ALTER TYPE access_request_status ADD VALUE 'revoked'")

    op.add_column(
        "access_requests",
        sa.Column("tier", sa.Enum("viewer", "contributor", "contributor_confidential", name="grant_tier"), nullable=True),
    )
    op.add_column(
        "access_requests",
        sa.Column("duration", sa.Enum("hours_72", "week_1", "month_1", "unlimited", name="grant_duration"), nullable=True),
    )
    op.add_column("access_requests", sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("access_requests", sa.Column("revoked_by", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_access_requests_revoked_by", "access_requests", "users",
        ["revoked_by"], ["user_id"],
    )

    # Concurrency invariant (spec §2.3): at most one pending stage request
    # per (user, stage) — enforced as a real DB constraint, not just an
    # application-level check-then-insert.
    op.execute(
        "CREATE UNIQUE INDEX uq_access_requests_one_pending_stage_request "
        "ON access_requests (user_id, stage_id) "
        "WHERE scope = 'stage' AND status = 'pending'"
    )

    # Supporting index for the new hot resolver query (spec §2.4).
    op.execute(
        "CREATE INDEX ix_access_requests_stage_grant_lookup "
        "ON access_requests (user_id, stage_id, status, decided_at) "
        "WHERE scope = 'stage'"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_access_requests_stage_grant_lookup")
    op.execute("DROP INDEX IF EXISTS uq_access_requests_one_pending_stage_request")
    op.drop_constraint("fk_access_requests_revoked_by", "access_requests", type_="foreignkey")
    op.drop_column("access_requests", "revoked_by")
    op.drop_column("access_requests", "revoked_at")
    op.drop_column("access_requests", "duration")
    op.drop_column("access_requests", "tier")
    op.execute("DROP TYPE IF EXISTS grant_duration")
    op.execute("DROP TYPE IF EXISTS grant_tier")
    # NOTE: Postgres has no `ALTER TYPE ... DROP VALUE` — removing 'revoked'
    # from access_request_status would require recreating the whole enum
    # type and every column/index that depends on it. Deliberately left
    # as a no-op here; this is a documented, permanent limitation of this
    # downgrade, not an oversight.
