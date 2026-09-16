"""
Tools for the Version Revision Agent (Master Plan v2, item 12 — the
diff-review gate on a document re-upload). Same conventions as
app/tools/draft_tools.py: the working file on disk is the single source of
truth, the LLM never carries document content through a tool argument, and
confirm_draft is a pure signal.

revise_document differs from draft_tools.draft_document in framing only: it
applies ONE described change to the document's CURRENT full content (read
fresh from disk each call) rather than drafting from a type + description.
"""

from agno.run import RunContext
from agno.tools import tool

from app.services.revision_generator import revise_document as _revise_document
from app.services.draft_workspace import read_working_draft, write_working_draft


@tool
def revise_document(requested_change: str, run_context: RunContext) -> str:
    """
    Applies ONE described change to the document currently under review,
    preserving everything else. Use this whenever the user asks for an edit —
    NOT for drafting a new document from scratch.

    Args:
        requested_change: free-form text describing exactly what to change
    """
    current = read_working_draft(run_context.session_id)
    if current is None:
        return "There is no document loaded in this review session to revise."
    result = _revise_document(current, requested_change)
    write_working_draft(run_context.session_id, result)
    return result


@tool(stop_after_tool_call=True)
def confirm_revision(confirmed: bool = True) -> str:
    """
    Signal that the user has EXPLICITLY confirmed the current revised content
    should be finalized as the document's new version. Pass confirmed=true.
    Do NOT pass document content — finalizing reads the current content
    straight from disk. Do NOT call this for edit requests, questions, or
    vague / ambiguous replies.
    """
    return "confirmed" if confirmed else "not_confirmed"
