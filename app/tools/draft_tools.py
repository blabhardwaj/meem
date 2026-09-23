"""
Tools for the Drafting Agent — thin @tool-decorated wrappers around
app/services/*. No business logic lives here.

Phase 4 pivot (MERGE_DECISIONS §4 "Drafting agent — decoupled from
persistence"): drafting NEVER touches the database, ABAC, or a
stage/team/project. There is no doc_type/stage extraction gate and no
chat-triggered upload.

The current draft is kept in a session-scoped working file on disk
(drafts/.wip/<session_id>.md) — see app/services/draft_workspace.py. draft_document
writes to it on every result; confirm_draft finalizes whatever that file
currently holds. The LLM never carries draft content through a tool argument.
"""

from agno.run import RunContext
from agno.tools import tool

from app.services.draft_generator import draft_document as _draft_document
from app.services.draft_generator import revise_document as _revise_document
from app.services.draft_workspace import read_working_draft, write_working_draft


@tool
def draft_document(document_type: str, user_input: str, run_context: RunContext) -> str:
    """
    Drafts a new document, or revises the session's existing one, from a
    document type and a description.

    Args:
        document_type: e.g. "Test Plan", "Design Doc", "Requirements Spec" etc.
        user_input: For a NEW document (no draft exists yet this session):
                    free-form text describing everything the document should
                    cover — bullets, paragraphs, or a mix.
                    For a REVISION (a draft already exists this session):
                    ONLY the requested change (e.g. "Add a section on partial
                    refunds," or "Swap sections 11 and 12"). Never repeat the
                    existing draft content here — it is read from disk
                    automatically and merged in for you.
    """
    # `run_context` is injected by agno (hidden from the model). We use its
    # session_id so each conversation reads/writes only its own working file.
    current = read_working_draft(run_context.session_id)
    if current:
        result = _revise_document(document_type, current, user_input)
    else:
        result = _draft_document(document_type, user_input)
    write_working_draft(run_context.session_id, result)
    return result


@tool(stop_after_tool_call=True)
def confirm_draft(confirmed: bool = True) -> str:
    """
    Signal that the user has EXPLICITLY confirmed they are satisfied with the
    current draft and want it finalized. Pass confirmed=true. Do NOT pass the
    draft content — finalizing reads the current draft straight from disk. Do
    NOT call this for edit requests, questions, or vague / ambiguous replies.

    This is only a signal. After it, the system runs the structural quality
    Scanner on the on-disk draft and saves it to a local Markdown file under
    drafts/, then shows the user the score and the file location. Nothing is
    uploaded to any project, stage, or team — that is a separate step.
    """
    return "confirmed" if confirmed else "not_confirmed"
