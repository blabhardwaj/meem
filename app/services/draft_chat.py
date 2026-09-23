"""
One turn of the /draft conversation, shared by the CLI (cli.py) and the HTTP
endpoint (app/routers/agents.py).

This is the exact flow cli.py's /draft handler used to run inline:
  - build a context prefix from the session's on-disk working draft,
  - run the Drafting Agent,
  - if it called draft_document, the working file was already rewritten (the
    tool does that) — report drafted=True,
  - if it called confirm_draft, finalize deterministically from disk (score via
    the Structure Scanner, move into drafts/, delete the working file),
  - otherwise it just replied (a clarifying question) — report neither.

Fully decoupled from persistence: no DB, no ABAC, no stage/team/project. The
finalized output is a local file, identical to the CLI.
"""

import logging
import uuid
from pathlib import Path

from agno.run.base import RunStatus
from sqlalchemy import select, text

from app.agents.drafting_agent import drafting_agent
from app.database import SessionLocal
from app.models.chat import ChatMessage, ChatSession
from app.services import draft_workspace
from app.services.chat_history import append_message, resolve_chat_session

logger = logging.getLogger(__name__)


def _tool_called(response, name: str) -> bool:
    return bool(getattr(response, "tools", None)) and any(
        t.tool_name == name for t in response.tools
    )


def _format_layout_block(layout: list[dict]) -> str:
    """
    Master Plan v2, item 14: a standard section layout (built-in, from
    draft_layouts.py, or extracted from an uploaded reference document via
    outline_extraction.py — both share the same {name, purpose} shape) to
    fold into user_input the first time draft_document is called this
    session. Only ever injected on the session's first real drafting turn
    (see run_draft_turn) — a layout describes the document's overall section
    structure, which doesn't fit the "requested change" contract a revision
    turn passes to draft_document instead.
    """
    lines = "\n".join(f"- {s['name']}: {s['purpose']}" for s in layout if s.get("name"))
    return (
        "[The user has selected a standard section layout for this document. When you call "
        "draft_document, pass this layout as part of user_input and instruct it to structure the "
        "document using these sections IN THIS ORDER, adapting section content to what the user "
        "actually describes — do not invent facts to fill a section the user said nothing about, "
        "but do keep the section headings themselves.\n"
        f"--- LAYOUT ---\n{lines}\n--- END LAYOUT ---]\n\n"
    )


def build_context_prefix(session_id: str, layout: list[dict] | None = None) -> str:
    """The per-turn context prefix — the working file on disk, verbatim."""
    current = draft_workspace.read_working_draft(session_id)
    if not current:
        base = "[No draft exists in this conversation yet.]\n\n"
        if layout:
            base += _format_layout_block(layout)
        return base

    return (
        "[A draft currently exists — the exact working copy below is what is on "
        "disk right now.\n"
        "- If the user requests ANY edit, call draft_document again with ONLY "
        "the requested change as user_input (e.g. \"Add a section on partial "
        "refunds\"). Do NOT repeat this draft's text back into user_input — it "
        "is read from disk and merged in for you automatically.\n"
        "- If the user confirms or approves this draft, call confirm_draft.\n"
        "- If the user is just answering a question, reply conversationally.]\n\n"
        f"--- CURRENT WORKING DRAFT ---\n{current}\n--- END CURRENT DRAFT ---\n\n"
    )


