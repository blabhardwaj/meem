"""
Master Plan v2, item 12: a real content diff between a re-uploaded document's
new content and the immediately previous finalized version — factual only
(what changed), no automated "is this better/worse" judgment (explicitly out
of scope per DocFlow_AI_Version_Check_Spec.md §3).
"""

import difflib


def diff_summary(previous_content: str, new_content: str) -> dict:
    """
    Line-level diff between two Markdown documents.

    Returns:
        {
            "added_lines": int, "removed_lines": int,
            "has_changes": bool,
            "unified_diff": str,   # readable unified-diff text block
        }
    """
    previous_lines = previous_content.splitlines(keepends=True)
    new_lines = new_content.splitlines(keepends=True)

    diff_lines = list(difflib.unified_diff(
        previous_lines, new_lines,
        fromfile="previous version", tofile="new version",
        lineterm="",
    ))

    added = sum(1 for line in diff_lines if line.startswith("+") and not line.startswith("+++"))
    removed = sum(1 for line in diff_lines if line.startswith("-") and not line.startswith("---"))

    return {
        "added_lines": added,
        "removed_lines": removed,
        "has_changes": added > 0 or removed > 0,
        "unified_diff": "\n".join(diff_lines),
    }
