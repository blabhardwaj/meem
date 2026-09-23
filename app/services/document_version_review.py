"""
Master Plan v2, item 12 — the version diff/review gate.

Per DocFlow_AI_Version_Check_Spec.md: re-uploading an already-existing
document never silently replaces its current version. Instead: parse the new
file, diff it against the current version's content, run the initial scan,
seed a session-scoped working file (same draft_workspace machinery as the
first-upload review chat and Phase 4 chat-drafting) with the new content, and
write the diff's anchor (the untouched previous-version content) alongside it
so every later revision's diff stays anchored to the true previous version,
never to the just-edited state.

Independent of the human-approval WorkflowState gate (Section 8 of the spec)
— this fires on every re-upload regardless of the target stage's
requires_approval setting.
"""

import uuid

from agno.run.base import RunStatus
from sqlalchemy.orm import Session

from app.agents.revision_agent import revision_agent
from app.models.document import Document, DocumentVersion
from app.services import draft_workspace
from app.services.access_control import can_edit_document
from app.services.ai_usage import check_and_consume_ai_usage
from app.services.document_diff import diff_summary
from app.services.document_finalize import failed_criteria, run_full_scan, scan_passed
from app.services.document_finalize import finalize_document_revision
from app.services.document_parser import parse_document_to_markdown


class DocumentNotFoundError(Exception):
    pass


class PermissionDeniedError(Exception):
    pass


class NoChangesError(Exception):
    """
    UI_FIXES_2026-09-15.md: a re-uploaded file was byte-for-byte identical
    (after markdown parsing) to the current version's content. Refuse to
    seed a review session for it — finalizing would write a real new
    DocumentVersion row with an independently re-run (LLM-based, not
    perfectly deterministic) scan, which could land a different
    indexed/needs_attention status than the current version despite having
    identical content. That looked like a data bug (two versions, same
    content, different status) but was actually this code never checking
    for a no-op upload in the first place.
    """


def _current_version_content(db: Session, document: Document) -> str:
    version = db.get(DocumentVersion, document.current_version_id)
    content = version.file_data
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="replace")
    return content


def start_version_review(
    db: Session,
    *,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    original_filename: str,
    mime_type: str,
    file_data: bytes,
    session_id: str,
) -> dict:
    """
    Parses a re-uploaded file, diffs it against the document's current
    version, runs the initial scan, and seeds a new review session (both the
    mutable working draft and the write-once diff anchor).

    Raises:
        DocumentNotFoundError, PermissionDeniedError,
        UnsupportedDocumentTypeError, DocumentParseError (from document_parser)

    Returns:
        {
            "diff": dict (see document_diff.diff_summary),
            "scan": dict | None, "scan_error": str | None,
            "reformed_content": str | None,
            "injection_flagged": bool, "injection_findings": list,
            "failed_criteria": list[str],
        }
    """
    document = db.get(Document, document_id)
    if document is None or document.current_version_id is None:
        raise DocumentNotFoundError(f"Document {document_id} has no current version")

    if not can_edit_document(db, user_id, document):
        raise PermissionDeniedError(
            "Only the document's uploader, a team lead, or a project/org admin "
            "can upload a new version of this document."
        )

    draft_workspace.cleanup_stale_sessions()

    previous_content = _current_version_content(db, document)
    new_content = parse_document_to_markdown(file_data, mime_type, original_filename)

    diff = diff_summary(previous_content, new_content)
    if not diff["has_changes"]:
        raise NoChangesError(
            "This file is identical to the current version — nothing to review."
        )
    scan_outcome = run_full_scan(new_content)
    passed = scan_passed(scan_outcome)

    draft_workspace.write_anchor_content(session_id, previous_content)
    draft_workspace.write_working_draft(session_id, new_content)

    return {
        "content": new_content,
        "diff": diff,
        "scan": scan_outcome["scan"],
        "scan_error": scan_outcome["scan_error"],
        "reformed_content": scan_outcome["reformed_content"],
        "injection_flagged": scan_outcome["injection"]["flagged"],
        "injection_findings": scan_outcome["injection"]["findings"],
        "scan_passed": passed,
        "failed_criteria": failed_criteria(
            scan_outcome["scan"].get("criteria") if scan_outcome["scan"] else None
        ),
    }


def start_version_review_from_current(
    db: Session, *, document_id: uuid.UUID, user_id: uuid.UUID, session_id: str
) -> dict:
    """
    UI_FIXES_2026-09-15.md: "edit the current document via chat, no re-upload
    needed" — e.g. fixing a contradiction the audit engine flagged with a
    small wording change, without leaving the app to edit a file and upload
    it back. Seeds a review session directly from the current version's own
    content: no file, no initial diff (there's nothing to diff against yet —
    the anchor IS the current content), no initial scan (unlike
    start_version_review, nothing has changed yet to scan). The chat loop
    (revise_document / confirm_revision, via run_version_review_turn) and
    finalize (a real new DocumentVersion, scanned, on confirm) are identical
    to the re-upload path — same anchor/working-draft machinery, same
    finalize_document_revision.

    Raises:
        DocumentNotFoundError, PermissionDeniedError
    """
    document = db.get(Document, document_id)
    if document is None or document.current_version_id is None:
        raise DocumentNotFoundError(f"Document {document_id} has no current version")

    if not can_edit_document(db, user_id, document):
        raise PermissionDeniedError(
            "Only the document's uploader, a team lead, or a project/org admin "
            "can edit this document."
        )

    draft_workspace.cleanup_stale_sessions()

    current_content = _current_version_content(db, document)
    draft_workspace.write_anchor_content(session_id, current_content)
    draft_workspace.write_working_draft(session_id, current_content)

    # UI_FIXES_2026-09-15.md #31: matches the re-upload flow's own opener
    # (_version_diff_reply in document_review.py) — deterministic, built from
    # this document's real data, never an LLM call. The agent only starts
    # once the user sends their first real message (see
    # revision_agent.py's instructions, which assume this greeting already
    # happened) — same pattern as the diff-review opener.
    version = db.get(DocumentVersion, document.current_version_id)
    line_count = current_content.count("\n") + 1 if current_content else 0
    version_label = f"v{version.version_number}" if version.version_number is not None else "Draft"
    return {
        "content": current_content,
        "version_number": version.version_number,
        "reply": (
            f"Editing '{document.original_filename}' ({version_label}, "
            f"{line_count} lines) — you can see the full current content above. "
            "Describe the change you'd like, or say it looks good to finalize as-is."
        ),
    }


