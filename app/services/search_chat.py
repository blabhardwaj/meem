"""
One turn of the /search conversation (the merged Search tab), replacing the
separate /rag and /query conversations. Mirrors their scoping, context, and
dual-history model exactly, but drives the single search_agent, which carries
both agents' tools.

  1. SCOPING — resolve_chat_session with mode="search". A conversation
     belongs to exactly one (user_id, project_id).

  2. CONTEXT — both the RAG tools and the Query tools read caller identity
     from their own ContextVar (rag_context / query_context respectively);
     both are set together around the run and reset always, since either
     tool family may be called in a single turn.

  3. HISTORY — mirrored into chat_messages exactly as rag_chat/query_chat did.

The agno session id is `search-<canonical uuid>` — namespaced so it can never
collide with a drafting/scanner/legacy rag or query session that reuses the
same uuid.
"""

import time
import uuid

from agno.run.base import RunStatus
from sqlalchemy import text

from app.agents.search_agent import search_agent
from app.database import SessionLocal
from app.services.agent_retry import run_agent_resilient
from app.services.ai_usage import check_and_consume_ai_usage
from app.services.chat_history import append_message, resolve_chat_session
from app.services.rag_context import get_rag_context, reset_rag_context, set_rag_context
from app.services.query_context import reset_query_context, set_query_context

try:
    # app/services/demo_cache.py is gitignored (demo-rehearsal tooling, not
    # committed) — optional on purpose, so a clone without it still boots and
    # behaves exactly as if DEMO_CACHE_MODE were unset (no-op both ways).
    from app.services.demo_cache import find_cached_response, record_response
except ModuleNotFoundError:
    def find_cached_response(project_id, user_id, question):  # noqa: ANN001, ARG001
        return None

    def record_response(project_id, user_id, question, response):  # noqa: ANN001, ARG001
        pass

_TOOL_NAMES = (
    "search_documents",
    "summarize_document",
    "request_confidential_access",
    "list_accessible_documents",
    "get_document_info",
    "get_version_history",
    "who_can_approve",
    "list_pending_approvals",
    "check_my_access",
    "get_project_structure",
    "get_my_accessible_stages",
    "get_stage_requirements",
    "get_stage_document_status",
    "get_project_readiness",
    "get_project_gaps",
)


class SearchTurnError(Exception):
    """
    The agent run itself failed (e.g. the model provider errored). The user's
    message is still recorded; `session_id` is the canonical conversation id
    so the caller can retry the same turn.
    """

    def __init__(self, message: str, *, session_id: str | None = None):
        super().__init__(message)
        self.session_id = session_id


def _tools_called(response) -> list[str]:
    tools = getattr(response, "tools", None) or []
    return [t.tool_name for t in tools if getattr(t, "tool_name", None) in _TOOL_NAMES]


def _agno_session_id(canonical: uuid.UUID) -> str:
    return f"search-{canonical}"


def run_search_turn(
    *, user_id: uuid.UUID, project_id: uuid.UUID, tenant_id: uuid.UUID,
    session_id: str | None, message: str,
) -> dict:
    """
    Run one Search turn.

    Returns:
        {"reply": str, "tools_called": list[str], "session_id": str, "timing": dict}

    Raises:
        SessionScopeError — session_id belongs to a different user/project.
        SearchTurnError — the model/agent run failed (its error is not persisted).
    """
    db = SessionLocal()
    try:
        # chat_sessions/chat_messages FORCE row level security (even the table
        # owner is checked) — db.info alone only takes effect on the NEXT
        # transaction (see app/database.py's after_begin listener), so an
        # explicit SET LOCAL is needed for the very first statement on this
        # session too. Mirrors the pattern in app/tools/graph_tools.py.
        db.info["tenant_id"] = str(tenant_id)
        db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(tenant_id)})

        session = resolve_chat_session(
            db, session_id=session_id, user_id=user_id, project_id=project_id, mode="search"
        )
        canonical = session.session_id
        append_message(db, session_id=canonical, role="user", content=message)
        db.commit()  # the user's turn is recorded even if the agent call fails

        t_turn_start = time.perf_counter()

        # Demo-cache lookup (see app/services/demo_cache.py) — a no-op unless
        # DEMO_CACHE_MODE=replay. A hit skips the live agent call (and its AI
        # usage charge) entirely; a miss falls through to the real call below
        # exactly as if caching were off.
        cached = find_cached_response(project_id, user_id, message or "")
        if cached is not None:
            reply = cached.get("reply", "")
            tools = cached.get("tools_called", [])
            append_message(db, session_id=canonical, role="assistant", content=reply)
            db.commit()
            t_turn_total = (time.perf_counter() - t_turn_start) * 1000
            return {
                "reply": reply,
                "tools_called": tools,
                "session_id": str(canonical),
                "timing": {"turn_total_ms": t_turn_total, "demo_cache_hit": True},
            }

        # Change 4 (PRODUCTION_READINESS_PLAN.md) — committed immediately so
        # an attempted call counts even if the agent run itself fails below.
        check_and_consume_ai_usage(db, tenant_id)
        db.commit()

        rag_token = set_rag_context(user_id=user_id, project_id=project_id, tenant_id=tenant_id)
        query_token = set_query_context(user_id=user_id, project_id=project_id)
        telemetry = {}
        try:
            t_agent_start = time.perf_counter()
            response = run_agent_resilient(
                search_agent,
                message or "continue",
                session_id=_agno_session_id(canonical),
                user_id=str(user_id),
            )
            t_agent = (time.perf_counter() - t_agent_start) * 1000
            ctx = get_rag_context()
            telemetry = dict(ctx.telemetry)
            telemetry["agent_total_ms"] = t_agent
        finally:
            reset_query_context(query_token)
            reset_rag_context(rag_token)

        t_turn_total = (time.perf_counter() - t_turn_start) * 1000
        telemetry["turn_total_ms"] = t_turn_total

        if getattr(response, "status", None) == RunStatus.error:
            raise SearchTurnError(
                getattr(response, "content", "") or "agent run failed",
                session_id=str(canonical),
            )

        reply = getattr(response, "content", "") or ""
        tools = _tools_called(response)

        append_message(db, session_id=canonical, role="assistant", content=reply)
        db.commit()

        # Demo-cache record (see app/services/demo_cache.py) — a no-op unless
        # DEMO_CACHE_MODE=record. Run during rehearsal to build up the cache
        # from real live calls before switching to replay for the live demo.
        record_response(project_id, user_id, message or "", {"reply": reply, "tools_called": tools})

        return {
            "reply": reply,
            "tools_called": tools,
            "session_id": str(canonical),
            "timing": telemetry,
        }
    finally:
        db.close()
