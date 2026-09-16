"""UI fix batch (2026-09-15) item #23: project description never persisted.

CreateProjectRequest already accepted a `description` field and the frontend
already collects it at project-creation time, but the Project model had no
`description` column at all — the value was silently dropped on create, and
GET /projects hardcoded description=None on every read. Adding the column so
both sides can be fixed to actually use it.

Revision ID: e2a3b4c5d6f7
Revises: d5e6f7a8b9c0
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e2a3b4c5d6f7"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("description", sa.String(length=2000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("projects", "description")
