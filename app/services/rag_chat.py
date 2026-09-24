"""
One turn of the /rag conversation, shared by the CLI (cli.py) and the HTTP
endpoint (app/routers/agents.py).

Responsibilities:

  1. SCOPING — resolve the ChatSession for this turn via
     app/services/chat_history. A conversation belongs to exactly one
     (user_id, project_id); a caller can't continue someone else's session,
     and the same user's Project A conversation can't bleed into Project B.

  2. CONTEXT — the RAG tools need the caller's real user_id/project_id, but
     that must never be an LLM-supplied argument. Set on
     app/services/rag_context around the run, reset always.

  3. HISTORY — Agno keeps its own in-context history keyed by the canonical
     session id (and user_id) in `ai.agent_sessions_runs`; we ALSO mirror
     every user/assistant turn into our own `chat_messages` so the
     conversation is queryable through first-party models and survives
     independently of Agno's schema.

The agno session id is `rag-<canonical uuid>` — namespaced so it can never
collide with a drafting/scanner session that happens to reuse the same uuid.
"""

import time
import uuid

from agno.run.base import RunStatus

from app.agents.rag_agent import rag_agent
from app.database import SessionLocal
from app.services.agent_retry import run_agent_resilient
from app.services.chat_history import append_message, resolve_chat_session
from app.services.query_context import reset_query_context, set_query_context
from app.services.rag_context import get_rag_context, reset_rag_context, set_rag_context

_TOOL_NAMES = (
    "search_documents",
    "summarize_document",
    "request_confidential_access",
    "get_project_readiness",
    "get_project_gaps",
)


class RagTurnError(Exception):
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
    return f"rag-{canonical}"


def run_rag_turn(
    *, user_id: uuid.UUID, project_id: uuid.UUID, session_id: str | None, message: str
) -> dict:
    """
    Run one RAG turn.

    Args:
        session_id: the conversation to continue — an existing session UUID,
            or None / a fresh string to start a new one. Ownership is
            enforced (see chat_history.resolve_chat_session).

    Returns:
        {
          "reply": str,
          "tools_called": list[str],
          "session_id": str,   # canonical id — pass this back on the next turn
        }

    Raises:
        SessionScopeError — session_id belongs to a different user/project.
        RagTurnError — the model/agent run failed (its error is not persisted).
    """
    db = SessionLocal()
    try:
        session = resolve_chat_session(
            db, session_id=session_id, user_id=user_id, project_id=project_id
        )
        canonical = session.session_id
        append_message(db, session_id=canonical, role="user", content=message)
        db.commit()  # the user's turn is recorded even if the agent call fails

        t_turn_start = time.perf_counter()
        rag_token = set_rag_context(user_id=user_id, project_id=project_id)
        # get_project_readiness/get_project_gaps (added so rag_agent can
        # answer audit/contradiction questions, not just content search) are
        # Query tools — they read query_context, not rag_context, so both
        # must be set around this run, same as search_chat.py does for the
        # merged Search agent.
        query_token = set_query_context(user_id=user_id, project_id=project_id)
        telemetry = {}
        try:
            t_agent_start = time.perf_counter()
            response = run_agent_resilient(
                rag_agent,
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

        # A failed model call (Agno swallows it and puts the provider error in
        # .content) must not be persisted as an assistant turn or returned as
        # an answer. The user turn stays recorded — it really was asked.
        if getattr(response, "status", None) == RunStatus.error:
            raise RagTurnError(
                getattr(response, "content", "") or "agent run failed",
                session_id=str(canonical),
            )

        reply = getattr(response, "content", "") or ""
        tools = _tools_called(response)

        append_message(db, session_id=canonical, role="assistant", content=reply)
        db.commit()

        return {
            "reply": reply,
            "tools_called": tools,
            "session_id": str(canonical),
            "timing": telemetry,
        }
    finally:
        db.close()
