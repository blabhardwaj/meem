"""add reason column to access_requests

Revision ID: b5c6d7e8f9a0
Revises: a3b4c5d6e7f8
Create Date: 2026-09-18 06:00:00.000000

Free-text justification the requester types when asking for stage/
document/team access, shown to the approver alongside the request.
Nullable — existing rows and any caller that doesn't pass one are
unaffected.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b5c6d7e8f9a0"
down_revision: Union[str, None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "access_requests",
        sa.Column("reason", sa.String(length=500), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("access_requests", "reason")