def run_draft_turn(
    session_id: str,
    message: str,
    *,
    user_id: uuid.UUID | str | None = None,
    project_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | str | None = None,
    layout: list[dict] | None = None,
    author_name: str | None = None,
) -> dict:
    """
    Run one drafting turn for `session_id`.

    Chat history (chat_sessions/chat_messages) is persisted only when
    `user_id`, `project_id`, AND `tenant_id` are all given — the CLI (cli.py)
    calls this with none of them, by design (no DB, no ABAC), and that stays
    fully decoupled from persistence. `tenant_id` is required, not optional
    for a caller that does pass user_id/project_id, because chat_sessions/
    chat_messages FORCE ROW LEVEL SECURITY keyed on it — omitting it doesn't
    raise, it just makes every write silently violate the RLS policy.

    Returns:
        {
          "reply": str,            # the agent's text
          "drafted": bool,         # draft_document was called this turn
          "finalized": bool,       # confirm_draft was called AND finalize ran
          "scan": dict | None,     # Structure Scanner result (finalize only)
          "scan_error": str | None,
          "final_content": str | None,  # exact finalized bytes
          "draft_content": str | None,  # exact current working draft content
          "path": str | None,      # local file path
          "filename": str | None,  # basename, for the download URL
          "draft_id": str | None,  # safe server-side reference to finalized artifact
          "session_id": str,       # canonical session id
        }
    """
    db = None
    chat_session = None
    canonical = session_id
    uid = None
    if user_id:
        try:
            uid = uuid.UUID(str(user_id))
        except (ValueError, TypeError):
            uid = None

    if uid and project_id and tenant_id:
        db = SessionLocal()
        # RLS on chat_sessions/chat_messages (FORCE ROW LEVEL SECURITY) is
        # keyed on this GUC via projects.tenant_id — every other internal
        # SessionLocal() in this codebase sets it before the first query.
        # Without it, every INSERT here silently violates the policy's
        # implicit WITH CHECK and previously vanished into the bare except
        # below with zero trace: chat history persistence looked like it
        # worked (the API still returned 200 with a reply) but nothing was
        # ever written, so GET /chat/sessions always came back empty.
        db.info["tenant_id"] = str(tenant_id)
        db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(tenant_id)})
        try:
            chat_session = resolve_chat_session(
                db, session_id=session_id, user_id=uid, project_id=project_id, mode="draft"
            )
            canonical = str(chat_session.session_id)
            append_message(db, session_id=chat_session.session_id, role="user", content=message)
            db.commit()

            # If no working draft on disk, check if there's draft markdown in past assistant messages to restore
            if not draft_workspace.has_working_draft(canonical):
                past_assistant_msgs = (
                    db.execute(
                        select(ChatMessage)
                        .where(ChatMessage.session_id == chat_session.session_id, ChatMessage.role == "assistant")
                        .order_by(ChatMessage.created_at.desc())
                    )
                    .scalars()
                    .all()
                )
                for pm in past_assistant_msgs:
                    if pm.content and "# " in pm.content:
                        idx = pm.content.find("# ")
                        draft_body = pm.content[idx:].strip()
                        draft_workspace.write_working_draft(canonical, draft_body)
                        break
        except Exception:
            logger.exception(
                "run_draft_turn: failed to persist chat history for session_id=%s", session_id
            )
            if db:
                db.rollback()

    try:
        prefix = build_context_prefix(canonical, layout=layout)
        before = draft_workspace.read_working_draft(canonical)
        response = drafting_agent.run(prefix + (message or "continue"), session_id=canonical)

        # A failed LLM call (rate limit, network, etc.) must never fall
        # through to the confirm_draft check below: agno's RunResponse can
        # still carry stale .tools metadata from an earlier successful turn
        # in this same session on an error response, which could otherwise
        # make _tool_called(response, "confirm_draft") return True on a turn
        # that never actually ran — silently finalizing (a real, permanent
        # file write) whatever draft is on disk at that moment instead of
        # surfacing the failure. Same class of bug found and fixed today in
        # app/services/document_version_review.py and document_review_chat.py.
        if getattr(response, "status", None) == RunStatus.error:
            raise RuntimeError(getattr(response, "content", "") or "drafting agent run failed")

        after = draft_workspace.read_working_draft(canonical)
        reply = getattr(response, "content", "") or ""

        # "drafted this turn" = draft_document ran. On rate-limit-heavy turns the
        # tool list on the RunOutput can be incomplete, so also treat a changed
        # working file as proof the tool ran.
        drafted_this_turn = _tool_called(response, "draft_document") or (
            after is not None and after != before
        )

        # The LLM is instructed to leave a "[Author Name]"/"[Date]" placeholder
        # for a sign-off line rather than invent one — it does not reliably
        # replace it on a later edit turn either (see draft_workspace docs).
        # Resolve it deterministically against the authenticated caller right
        # after the working file is rewritten, so the user never has to ask
        # the agent to fill in their own name.
        if drafted_this_turn and after is not None and author_name:
            resolved = draft_workspace.apply_author_placeholder(after, author_name)
            if resolved != after:
                draft_workspace.write_working_draft(canonical, resolved)
                after = resolved

        base = {
            "reply": reply,
            "drafted": False,
            "finalized": False,
            "scan": None,
            "scan_error": None,
            "final_content": None,
            "draft_content": after,
            "path": None,
            "filename": None,
            "draft_id": None,
            "session_id": canonical,
        }

        if _tool_called(response, "confirm_draft"):
            if not draft_workspace.has_working_draft(canonical):
                base["scan_error"] = "There is no draft in this conversation to finalize yet."
            else:
                outcome = draft_workspace.finalize(canonical, user_id=uid, author_name=author_name)
                base.update(
                    finalized=True,
                    scan=outcome["scan"],
                    scan_error=outcome["scan_error"],
                    final_content=outcome["content"],
                    draft_content=outcome["content"],
                    path=outcome["path"],
                    filename=Path(outcome["path"]).name,
                    draft_id=outcome.get("draft_id"),
                )

        base["drafted"] = drafted_this_turn

        if db and chat_session:
            try:
                assistant_content = reply
                if base.get("final_content"):
                    final_header = f"Draft finalized as `{base['filename']}`"
                    if base.get("scan") and "overall_score" in base["scan"]:
                        final_header += f" (Score: {base['scan']['overall_score']}/60)"
                    final_header += ".\n\n"
                    if base["final_content"] not in assistant_content:
                        assistant_content = f"{final_header}{base['final_content']}".strip()
                    elif not assistant_content.startswith("Draft finalized"):
                        assistant_content = f"{final_header}{assistant_content}".strip()
                elif base.get("draft_content") and base["draft_content"] not in assistant_content:
                    assistant_content = f"{assistant_content}\n\n{base['draft_content']}".strip() if assistant_content else base["draft_content"]

                append_message(db, session_id=chat_session.session_id, role="assistant", content=assistant_content or "Draft updated.")
                db.commit()
            except Exception:
                logger.exception(
                    "run_draft_turn: failed to persist assistant reply for session_id=%s", session_id
                )
                db.rollback()

        return base
    finally:
        if db:
            db.close()
