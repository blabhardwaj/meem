"""update knowledge.nodes unique constraint to include project_id

Revision ID: e2f3a4b5c6d7
Revises: e1f2a3b4c5d6
Create Date: 2026-09-13 04:30:00.000000

Updates uq_knowledge_nodes_source on knowledge.nodes to include project_id:
(tenant_id, project_id, source_table, source_id).
Ensures users and teams participating in multiple projects have isolated project context.
"""
from typing import Sequence, Union
from alembic import op


revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        constraint_name="uq_knowledge_nodes_source",
        table_name="nodes",
        schema="knowledge",
        type_="unique",
    )
    op.create_unique_constraint(
        constraint_name="uq_knowledge_nodes_source",
        table_name="nodes",
        schema="knowledge",
        columns=["tenant_id", "project_id", "source_table", "source_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        constraint_name="uq_knowledge_nodes_source",
        table_name="nodes",
        schema="knowledge",
        type_="unique",
    )
    op.create_unique_constraint(
        constraint_name="uq_knowledge_nodes_source",
        table_name="nodes",
        schema="knowledge",
        columns=["tenant_id", "source_table", "source_id"],
    )
