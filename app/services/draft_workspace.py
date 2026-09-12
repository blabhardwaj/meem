"""
On-disk working copy of the in-progress chat draft.

The draft being edited in a /draft conversation lives in a session-scoped
working file — drafts/.wip/<session_id>.md — rewritten on every revision. That
file, NOT any in-memory variable, is the source of truth for "the current
draft": it is inspectable mid-conversation, and it is the exact bytes that get
scanned and saved when the user finalizes. The LLM never carries draft content
through a tool argument.

Concurrency: every function takes `session_id` EXPLICITLY. There is no
module-level "current session" — the CLI has one session, the HTTP server has
one per request, and two of them touching this module at the same time must
never see each other's working file.

Still fully decoupled from persistence (MERGE_DECISIONS §4): no DB, no ABAC,
no stage / team / project.
"""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.services.draft_export import DRAFTS_DIR, save_draft
from app.services.scan_score import ScoringError, score_document

WIP_DIR = DRAFTS_DIR / ".wip"
FINALIZED_DIR = DRAFTS_DIR / ".finalized"


class DraftNotFoundError(Exception):
    pass


class DraftPermissionError(Exception):
    pass

# Hidden marker embedded at the top of a chat-finalized file (see
# embed_scan_marker / check_scan_marker below) — lets the upload flow
# (app/services/document_upload_review.py) recognize a chat-drafted file
# re-uploaded UNCHANGED and skip re-running the (paid, LLM-backed) structural
# scan, reusing the score already computed when it was finalized here.
_SCAN_MARKER_RE = re.compile(
    r"^<!--\s*docflow-scan:\s*score=(\d+)/60;\s*content-hash=([0-9a-f]{64})\s*-->\n?",
    re.IGNORECASE,
)


def _wip_path(session_id: str) -> Path:
    if not session_id:
        raise ValueError("session_id is required")
    # Guard against a session_id that would escape the .wip directory.
    name = Path(str(session_id)).name
    if name != str(session_id) or not name:
        raise ValueError(f"Invalid session_id: {session_id!r}")
    return WIP_DIR / f"{name}.md"


def write_working_draft(session_id: str, content: str) -> str:
    """Overwrite the session's working file with the latest draft. Returns its path."""
    WIP_DIR.mkdir(parents=True, exist_ok=True)
    path = _wip_path(session_id)
    # write_bytes / read_bytes (not write_text): keep the draft's bytes exact
    # across the round-trip — no platform newline translation — so the
    # finalized file, the scanned content, and what the user was shown all match.
    path.write_bytes(content.encode("utf-8"))
    return str(path)


def read_working_draft(session_id: str) -> str | None:
    """Current draft text from disk for this session, or None if none exists yet."""
    path = _wip_path(session_id)
    return path.read_bytes().decode("utf-8") if path.exists() else None


def has_working_draft(session_id: str) -> bool:
    return _wip_path(session_id).exists()


def delete_working_draft(session_id: str) -> None:
    """Remove the session's working file, if it exists. Idempotent."""
    path = _wip_path(session_id)
    if path.exists():
        path.unlink()


def embed_scan_marker(content: str, score: int) -> str:
    """
    Prepends a hidden HTML-comment marker recording the score and a SHA-256
    of `content` (computed BEFORE the marker is added, i.e. of the content
    "below" it). Invisible in any rendered Markdown viewer.
    """
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return f"<!-- docflow-scan: score={score}/60; content-hash={content_hash} -->\n{content}"


def check_scan_marker(content: str) -> dict | None:
    """
    If `content` starts with a docflow-scan marker AND a fresh SHA-256 of the
    content below it matches the embedded hash (i.e. nothing was edited since
    it was finalized here), returns {"score": int, "content_without_marker":
    str}. Otherwise (no marker, or a hash mismatch meaning the content WAS
    edited after download) returns None — the caller should scan fresh.
    """
    match = _SCAN_MARKER_RE.match(content)
    if not match:
        return None
    score = int(match.group(1))
    embedded_hash = match.group(2)
    remainder = content[match.end():]
    if hashlib.sha256(remainder.encode("utf-8")).hexdigest() != embedded_hash:
        return None
    return {"score": score, "content_without_marker": remainder}


