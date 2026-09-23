"""
Change 4 (PRODUCTION_READINESS_PLAN.md): a minimal per-tenant AI usage
gate — not a billing system. One counter row per tenant (app/models/ai_usage.py),
checked and incremented atomically before an AI call, so one tenant can't
consume unlimited capacity.

Gated at the 7 OUTER chat/agent entry points (draft_chat, document_review_chat,
document_version_review, query_chat, rag_chat, scan_chat, search_chat) —
not at every individual LLM call site. The other ~8 raw-Groq-client call
sites (draft_generator, revision_generator, graph/llm_extraction, etc.) only
ever fire as tool-calls nested inside one of those 7 outer agent.run()
calls, so gating the outer call transitively covers them. Known
simplification: a single user turn that fires multiple nested LLM calls
still only counts as one usage unit — acceptable for a same-day minimum
viable version, not silently glossed over.
"""
import uuid

from sqlalchemy import update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models.ai_usage import AIUsage

DEFAULT_CALLS_LIMIT = 2000


class AIUsageLimitExceededError(Exception):
    """Raised when a tenant has exhausted its configured AI call limit."""

    def __init__(self, tenant_id: uuid.UUID):
        self.tenant_id = tenant_id
        super().__init__(f"AI usage limit reached for tenant {tenant_id}")


def check_and_consume_ai_usage(db: Session, tenant_id: uuid.UUID) -> None:
    """
    Atomically checks a tenant's AI usage against its limit and, if under
    it, increments the counter — one statement, so two concurrent calls
    from the same tenant can't both slip through at the boundary. Call this
    immediately before the outer agent/model call in each of the 7 chat
    entry points.

    Lazily creates the tenant's row on first use (ON CONFLICT DO NOTHING)
    as a second line of defense alongside the migration's backfill, for any
    tenant created after that backfill ran.

    Raises:
        AIUsageLimitExceededError: the tenant has no remaining calls this period.
    """
    db.execute(
        insert(AIUsage)
        .values(tenant_id=tenant_id, calls_used=0, calls_limit=DEFAULT_CALLS_LIMIT)
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )

    result = db.execute(
        update(AIUsage)
        .where(AIUsage.tenant_id == tenant_id, AIUsage.calls_used < AIUsage.calls_limit)
        .values(calls_used=AIUsage.calls_used + 1)
        .returning(AIUsage.calls_used)
    )
    if result.first() is None:
        raise AIUsageLimitExceededError(tenant_id)
