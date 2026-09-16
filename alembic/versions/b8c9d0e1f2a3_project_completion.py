"""Project-level manual completion marking (Layer 4 in
AUDIT_RULE_TAXONOMY_MATRIX.md: a persisted human decision, not another
detector). project_admin/org_admin mark a project complete; it auto-reopens
when the next sync_and_audit_project() run produces a different
(completeness_score, blockers_count, findings_signature) than the snapshot
taken at mark-time. No stage-level equivalent -- stages only ever show
Coverage + Audit, never a completion state.

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "projects",
        sa.Column("completed_by", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True),
    )
    # Snapshot of the audit state at the moment completion was marked, used
    # solely to detect "has anything that matters changed since" on the next
    # audit run. completeness_score/blockers_count mirror AuditRun's own
    # columns; findings_signature is the sorted (rule_code, affected_entity_id)
    # pairs for is_blocker=True findings only -- see
    # app/services/graph/completion.py for why non-blocking findings are
    # deliberately excluded from it.
    op.add_column("projects", sa.Column("completion_snapshot_score", sa.Float(), nullable=True))
    op.add_column("projects", sa.Column("completion_snapshot_blockers", sa.Integer(), nullable=True))
    op.add_column("projects", sa.Column("completion_snapshot_signature", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("projects", "completion_snapshot_signature")
    op.drop_column("projects", "completion_snapshot_blockers")
    op.drop_column("projects", "completion_snapshot_score")
    op.drop_column("projects", "completed_by")
    op.drop_column("projects", "completed_at")