def finalize(session_id: str, *, user_id: uuid.UUID | str | None = None) -> dict:
    """
    Finalize this session's draft:
      1. read the working file (the real, final draft — nothing the LLM could
         have substituted),
      2. score it via score_document(),
      3. on a successful score, prepend a hidden scan-result marker (see
         embed_scan_marker) — lets a later re-upload of this exact file skip
         re-scanning (see check_scan_marker / document_upload_review.py),
      4. save it into drafts/ with draft_export's slug+timestamp name,
      5. record finalized metadata under drafts/.finalized/<draft_id>.json,
      6. delete the working file.

    Returns {"scan", "scan_error", "path", "content", "draft_id", "filename"} — "content"
    is the EXACT bytes written to disk (marker included when a score exists).
    """
    path = _wip_path(session_id)
    if not path.exists():
        raise RuntimeError("No working draft to finalize")
    content = path.read_bytes().decode("utf-8")

    scan: dict | None = None
    scan_error: str | None = None
    try:
        scan = score_document(content)
    except ScoringError as exc:
        scan_error = str(exc)
    except Exception as exc:  # network / rate-limit / etc — saving must not fail
        scan_error = f"{type(exc).__name__}: {exc}"

    content_to_save = embed_scan_marker(content, scan["overall_score"]) if scan else content
    final_path = save_draft(content_to_save)
    delete_working_draft(session_id)

    draft_id = str(uuid.uuid4())
    FINALIZED_DIR.mkdir(parents=True, exist_ok=True)
    filename = Path(final_path).name
    meta = {
        "draft_id": draft_id,
        "user_id": str(user_id) if user_id else None,
        "session_id": str(session_id),
        "filename": filename,
        "path": str(final_path),
        "score": scan["overall_score"] if scan else None,
        "scan": scan,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    meta_path = FINALIZED_DIR / f"{draft_id}.json"
    meta_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    return {
        "scan": scan,
        "scan_error": scan_error,
        "path": final_path,
        "content": content_to_save,
        "draft_id": draft_id,
        "filename": filename,
    }


def get_finalized_draft(draft_id: str, *, user_id: uuid.UUID | str) -> dict:
    """
    Look up a finalized draft by its opaque draft_id and verify that the acting user
    owns it. Returns a dict with {draft_id, user_id, filename, path, score, content_bytes, content_str}.
    Raises DraftNotFoundError if missing/invalid, or DraftPermissionError if owned by another user.
    """
    if not draft_id:
        raise DraftNotFoundError("draft_id is required")
    safe_name = Path(str(draft_id)).name
    if safe_name != str(draft_id) or not safe_name:
        raise DraftNotFoundError(f"Invalid draft_id: {draft_id!r}")

    meta_path = FINALIZED_DIR / f"{safe_name}.json"
    if not meta_path.is_file():
        raise DraftNotFoundError(f"Finalized draft {draft_id} not found")

    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise DraftNotFoundError(f"Could not read finalized draft metadata: {exc}") from exc

    # Enforce ownership: user_id must match if set
    expected_user = str(user_id) if user_id else None
    if data.get("user_id") and expected_user and data["user_id"] != expected_user:
        raise DraftPermissionError("You do not have access to this draft")

    # Resolve actual draft file
    file_path = Path(data.get("path") or (DRAFTS_DIR / data["filename"]))
    if not file_path.is_file():
        raise DraftNotFoundError(f"Draft file {data.get('filename')} not found on disk")

    file_bytes = file_path.read_bytes()
    data["content_bytes"] = file_bytes
    data["content_str"] = file_bytes.decode("utf-8")
    return data