def _tool_called(response, name: str) -> bool:
    return bool(getattr(response, "tools", None)) and any(
        t.tool_name == name for t in response.tools
    )


def _build_context_prefix(session_id: str) -> str:
    current = draft_workspace.read_working_draft(session_id)
    if not current:
        return "[No document is loaded in this review session.]\n\n"
    return (
        "[The document currently under review — the exact working copy below is what is on "
        "disk right now.\n"
        "- If the user requests ANY edit, call revise_document(requested_change) with exactly "
        "what they described.\n"
        "- If the user confirms this version should be finalized, call confirm_revision.\n"
        "- If the user is just answering a question, reply conversationally.]\n\n"
        f"--- CURRENT DOCUMENT UNDER REVIEW ---\n{current}\n--- END ---\n\n"
    )


def run_version_review_turn(
    db: Session, *, session_id: str, document_id: uuid.UUID, user_id: uuid.UUID,
    tenant_id: uuid.UUID, message: str,
) -> dict:
    """
    One turn of the version-review conversation. On a revision, recomputes
    the diff against the session's write-once anchor (the ORIGINAL previous
    version) — never against the just-edited state. On finalize, delegates to
    the existing finalize_document_revision (same as the first-upload review
    chat) and additionally deletes this session's anchor file.

    Returns:
        {
          "reply": str, "revised": bool, "finalized": bool,
          "diff": dict | None,             # recomputed anchor-relative diff, revision turns only
          "content": str | None,           # working draft's current text (UI_FIXES_2026-09-15.md #31)
          "version_id": str | None, "version_number": int | None,
          "status": str | None,
          "scan": dict | None, "scan_error": str | None,
          "reformed_content": str | None,
          "injection_flagged": bool | None, "injection_findings": list | None,
          "should_index": bool | None, "failed_criteria": list[str],
          "workflow_reset": bool,          # True if a prior approval was reset to draft
        }
    """
    # Change 4 (PRODUCTION_READINESS_PLAN.md) — committed immediately so an
    # attempted call counts even if the agent run itself fails below.
    check_and_consume_ai_usage(db, tenant_id)
    db.commit()

    prefix = _build_context_prefix(session_id)
    before = draft_workspace.read_working_draft(session_id)
    response = revision_agent.run(prefix + (message or "continue"), session_id=session_id)

    # A failed LLM call (rate limit, network, etc.) must never fall through
    # to the confirm_revision/finalize check below: agno's RunResponse can
    # still carry stale .tools metadata from an earlier successful turn in
    # this same session on an error response, which previously let
    # _tool_called(response, "confirm_revision") return True on a turn that
    # never actually ran — silently finalizing whatever was on disk at that
    # moment (possibly stale/unrevised content) instead of surfacing the
    # failure. Mirrors the same check in app/services/search_chat.py.
    if getattr(response, "status", None) == RunStatus.error:
        raise RuntimeError(getattr(response, "content", "") or "revision agent run failed")

    after = draft_workspace.read_working_draft(session_id)
    reply = getattr(response, "content", "") or ""

    revised_this_turn = _tool_called(response, "revise_document") or (
        after is not None and after != before
    )

    base = {
        "reply": reply, "revised": False, "finalized": False, "diff": None,
        "content": after,  # the working draft's current text, for the UI to display live
        "version_id": None, "version_number": None, "status": None,
        "scan": None, "scan_error": None, "reformed_content": None,
        "injection_flagged": None, "injection_findings": None, "should_index": None,
        "failed_criteria": [], "workflow_reset": False,
    }

    if _tool_called(response, "confirm_revision"):
        if not draft_workspace.has_working_draft(session_id):
            base["reply"] = reply or "There is no document in this review session to finalize yet."
            return base
        outcome = finalize_document_revision(
            db, document_id=document_id, user_id=user_id, session_id=session_id
        )
        from app.services.workflow import reset_to_draft_if_approved
        workflow_reset = reset_to_draft_if_approved(
            db, document_id=document_id, triggered_by=user_id
        )
        draft_workspace.delete_anchor_content(session_id)
        base.update(
            finalized=True,
            version_id=outcome["version_id"],
            version_number=outcome["version_number"],
            status=outcome["status"],
            scan=outcome["scan"],
            scan_error=outcome["scan_error"],
            reformed_content=outcome["reformed_content"],
            injection_flagged=outcome["injection_flagged"],
            injection_findings=outcome["injection_findings"],
            should_index=outcome["should_index"],
            failed_criteria=outcome["failed_criteria"],
            workflow_reset=workflow_reset,
        )
        return base

    if revised_this_turn and after is not None:
        anchor = draft_workspace.read_anchor_content(session_id)
        if anchor is not None:
            base["diff"] = diff_summary(anchor, after)

    base["revised"] = revised_this_turn
    return base
