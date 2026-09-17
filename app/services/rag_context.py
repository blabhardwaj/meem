"""
Per-turn context for the RAG Agent — WHO is asking, and within WHICH project.

Same principle as app/services/session_context.py: the acting user_id and
project_id are established by the caller (CLI startup, or the authenticated
HTTP endpoint) and read internally by the RAG tools. They are NEVER tool
arguments the LLM supplies, so a crafted chat message can't make the agent
retrieve, summarise, or request access as a different user or in another
project.

Unlike session_context (a plain module dict, set once at CLI startup), this
is a ContextVar set/reset around a single agent turn — run_rag_turn() in
app/services/rag_chat.py owns that lifecycle — so concurrent HTTP requests
never see each other's identity.
"""

import contextvars
import uuid
from dataclasses import dataclass, field


@dataclass
class RagContext:
    user_id: uuid.UUID
    project_id: uuid.UUID
    # The tenant to SET LOCAL app.current_tenant_id to on the RAG tools' own
    # SessionLocal() sessions (see app/tools/rag_tools.py's _run_search). Optional
    # only because the legacy, currently-unreachable run_rag_turn() (rag_chat.py)
    # doesn't have it in scope to pass — every LIVE caller (run_search_turn,
    # search_chat.py) always provides it. When None, callers fall back to an
    # unscoped User lookup (safe: `users` has RLS enabled but not FORCED) to
    # derive it, rather than running fully unscoped against FORCE-RLS tables.
    tenant_id: uuid.UUID | None = None
    telemetry: dict = field(default_factory=dict)


_ctx: contextvars.ContextVar[RagContext | None] = contextvars.ContextVar(
    "rag_agent_context", default=None
)


def set_rag_context(
    *, user_id: uuid.UUID, project_id: uuid.UUID, tenant_id: uuid.UUID | None = None
) -> contextvars.Token:
    return _ctx.set(RagContext(user_id=user_id, project_id=project_id, tenant_id=tenant_id, telemetry={}))


def reset_rag_context(token: contextvars.Token) -> None:
    _ctx.reset(token)


def get_rag_context() -> RagContext:
    ctx = _ctx.get()
    if ctx is None:
        raise RuntimeError(
            "No RAG context set — a RAG tool was called outside run_rag_turn()."
        )
    return ctx
