"""add confidential access grant scopes

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-09-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, None] = "b8c9d0e1f2a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create the enum type first
    op.execute("CREATE TYPE access_request_scope AS ENUM ('document', 'stage', 'team')")

    op.add_column(
        "access_requests",
        sa.Column(
            "scope",
            sa.Enum("document", "stage", "team", name="access_request_scope"),
            nullable=False,
            server_default="team",
        ),
    )
    op.add_column("access_requests", sa.Column("document_id", sa.UUID(), nullable=True))
    op.add_column("access_requests", sa.Column("stage_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_access_requests_document_id", "access_requests", "documents",
        ["document_id"], ["document_id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_access_requests_stage_id", "access_requests", "stages",
        ["stage_id"], ["stage_id"], ondelete="CASCADE",
    )
    op.create_check_constraint(
        "ck_access_request_scope_target",
        "access_requests",
        "(scope = 'document' AND document_id IS NOT NULL AND stage_id IS NULL) OR "
        "(scope = 'stage' AND stage_id IS NOT NULL AND document_id IS NULL) OR "
        "(scope = 'team' AND document_id IS NULL AND stage_id IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_access_request_scope_target", "access_requests", type_="check")
    op.drop_constraint("fk_access_requests_stage_id", "access_requests", type_="foreignkey")
    op.drop_constraint("fk_access_requests_document_id", "access_requests", type_="foreignkey")
    op.drop_column("access_requests", "stage_id")
    op.drop_column("access_requests", "document_id")
    op.drop_column("access_requests", "scope")
    op.execute("DROP TYPE IF EXISTS access_request_scope")
