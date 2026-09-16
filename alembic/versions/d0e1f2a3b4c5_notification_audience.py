"""add notification audience

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-09-17 00:00:01.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the enum type first
    op.execute("CREATE TYPE notification_audience AS ENUM ('approver', 'requester')")

    op.add_column(
        "notifications",
        sa.Column(
            "audience",
            sa.Enum("approver", "requester", name="notification_audience"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("notifications", "audience")
    op.execute("DROP TYPE IF EXISTS notification_audience")
