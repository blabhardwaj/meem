"""Version numbering redesign: version_number now means "the Nth version
of this document to ever be APPROVED" (nullable — None until approved),
not "the Nth DocumentVersion row ever created". Adds approval_outcome
(approved/rejected), set once a version's fate is resolved: 'approved' on
the version approve_document() promotes to current, 'rejected' both on an
explicit human reject AND when a still-unresolved version is auto-
superseded by a later version being approved instead.

Existing rows keep their current version_number as-is (this migration
does not retroactively renumber history — see the version-delete feature
work for why: renumbering would rewrite a number that may already be
referenced in audit_log details, chat history, or Qdrant payloads).

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("document_versions", "version_number", nullable=True)

    approval_outcome = postgresql.ENUM("approved", "rejected", name="version_approval_outcome")
    approval_outcome.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "document_versions",
        sa.Column("approval_outcome", approval_outcome, nullable=True),
    )


def downgrade() -> None:
    op.drop_column("document_versions", "approval_outcome")
    postgresql.ENUM(name="version_approval_outcome").drop(op.get_bind(), checkfirst=True)
    op.alter_column("document_versions", "version_number", nullable=False)
