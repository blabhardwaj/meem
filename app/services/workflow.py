"""
Document approval workflow (MERGE_DECISIONS §3/4 crossover).

A document has a WorkflowState row ONLY if its stage has
requires_approval=True — created by create_document(). Absence of a row means
"no human sign-off needed for this document"; every function here raises
WorkflowError in that case.

Lifecycle:
    draft     --submit-->  pending_review  --approve-->  approved
    rejected  --submit-->  pending_review  --reject -->  rejected

A rejected document can be resubmitted (its stale rejection_reason is cleared);
an approved document is terminal.

This is purely human sign-off — an independent axis from the Structure/
Injection Scanner (app/services/document_finalize.py), which sets
DocumentVersion.status on every real upload/finalize regardless of whether
the stage requires approval. approve_document() calls should_index()
(app/services/indexing.py) at the end: on a requires_approval stage, a
version can already be Scanner-`indexed` and still be waiting on exactly
this approval before it's actually ready to index.

Gating: has_permission(db, user_id, <action>, team_id, project_id), where
team_id / project_id are the DOCUMENT's team and project. Per §3/4:
  - "submit"  -> contributor+ (same bar as upload)
  - "approve" -> team_lead+ on that specific team
  - "reject"  -> team_lead+ on that specific team
org_admin / project_admin bypass, as everywhere else.

The `role` parameter is accepted for call-site symmetry with the rest of the
service layer; has_permission() is the actual authority and does not need it.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentScan, DocumentStatus, DocumentVersion, VersionApprovalOutcome
from app.models.workflow import WorkflowState, WorkflowStatus
from app.services.access_control import has_permission, is_grant_only_confidential_access
from app.services.audit import record_audit
from app.services.indexing import index_document, should_index, unindex_document
from app.services.notifications import (
    notify_document_decided,
    notify_document_viewers,
    notify_pending_review,
)

_OVERRIDE_ROLES = {"org_admin", "project_admin"}


class WorkflowError(Exception):
    """No workflow row for this document, or it is in the wrong state."""


class WorkflowPermissionError(Exception):
    """Caller lacks the required role for this workflow action."""


class WorkflowScanNotPassedError(Exception):
    """
    The document's current version never passed the Structure/Injection
    Scanner (DocumentVersion.status != indexed). Approval is blocked
    regardless of what the frontend showed the reviewer — the frontend's
    disabled-Approve-button guard is UX only; this is the real gate.

    Carries `scan_detail` (overall_score, criteria, injection_flagged, or
    None if the version was never scored at all) so the router can return
    it to the caller.
    """

    def __init__(self, message: str, scan_detail: dict | None):
        super().__init__(message)
        self.scan_detail = scan_detail


def _latest_scan_detail(db: Session, document: Document) -> dict | None:
    """
    The most recent DocumentScan for the document's current version, as a
    plain dict for API responses/audit logging. None if the version was
    never scored (e.g. a total Groq failure at finalize time left nothing
    to record — see document_finalize.py's _record_scan).
    """
    if document.current_version_id is None:
        return None
    scan = (
        db.query(DocumentScan)
        .filter(DocumentScan.version_id == document.current_version_id)
        .order_by(DocumentScan.created_at.desc())
        .first()
    )
    if scan is None:
        return None
    return {
        "overall_score": scan.overall_score,
        "criteria": scan.criteria,
        "injection_flagged": scan.injection_flagged,
    }


def _check_scan_passed(
    db: Session, document_id: uuid.UUID, role: str, override: bool
) -> tuple[dict | None, bool]:
    """
    Raises WorkflowScanNotPassedError unless the document's current version
    is Scanner-`indexed` (see app/services/indexing.py's should_index
    docstring — `indexed` is the single canonical "Scanner satisfied" flag,
    covering both the structural score threshold and the injection check).

    Returns (scan_detail, override_used). override_used is True only when
    the scan had actually failed and an org_admin/project_admin's
    override=True let it through anyway — the caller audit-logs exactly
    that case, never a no-op override on an already-passing scan. A
    team_lead cannot override — intentionally a higher bar than ordinary
    approval.
    """
    document = db.get(Document, document_id)
    scan_detail = _latest_scan_detail(db, document) if document else None

    version = (
        db.get(DocumentVersion, document.current_version_id)
        if document and document.current_version_id
        else None
    )
    scan_ok = version is not None and version.status == DocumentStatus.indexed
    if scan_ok:
        return scan_detail, False

    if override and role in _OVERRIDE_ROLES:
        return scan_detail, True

    raise WorkflowScanNotPassedError(
        "This document's current version did not pass the structure/injection "
        "scan and cannot be approved.",
        scan_detail,
    )


def get_workflow_state(db: Session, document_id: uuid.UUID) -> WorkflowState | None:
    """The document's WorkflowState row, or None if its stage needs no approval."""
    return db.execute(
        select(WorkflowState).where(WorkflowState.document_id == document_id)
    ).scalar_one_or_none()


def _require_state(db: Session, document_id: uuid.UUID) -> WorkflowState:
    state = get_workflow_state(db, document_id)
    if state is None:
        raise WorkflowError(
            f"Document {document_id} has no approval workflow "
            "(its stage does not require approval)."
        )
    return state


def submit_for_review(
    db: Session,
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    team_id: uuid.UUID,
    project_id: uuid.UUID,
    role: str,
) -> WorkflowState:
    """
    draft -> pending_review, or rejected -> pending_review (resubmission after
    addressing the feedback). Requires 'submit' (contributor+). On resubmission
    from 'rejected', the stale rejection_reason is cleared.
    """
    state = _require_state(db, document_id)
    if not has_permission(db, user_id, "submit", team_id, project_id):
        raise WorkflowPermissionError(
            "You do not have permission to submit documents for review on this team."
        )
    document = db.get(Document, document_id)
    if document is not None and is_grant_only_confidential_access(db, user_id, document):
        raise WorkflowPermissionError(
            "Your access to this document is read-only (granted via a confidential-access "
            "request, not your team role) — you cannot submit it for review."
        )
    if state.state not in (WorkflowStatus.draft, WorkflowStatus.rejected):
        raise WorkflowError(
            f"Document is '{state.state.value}' — only a 'draft' or a "
            "'rejected' document can be submitted for review."
        )
    if state.state == WorkflowStatus.rejected:
        state.rejection_reason = None  # the previous rejection no longer applies
    state.state = WorkflowStatus.pending_review
    record_audit(
        db, actor_id=user_id, action="SUBMIT_DOCUMENT", resource_type="document",
        resource_id=document_id, details={"state": "pending_review"},
    )
    if document is not None:
        notify_pending_review(db, document=document, team_id=team_id)
    db.commit()
    return state


def promote_version_on_approval(db: Session, *, document_id: uuid.UUID, version_id: uuid.UUID) -> None:
    """
    The version-numbering half of approving a document — separate from the
    WorkflowState transition so both approve_document() below and
    document_upload_review.py's auto-approve-on-upload branch can share it
    (they both promote a version to approved, just via different call
    paths). Stages on `db` without committing; the caller commits.

    - Assigns `version_id` the next version_number in this document's
      approved lineage (max existing approved version_number + 1, or 1 if
      none yet) and sets its approval_outcome to approved.
    - Every OTHER version of this document that has no approval_outcome
      yet (still an unresolved draft/pending_review/needs_attention
      attempt) is marked approval_outcome=rejected — auto-superseded, since
      a document can only ever have one "current" lineage; an older
      unresolved attempt that didn't win out is a dead end the moment a
      different version gets approved instead. Deliberately does NOT touch
      versions that are already resolved (an earlier version_number that
      was itself approved once, or already explicitly rejected) — history
      is never rewritten.
    """
    target = db.get(DocumentVersion, version_id)
    if target is None:
        return

    max_approved = db.execute(
        select(func.max(DocumentVersion.version_number)).where(
            DocumentVersion.document_id == document_id
        )
    ).scalar_one()
    target.version_number = (max_approved or 0) + 1
    target.approval_outcome = VersionApprovalOutcome.approved

    others = db.execute(
        select(DocumentVersion).where(
            DocumentVersion.document_id == document_id,
            DocumentVersion.version_id != version_id,
            DocumentVersion.approval_outcome.is_(None),
        )
    ).scalars().all()
    for other in others:
        other.approval_outcome = VersionApprovalOutcome.rejected


def approve_document(
    db: Session,
    document_id: uuid.UUID,
    approver_id: uuid.UUID,
    team_id: uuid.UUID,
    project_id: uuid.UUID,
    role: str,
    override: bool = False,
) -> WorkflowState:
    """
    pending_review -> approved. Requires 'approve' (team_lead+ on this team)
    AND the current version must be Scanner-`indexed` (see
    _check_scan_passed) — a reviewer cannot approve a document that never
    passed the structure/injection scan, whatever the frontend showed them.
    org_admin/project_admin may force it through with override=True; that
    override is written to audit_log, never silent.
    """
    state = _require_state(db, document_id)
    if not has_permission(db, approver_id, "approve", team_id, project_id):
        raise WorkflowPermissionError(
            "You do not have permission to approve documents on this team."
        )
    document = db.get(Document, document_id)
    # Currently unreachable in practice: 'approve' already requires
    # team_lead+ (has_permission above), and team_lead+ always has native —
    # never grant-only — access, so this can never actually trigger today.
    # Kept for defense in depth in case the required role for 'approve'
    # ever changes; the message itself must stay plain and user-facing.
    if document is not None and is_grant_only_confidential_access(db, approver_id, document):
        raise WorkflowPermissionError(
            "Your access to this document is read-only (granted via a confidential-access "
            "request, not your team role) — you cannot approve it."
        )
    if state.state != WorkflowStatus.pending_review:
        raise WorkflowError(
            f"Document is '{state.state.value}', not 'pending_review' — cannot approve."
        )
    scan_detail, override_used = _check_scan_passed(db, document_id, role, override)
    if override_used:
        record_audit(
            db, actor_id=approver_id, action="APPROVE_OVERRIDE_SCAN", resource_type="document",
            resource_id=document_id, details={"scan": scan_detail},
        )
        # override=True is meant to let the scan-failed version through for
        # real, not just cosmetically flip WorkflowState to approved while
        # should_index() (which independently checks DocumentVersion.status
        # == indexed) keeps refusing to index it forever — that would make
        # every override silently unsearchable and permanently invisible to
        # R001/coverage, with no way to ever clear it short of a fresh
        # upload. The human override already IS the "Scanner satisfied"
        # signal for this version.
        if document is not None and document.current_version_id is not None:
            version = db.get(DocumentVersion, document.current_version_id)
            if version is not None:
                version.status = DocumentStatus.indexed
    state.state = WorkflowStatus.approved
    state.approved_by = approver_id
    state.approval_timestamp = datetime.now(timezone.utc)
    state.rejection_reason = None
    if document is not None and document.current_version_id is not None:
        promote_version_on_approval(
            db, document_id=document_id, version_id=document.current_version_id
        )
    record_audit(
        db, actor_id=approver_id, action="APPROVE_DOCUMENT", resource_type="document",
        resource_id=document_id, details={"state": "approved"},
    )
    if document is not None and document.uploaded_by != approver_id:
        notify_document_decided(db, document=document, approved=True)
    db.commit()

    # Indexing trigger (app/services/indexing.py): approval is the OTHER
    # half of should_index() for a requires_approval stage — the version may
    # already be Scanner-`indexed` and was only waiting on this.
    if should_index(db, document_id):
        index_document(db, document_id)
        if document is not None:
            notify_document_viewers(
                db, document=document,
                exclude_user_ids={approver_id, document.uploaded_by},
            )
            db.commit()

    return state


def reset_to_draft_if_approved(
    db: Session, *, document_id: uuid.UUID, triggered_by: uuid.UUID
) -> bool:
    """
    Master Plan v2, item 12: finalizing a new document version (via the
    version-diff-review gate) invalidates a prior human sign-off — an
    approved document whose content just changed underneath that approval
    must not remain silently 'approved' for the new content. No-op (returns
    False) if the document has no WorkflowState row (stage doesn't require
    approval) or is not currently 'approved'.

    `triggered_by` is whoever finalized the new version (the audit actor —
    NOT the original approver, who did not take this action).

    BUGFIXES_2026-09-15.md: also removes the document from the RAG index if
    it was there. The prior version could have been should_index()=True
    (Scanner-indexed AND approved) and therefore actually indexed into
    Qdrant; resetting the approval here without also un-indexing left that
    OLD, now-unapproved content fully searchable, indefinitely, with nothing
    in a search result to indicate the approval backing it had been revoked.
    should_index() is false again immediately after this reset (state is no
    longer approved), so un-indexing unconditionally here is always correct
    — never a case where the reset itself is compatible with staying indexed.

    Returns True if a reset actually happened.
    """
    state = get_workflow_state(db, document_id)
    if state is None or state.state != WorkflowStatus.approved:
        return False
    document = db.get(Document, document_id)
    if document is not None and is_grant_only_confidential_access(db, triggered_by, document):
        raise WorkflowPermissionError(
            "Your access to this document is read-only (granted via a confidential-access "
            "request, not your team role) — you cannot revise it in a way that resets its "
            "approval state."
        )
    previous_approver = state.approved_by
    state.state = WorkflowStatus.draft
    state.approved_by = None
    state.approval_timestamp = None
    state.rejection_reason = None
    record_audit(
        db, actor_id=triggered_by, action="RESET_WORKFLOW_ON_NEW_VERSION",
        resource_type="document", resource_id=document_id,
        details={
            "state": "draft", "previous_approver": str(previous_approver) if previous_approver else None,
            "reason": "new version finalized via version-review gate",
        },
    )
    db.commit()
    unindex_document(db, document_id)
    return True


def reject_document(
    db: Session,
    document_id: uuid.UUID,
    approver_id: uuid.UUID,
    team_id: uuid.UUID,
    project_id: uuid.UUID,
    role: str,
    reason: str,
) -> WorkflowState:
    """
    pending_review -> rejected. Requires 'reject' (team_lead+ on this team) and
    a non-empty reason (raises ValueError otherwise).
    """
    if not reason or not reason.strip():
        raise ValueError("A rejection reason is required.")
    state = _require_state(db, document_id)
    if not has_permission(db, approver_id, "reject", team_id, project_id):
        raise WorkflowPermissionError(
            "You do not have permission to reject documents on this team."
        )
    document = db.get(Document, document_id)
    # Same defense-in-depth note as approve_document above: currently
    # unreachable since 'reject' also requires team_lead+.
    if document is not None and is_grant_only_confidential_access(db, approver_id, document):
        raise WorkflowPermissionError(
            "Your access to this document is read-only (granted via a confidential-access "
            "request, not your team role) — you cannot reject it."
        )
    if state.state != WorkflowStatus.pending_review:
        raise WorkflowError(
            f"Document is '{state.state.value}', not 'pending_review' — cannot reject."
        )
    state.state = WorkflowStatus.rejected
    state.rejection_reason = reason.strip()
    if document is not None and document.current_version_id is not None:
        current_version = db.get(DocumentVersion, document.current_version_id)
        if current_version is not None and current_version.approval_outcome is None:
            current_version.approval_outcome = VersionApprovalOutcome.rejected
    record_audit(
        db, actor_id=approver_id, action="REJECT_DOCUMENT", resource_type="document",
        resource_id=document_id, details={"state": "rejected", "reason": reason.strip()},
    )
    if document is not None and document.uploaded_by != approver_id:
        notify_document_decided(db, document=document, approved=False, reason=reason.strip())
    db.commit()
    return state
