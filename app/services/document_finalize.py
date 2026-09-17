"""
Finalizing a document-review chat session (upload -> auto-scan -> revise in
chat -> finalize), distinct in meaning from Phase 4's chat-drafting finalize:
this writes a NEW DocumentVersion on the SAME Document row (never a new
Document — single-stage-per-upload; cross-stage relevance is handled by
stage_references, not multi-stage uploads).

Status on the new version is Scanner-driven ONLY, independent of any
approval policy: `indexed` if the scan passed AND nothing was flagged,
`needs_attention` otherwise (a failed structural score and a flagged
injection scan both route here — same human-review surface). Whether an
`indexed` version is actually ready to be indexed (which may ALSO require
human approval, on a requires_approval stage) is should_index()'s job —
see app/services/indexing.py — called at the end of this function.

run_full_scan() is shared by the upload-time auto-scan
(document_upload_review.py) and this finalize step: score_document, then
(mirroring the Scanner Agent's own documented sequence) reform_document if
the score is below threshold, then scan_for_injection — always, regardless
of score, since it's a cheap deterministic gate, not an expensive rescan
being guarded against.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import (
    Document,
    DocumentScan,
    DocumentStatus,
    DocumentVersion,
    ScanReviewStatus,
)
from app.services import draft_workspace
from app.services.access_control import is_grant_only_confidential_access
from app.services.audit import record_audit
from app.services.indexing import index_document, should_index
from app.services.injection_scan import scan_for_injection
from app.services.scan_prompts import REFORMATION_THRESHOLD
from app.services.scan_reformer import ReformationError, reform_document
from app.services.workflow import promote_version_on_approval
from app.services.scan_score import PER_CRITERION_MINIMUM, ScoringError, score_document


class DocumentNotFoundError(Exception):
    pass


class NoWorkingDraftError(Exception):
    pass


class GrantOnlyAccessError(Exception):
    """
    The caller's access to this document comes only from a read-only
    confidential-access grant (not native role-based access) — a grant
    never confers write/finalize capability, however the document was
    originally reached.
    """


def failed_criteria(criteria: list[dict] | None) -> list[str]:
    """
    Names of criteria scoring below PER_CRITERION_MINIMUM (Master Plan v2,
    item 6), or [] if there's nothing to check. Takes the raw criteria list
    directly (not the whole scan_outcome dict) so it works equally on a live
    score_document() result and on a persisted DocumentScan.criteria column.
    """
    if not criteria:
        return []
    return [c["name"] for c in criteria if c["score"] < PER_CRITERION_MINIMUM]


def _meets_quality_bar(scan: dict | None) -> bool:
    """
    The structural-quality half of passing, independent of injection: overall
    score at/above REFORMATION_THRESHOLD AND every criterion at/above
    PER_CRITERION_MINIMUM. Previously only the overall sum was checked, so a
    document scoring 0/20 on one axis and 18-20/20 on the others could still
    pass (e.g. 0+18+18=36, the old 60% threshold) — a single-axis floor
    closes that regardless of how high the other criteria score.
    """
    return (
        scan is not None
        and scan["overall_score"] >= REFORMATION_THRESHOLD
        and not failed_criteria(scan.get("criteria"))
    )


def run_full_scan(content: str) -> dict:
    """
    Score + (if it doesn't meet the quality bar) reform + injection-scan.

    Returns:
        {
            "scan": dict | None,           # score_document() result
            "scan_error": str | None,
            "reformed_content": str | None,
            "injection": {"flagged": bool, "findings": [...]},
        }
    """
    result: dict = {"scan": None, "scan_error": None, "reformed_content": None, "injection": None}

    try:
        result["scan"] = score_document(content)
    except ScoringError as exc:
        result["scan_error"] = str(exc)
    except Exception as exc:  # network / rate-limit / etc — must not raise
        result["scan_error"] = f"{type(exc).__name__}: {exc}"

    # Reform on either failure mode — overall below threshold, OR a single
    # criterion below its floor even if the overall sum would have passed.
    if result["scan"] is not None and not _meets_quality_bar(result["scan"]):
        try:
            result["reformed_content"] = reform_document(content, result["scan"])
        except ReformationError as exc:
            reform_err = f"reform failed: {exc}"
            result["scan_error"] = (
                f"{result['scan_error']}; {reform_err}" if result["scan_error"] else reform_err
            )

    result["injection"] = scan_for_injection(content)
    return result


def scan_passed(scan_outcome: dict) -> bool:
    """A version may become `indexed` only if it meets the quality bar AND was not flagged."""
    return (
        _meets_quality_bar(scan_outcome["scan"])
        and not scan_outcome["injection"]["flagged"]
    )


def _record_scan(db: Session, *, version_id: uuid.UUID, scan_outcome: dict) -> None:
    """Persists a DocumentScan row for this version, if a score exists (a total
    scoring failure — e.g. Groq unreachable — leaves nothing to record)."""
    if scan_outcome["scan"] is None:
        return
    passed = scan_passed(scan_outcome)
    db.add(DocumentScan(
        version_id=version_id,
        overall_score=scan_outcome["scan"]["overall_score"],
        criteria=scan_outcome["scan"]["criteria"],
        reform_triggered=scan_outcome["reformed_content"] is not None,
        reformed_content=scan_outcome["reformed_content"],
        review_status=ScanReviewStatus.not_required if passed else ScanReviewStatus.pending,
        injection_flagged=scan_outcome["injection"]["flagged"],
        injection_findings=scan_outcome["injection"]["findings"],
    ))


def finalize_document_revision(db: Session, *, document_id: uuid.UUID, user_id: uuid.UUID, session_id: str) -> dict:
    """
    Reads the review session's current working-draft content (the
    drafting_agent's revisions, same draft_workspace machinery as Phase 4),
    runs the full scan, and writes it as a new DocumentVersion on
    `document_id`. Deletes the working file afterward either way.

    Returns:
        {
            "version_id": str, "version_number": int, "status": str,
            "scan": dict | None, "scan_error": str | None,
            "reformed_content": str | None,
            "injection_flagged": bool, "injection_findings": list,
            "should_index": bool, "failed_criteria": list[str],
        }

    Raises:
        DocumentNotFoundError, NoWorkingDraftError
    """
    # Lock the Document row for the rest of this transaction so two
    # concurrent finalizes on the same document can't race setting
    # current_version_id to two different new versions (Master Plan v2,
    # item 12 — this gap predates this function; closing it here since
    # item 12's revision loop is the first caller likely to run this
    # finalize path repeatedly in quick succession within one session).
    document = db.execute(
        select(Document).where(Document.document_id == document_id).with_for_update()
    ).scalar_one_or_none()
    if document is None:
        raise DocumentNotFoundError(f"Unknown document: {document_id}")

    if is_grant_only_confidential_access(db, user_id, document):
        raise GrantOnlyAccessError(
            "Your access to this document is read-only (granted via a confidential-access "
            "request, not your team role) — you cannot finalize a revision."
        )

    content = draft_workspace.read_working_draft(session_id)
    if content is None:
        raise NoWorkingDraftError("No working draft to finalize")

    scan_outcome = run_full_scan(content)
    passed = scan_passed(scan_outcome)

    version_id = uuid.uuid4()
    content_bytes = content.encode("utf-8")
    version = DocumentVersion(
        version_id=version_id,
        document_id=document_id,
        # No version_number here — unassigned until approve_document()
        # (or the auto-approve path) promotes this version, per
        # DocumentVersion's docstring.
        file_data=content_bytes,
        file_size_bytes=len(content_bytes),
        uploaded_by=user_id,
        # Scanner-driven only — independent of any approval policy. A failed
        # structural score and a flagged injection scan both land here
        # (needs_attention), never pending_review: finalize is a definitive
        # decision point, not an "awaiting first look" state like upload is.
        status=DocumentStatus.indexed if passed else DocumentStatus.needs_attention,
    )
    db.add(version)
    db.flush()
    document.current_version_id = version_id

    _record_scan(db, version_id=version_id, scan_outcome=scan_outcome)

    record_audit(
        db, actor_id=user_id, action="FINALIZE_DOCUMENT_REVISION", resource_type="document",
        resource_id=document_id,
        details={
            "version_id": str(version_id),
            "status": version.status.value,
            "overall_score": scan_outcome["scan"]["overall_score"] if scan_outcome["scan"] else None,
            "injection_flagged": scan_outcome["injection"]["flagged"],
        },
    )
    db.commit()

    draft_workspace.delete_working_draft(session_id)

    # Indexing trigger (app/services/indexing.py): combines this version's
    # just-set Scanner status with the stage's approval policy (if any). On
    # a requires_approval stage, an `indexed` version still needs a human
    # approve_document() call before this returns True — see that function's
    # own should_index() call for the other half of this trigger. On a stage
    # with NO approval requirement, a passing scan alone is enough and
    # approve_document() is never called at all for this document — so THIS
    # is the only place that ever promotes such a version (assigns its
    # version_number, marks it approved) for that case.
    ready_to_index = should_index(db, document_id)
    if ready_to_index:
        promote_version_on_approval(db, document_id=document_id, version_id=version_id)
        db.commit()
        index_document(db, document_id)

    return {
        "version_id": str(version_id),
        "version_number": version.version_number,
        "status": version.status.value,
        "scan": scan_outcome["scan"],
        "scan_error": scan_outcome["scan_error"],
        "reformed_content": scan_outcome["reformed_content"],
        "injection_flagged": scan_outcome["injection"]["flagged"],
        "injection_findings": scan_outcome["injection"]["findings"],
        "should_index": ready_to_index,
        "failed_criteria": failed_criteria(scan_outcome["scan"].get("criteria") if scan_outcome["scan"] else None),
    }
