"""Master Plan v2, item 10: document-level coherence check storage.

Adds knowledge.document_coherence_checks — caches the LLM assessment of a
document version's content against related project context (does it
contradict, duplicate, or fail to satisfy known requirements/claims), keyed
like extraction_runs (version_id, content_hash) so it's computed once per
finalize and re-evaluated on every audit sweep for free rather than calling
the LLM on every audit run.

Revision ID: b3c4d5e6f7a8
Revises: a1b2c3d4e5f6
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "document_coherence_checks",
        sa.Column("check_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("checker_version", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        # [{type: "contradiction"|"duplicate"|"unmet_requirement", description, confidence, related_context}]
        sa.Column("issues", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["public.tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["public.projects.project_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["public.documents.document_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["version_id"], ["public.document_versions.version_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("check_id"),
        sa.UniqueConstraint("version_id", "content_hash", "checker_version", name="uq_knowledge_coherence_idempotency"),
        schema="knowledge",
    )
    op.create_index(
        "idx_document_coherence_checks_document",
        "document_coherence_checks",
        ["document_id", "version_id"],
        unique=False,
        schema="knowledge",
    )


def downgrade() -> None:
    op.drop_table("document_coherence_checks", schema="knowledge")
