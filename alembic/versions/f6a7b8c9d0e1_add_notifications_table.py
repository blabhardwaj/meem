"""Notifications feature: per-recipient event rows (document approved/
rejected, pending-review assignment, confidential-access request/decision,
role change, newly-approved-and-indexed document fan-out). RLS follows the
access_requests pattern (join through users to tenant_id), since this is
per-user private data, not an admin audit trail.

Revision ID: f6a7b8c9d0e1
Revises: e2a3b4c5d6f7
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e2a3b4c5d6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NOTIFICATION_TYPES = (
    "document_approved",
    "document_rejected",
    "document_pending_review",
    "document_available",
    "access_request_created",
    "access_request_approved",
    "access_request_denied",
    "role_changed",
)


def upgrade() -> None:
    notification_type = postgresql.ENUM(
        *_NOTIFICATION_TYPES, name="notification_type"
    )

    op.create_table(
        "notifications",
        sa.Column("notification_id", sa.UUID(), nullable=False),
        sa.Column("recipient_user_id", sa.UUID(), nullable=False),
        sa.Column("notification_type", notification_type, nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("resource_type", sa.String(length=255), nullable=True),
        sa.Column("resource_id", sa.UUID(), nullable=True),
        sa.Column("project_id", sa.UUID(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"]),
        sa.PrimaryKeyConstraint("notification_id"),
    )
    op.create_index(
        "idx_notifications_recipient_created",
        "notifications",
        ["recipient_user_id", "created_at"],
    )

    op.execute("""
    ALTER TABLE notifications ENABLE ROW LEVEL SECURITY;
    ALTER TABLE notifications FORCE ROW LEVEL SECURITY;
    DROP POLICY IF EXISTS tenant_isolation ON notifications;
    CREATE POLICY tenant_isolation ON notifications
        USING (EXISTS (
            SELECT 1 FROM users u
            WHERE u.user_id = notifications.recipient_user_id
              AND u.tenant_id = NULLIF(current_setting('app.current_tenant_id', true), '')::uuid
        ));
    """)


def downgrade() -> None:
    op.execute("""
    DROP POLICY IF EXISTS tenant_isolation ON notifications;
    ALTER TABLE notifications NO FORCE ROW LEVEL SECURITY;
    ALTER TABLE notifications DISABLE ROW LEVEL SECURITY;
    """)
    op.drop_index("idx_notifications_recipient_created", table_name="notifications")
    op.drop_table("notifications")
    postgresql.ENUM(name="notification_type").drop(op.get_bind(), checkfirst=True)
