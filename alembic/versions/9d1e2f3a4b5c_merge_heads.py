"""merge heads (access_request_reason + node_unique_constraint branches)

Revision ID: 9d1e2f3a4b5c
Revises: b5c6d7e8f9a0, e2f3a4b5c6d7
Create Date: 2026-09-23 20:00:00.000000

Pure merge — no schema changes. The migration graph had two independent
heads (b5c6d7e8f9a0 "add reason column to access_requests" and e2f3a4b5c6d7
"update knowledge.nodes unique constraint"), diverged from different
earlier ancestors. This merges them into a single head so the next
migration (ai_usage table, Change 4 of PRODUCTION_READINESS_PLAN.md) has
one place to chain onto.
"""
from typing import Sequence, Union


revision: str = "9d1e2f3a4b5c"
down_revision: Union[str, None] = ("b5c6d7e8f9a0", "e2f3a4b5c6d7")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
