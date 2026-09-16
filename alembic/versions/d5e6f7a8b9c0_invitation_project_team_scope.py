"""Master Plan v2, item 16: org onboarding wizard's invite step.

Adds optional project_id/team_id to public.invitations so an invite can
carry a team-role assignment (viewer/contributor/team_lead on a specific
team) or a project_admin scope, applied automatically when the invite is
accepted — rather than leaving every invited teammate with a bare org
account and no project access until an admin manually assigns one via
AdminPage. Both columns stay nullable: item 11's original org-wide
(role-only) invite flow is unaffected when neither is supplied.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "invitations",
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "invitations",
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_invitations_project_id", "invitations", "projects",
        ["project_id"], ["project_id"], ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_invitations_team_id", "invitations", "teams",
        ["team_id"], ["team_id"], ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint("fk_invitations_team_id", "invitations", type_="foreignkey")
    op.drop_constraint("fk_invitations_project_id", "invitations", type_="foreignkey")
    op.drop_column("invitations", "team_id")
    op.drop_column("invitations", "project_id")
