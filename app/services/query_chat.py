"""
One turn of the /query conversation, exposed over HTTP by
app/routers/agents.py (POST /agents/query/message).

Mirrors app/services/rag_chat.run_rag_turn exactly — same scoping, context,
and dual history model — for the Query Agent instead of the RAG Agent:

  1. SCOPING — the ChatSession for this turn is resolved via
     app/services/chat_history with mode="query". A conversation belongs to
     exactly one (user_id, project_id); it is tagged mode="query" so the
     Search-tab history sidebar never lists it.

  2. CONTEXT — the Query tools need the caller's real user_id/project_id, and
     that must never be an LLM-supplied argument. Set on
     app/services/query_context around the run, reset always.

  3. HISTORY — Agno keeps its own in-context history keyed by the namespaced
     agno session id; we ALSO mirror every turn into our own chat_messages.

The agno session id is `query-<canonical uuid>` — namespaced so it can never
collide with a rag/drafting/scanner session that reuses the same uuid.
"""

import uuid

from agno.run.base import RunStatus

from app.agents.query_agent import query_agent
from app.database import SessionLocal
from app.services.agent_retry import run_agent_resilient
from app.services.chat_history import append_message, resolve_chat_session
from app.services.query_context import reset_query_context, set_query_context

_TOOL_NAMES = (
    "get_document_info",
    "get_version_history",
    "who_can_approve",
    "list_pending_approvals",
    "check_my_access",
    "get_project_structure",
    "get_my_accessible_stages",
    "get_stage_requirements",
    "get_stage_document_status",
)


class QueryTurnError(Exception):
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
    return f"query-{canonical}"


def run_query_turn(
    *, user_id: uuid.UUID, project_id: uuid.UUID, session_id: str | None, message: str
) -> dict:
    """
    Run one Query turn.

    Returns:
        {"reply": str, "tools_called": list[str], "session_id": str}

    Raises:
        SessionScopeError — session_id belongs to a different user/project.
        QueryTurnError — the model/agent run failed (its error is not persisted).
    """
    db = SessionLocal()
    try:
        session = resolve_chat_session(
            db, session_id=session_id, user_id=user_id, project_id=project_id, mode="query"
        )
        canonical = session.session_id
        append_message(db, session_id=canonical, role="user", content=message)
        db.commit()  # the user's turn is recorded even if the agent call fails

        token = set_query_context(user_id=user_id, project_id=project_id)
        try:
            response = run_agent_resilient(
                query_agent,
                message or "continue",
                session_id=_agno_session_id(canonical),
                user_id=str(user_id),
            )
        finally:
            reset_query_context(token)

        if getattr(response, "status", None) == RunStatus.error:
            raise QueryTurnError(
                getattr(response, "content", "") or "agent run failed",
                session_id=str(canonical),
            )

        reply = getattr(response, "content", "") or ""
        tools = _tools_called(response)

        append_message(db, session_id=canonical, role="assistant", content=reply)
        db.commit()

        return {"reply": reply, "tools_called": tools, "session_id": str(canonical)}
    finally:
        db.close()
