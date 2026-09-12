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

import uuid
from pathlib import Path

from sqlalchemy import select

from app.agents.drafting_agent import drafting_agent
from app.database import SessionLocal
from app.models.chat import ChatMessage, ChatSession
from app.services import draft_workspace
from app.services.chat_history import append_message, resolve_chat_session


def _tool_called(response, name: str) -> bool:
    return bool(getattr(response, "tools", None)) and any(
        t.tool_name == name for t in response.tools
    )


def build_context_prefix(session_id: str) -> str:
    """The per-turn context prefix — the working file on disk, verbatim."""
    current = draft_workspace.read_working_draft(session_id)
    if not current:
        return "[No draft exists in this conversation yet.]\n\n"

    return (
        "[A draft currently exists — the exact working copy below is what is on "
        "disk right now.\n"
        "- If the user requests ANY edit, pass this FULL text back into "
        "draft_document's user_input with only the requested change applied — "
        "preserve all other sections, headings, formatting, and wording.\n"
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
) -> dict:
    """
    Run one drafting turn for `session_id`.

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

    if uid and project_id:
        db = SessionLocal()
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
            if db:
                db.rollback()

    try:
        prefix = build_context_prefix(canonical)
        before = draft_workspace.read_working_draft(canonical)
        response = drafting_agent.run(prefix + (message or "continue"), session_id=canonical)
        after = draft_workspace.read_working_draft(canonical)
        reply = getattr(response, "content", "") or ""

        # "drafted this turn" = draft_document ran. On rate-limit-heavy turns the
        # tool list on the RunOutput can be incomplete, so also treat a changed
        # working file as proof the tool ran.
        drafted_this_turn = _tool_called(response, "draft_document") or (
            after is not None and after != before
        )

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
                outcome = draft_workspace.finalize(canonical, user_id=uid)
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
                db.rollback()

        return base
    finally:
        if db:
            db.close()
