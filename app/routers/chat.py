"""
First-party chat-history surface for the frontend.

The chat agents (currently only the RAG Agent — see app/services/rag_chat.py)
mirror every turn into our own `chat_sessions` / `chat_messages` tables. This
router exposes that record so the Chat Interface panel can:

  - list a user's past conversations for a project (the history sidebar), and
  - reload the messages of a conversation after a page refresh.

Everything here is READ-ONLY except DELETE. New sessions and new messages are
only ever created by the agent runners (run_rag_turn), never by this router —
the frontend starts a new conversation simply by sending with no session_id.

SCOPING: a ChatSession belongs to exactly one (user_id, project_id) — the same
boundary app/services/chat_history.resolve_chat_session enforces. Here every
lookup is filtered by `identity.user_id`; a session that isn't the caller's is
a 404, never someone else's history.

The `mode` query param filters the listing to one chat agent's conversations
('search'/'rag' -> the RAG Agent, 'query' -> the Query Agent). Both agents
persist through these same tables; the Search-tab sidebar passes mode='rag'
so Query-tab conversations never show up in it, and vice versa.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.database import get_db
from app.models.chat import ChatMessage, ChatSession
from app.services.auth import ResolvedIdentity

router = APIRouter(prefix="/chat", tags=["chat"])

_TITLE_MAX = 80

# Frontend tab id -> stored ChatSession.mode. The Search tab historically sent
# "search"; the stored discriminator is "rag".
_MODE_ALIASES = {"search": "rag", "rag": "rag", "query": "query", "draft": "draft"}


class ChatSessionOut(BaseModel):
    session_id: str
    title: str
    started_at: str


class ChatMessageOut(BaseModel):
    message_id: str
    role: str
    content: str
    created_at: str
    sources: list = []


def _owned_session(db: Session, session_id: str, user_id: uuid.UUID) -> ChatSession:
    try:
        sid = uuid.UUID(session_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="Chat session not found")
    session = db.get(ChatSession, sid)
    if session is None or session.user_id != user_id:
        # Never distinguish "not yours" from "doesn't exist".
        raise HTTPException(status_code=404, detail="Chat session not found")
    return session


@router.get("/sessions", response_model=list[ChatSessionOut])
def list_sessions(
    project_id: uuid.UUID,
    mode: str | None = Query(default=None, max_length=40),
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    conditions = [
        ChatSession.user_id == identity.user_id,
        ChatSession.project_id == project_id,
    ]
    resolved_mode = _MODE_ALIASES.get((mode or "").lower()) if mode else None
    if resolved_mode is not None:
        conditions.append(ChatSession.mode == resolved_mode)

    sessions = (
        db.execute(
            select(ChatSession)
            .where(*conditions)
            .order_by(ChatSession.started_at.desc())
        )
        .scalars()
        .all()
    )
    if not sessions:
        return []

    # First user message per session -> the sidebar title.
    first_user: dict[uuid.UUID, str] = {}
    rows = (
        db.execute(
            select(ChatMessage.session_id, ChatMessage.content, ChatMessage.created_at)
            .where(
                ChatMessage.session_id.in_([s.session_id for s in sessions]),
                ChatMessage.role == "user",
            )
            .order_by(ChatMessage.created_at.asc())
        )
        .all()
    )
    for sid, content, _created in rows:
        if sid not in first_user and (content or "").strip():
            first_user[sid] = content.strip()

    out: list[ChatSessionOut] = []
    for s in sessions:
        title = first_user.get(s.session_id, "New conversation")
        if len(title) > _TITLE_MAX:
            title = title[: _TITLE_MAX - 1].rstrip() + "…"
        out.append(
            ChatSessionOut(
                session_id=str(s.session_id),
                title=title,
                started_at=s.started_at.isoformat(),
            )
        )
    return out


@router.get("/sessions/{session_id}/messages", response_model=list[ChatMessageOut])
def list_messages(
    session_id: str,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _owned_session(db, session_id, identity.user_id)
    messages = (
        db.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session.session_id)
            .order_by(ChatMessage.created_at.asc())
        )
        .scalars()
        .all()
    )
    return [
        ChatMessageOut(
            message_id=str(m.message_id),
            role=m.role,
            content=m.content,
            created_at=m.created_at.isoformat(),
            sources=[],
        )
        for m in messages
    ]


@router.delete("/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: str,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    session = _owned_session(db, session_id, identity.user_id)
    sid = session.session_id

    db.execute(delete(ChatMessage).where(ChatMessage.session_id == sid))
    db.execute(delete(ChatSession).where(ChatSession.session_id == sid))

    # Best-effort: drop the Agno-side mirror for this conversation too. Keyed
    # `<mode>-<canonical>` (see app/services/rag_chat._agno_session_id and
    # app/services/query_chat._agno_session_id) — 'query' sessions use the
    # 'query-' prefix, everything else the historical 'rag-' one.
    try:
        agno_prefix = "query" if session.mode == "query" else ("draft" if session.mode == "draft" else "rag")
        agno_sid = f"{agno_prefix}-{sid}"
        db.execute(
            text("DELETE FROM ai.agent_sessions_runs WHERE session_id = :s"),
            {"s": agno_sid},
        )
        db.execute(
            text("DELETE FROM ai.agent_sessions WHERE session_id = :s"),
            {"s": agno_sid},
        )
        if session.mode == "draft":
            from app.services.draft_workspace import delete_working_draft
            delete_working_draft(str(sid))
    except Exception:  # noqa: BLE001 — Agno schema is not our contract; never fail the delete on it
        pass

    db.commit()
    return None
