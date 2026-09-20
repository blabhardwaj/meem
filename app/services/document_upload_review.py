"""
Upload entry point for the "upload + scan + revise-in-chat + index" flow.

upload_and_scan() ties together:
  - document_persistence.create_document_from_file() — real file bytes ->
    Document + DocumentVersion (v1, pending_review by default), parsed to
    Markdown (Docling, called exactly once — every downstream step below
    reuses this same parsed_content, never re-parses)
  - a content-hash marker check (draft_workspace.check_scan_marker) — if this
    exact file was previously finalized by chat-drafting (Phase 4) and is
    unchanged, SKIP the paid structural rescan and reuse its embedded score
  - the Structure Scanner (score + reform-if-low, via document_finalize's
    shared run_full_scan) otherwise
  - the Injection Scanner — ALWAYS runs regardless of the marker, since it's
    a cheap deterministic security gate, not the expensive check being
    skipped. A flagged upload gets its v1 status corrected from the default
    pending_review to needs_attention (the row is still created either way)
  - persisting a DocumentScan row for v1
  - seeding the review chat session's on-disk working file (same
    draft_workspace machinery as Phase 4) with the parsed content, so the
    drafting_agent can revise it from there
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.document import Document, DocumentScan, DocumentStatus, DocumentVersion, ScanReviewStatus
from app.models.workflow import WorkflowState, WorkflowStatus
from app.services import draft_workspace
from app.services.access_control import has_permission
from app.services.document_finalize import failed_criteria, run_full_scan, scan_passed
from app.services.document_persistence import CreatedDocumentFromFile, create_document_from_file
from app.services.indexing import index_document, should_index
from app.services.injection_scan import scan_for_injection
from app.services.notifications import notify_document_viewers
from app.services.workflow import promote_version_on_approval


def upload_and_scan(
    db: Session,
    *,
    user_id: uuid.UUID,
    team_id: uuid.UUID,
    project_id: uuid.UUID,
    role: str,
    stage_id: uuid.UUID,
    original_filename: str,
    mime_type: str,
    file_data: bytes,
    session_id: str,
    sensitivity: str = "internal",
) -> dict:
    """
    Raises the same exceptions as create_document_from_file (PermissionDeniedError,
    StageNotFoundError, UnsupportedDocumentTypeError, DocumentParseError).

    Returns:
        {
            "created": CreatedDocumentFromFile,
            "scan": dict | None, "scan_error": str | None,
            "reformed_content": str | None,
            "injection_flagged": bool, "injection_findings": list,
            "scan_skipped": bool,   # True when the content-hash marker matched
            "scan_passed": bool, "failed_criteria": list[str],
            "status": str,    # the version's REAL resulting DocumentStatus —
                               # "pending_review" for an ordinary upload, but
                               # "indexed" when the uploader could auto-approve
                               # and the scan passed (BUGFIXES_2026-09-15.md
                               # Bug 3) or "needs_attention" when flagged.
            "auto_approved": bool,
        }
    """
    created: CreatedDocumentFromFile = create_document_from_file(
        db,
        user_id=user_id, team_id=team_id, project_id=project_id, role=role,
        stage_id=stage_id, original_filename=original_filename, mime_type=mime_type,
        file_data=file_data, sensitivity=sensitivity,
    )

    marker = draft_workspace.check_scan_marker(created.parsed_content)

    if marker is not None:
        # Exact same content as a previously chat-finalized file — skip the
        # (LLM-backed) structural rescan, reuse the embedded score. The
        # injection scan still runs: it's a new check that never ran when
        # this file was chat-finalized, and it's cheap/local either way.
        seed_content = marker["content_without_marker"]
        scan_outcome = {
            "scan": {
                "overall_score": marker["score"],
                "criteria": [],
                "summary": "Reused from this file's earlier chat-drafting scan (content unchanged).",
            },
            "scan_error": None,
            "reformed_content": None,
            "injection": scan_for_injection(seed_content),
        }
        scan_skipped = True
    else:
        seed_content = created.parsed_content
        scan_outcome = run_full_scan(seed_content)
        scan_skipped = False

    passed = scan_passed(scan_outcome)

    # The row was just created with the model's default status
    # (pending_review — v1 always starts there, awaiting the human's first
    # look via the review chat, regardless of structural score). Injection
    # is a SECURITY concern, not a quality one, so it overrides that default
    # immediately, at upload time — don't block persistence, but don't let a
    # flagged upload sit in the ordinary "awaiting review" bucket either.
    if scan_outcome["injection"]["flagged"]:
        db.query(DocumentVersion).filter(
            DocumentVersion.version_id == created.version_id
        ).update({"status": DocumentStatus.needs_attention}, synchronize_session=False)

    # BUGFIXES_2026-09-15.md: an uploader who already holds 'approve'
    # permission on this team (team_lead/admin) gets their upload reviewed
    # immediately, right here, instead of silently sitting in
    # pending_review forever until they separately open the review chat and
    # finalize — most such uploaders have no reason to know that second
    # step exists, and previously ended up with a WorkflowState the UI
    # showed as "approved" while should_index() could never be true (the
    # version's own status never left pending_review). The gate now runs
    # BEFORE any WorkflowState is set to approved: passing => real
    # Scanner-driven `indexed` status, approved, and actually indexed into
    # Qdrant; failing => stays `pending_review`/`needs_attention` and the
    # WorkflowState stays `draft`, exactly like any other failed scan — an
    # uploader with approve rights never gets a free pass around the scan.
    can_auto_approve = (
        created.workflow_state is not None  # stage requires approval -> a WorkflowState row exists
        and has_permission(db, user_id, "approve", team_id, project_id)
    )
    auto_approved = False
    final_status = (
        DocumentStatus.needs_attention if scan_outcome["injection"]["flagged"] else DocumentStatus.pending_review
    )
    review_status = ScanReviewStatus.pending

    if can_auto_approve and passed and not scan_outcome["injection"]["flagged"]:
        final_status = DocumentStatus.indexed
        review_status = ScanReviewStatus.not_required
        auto_approved = True

    if final_status != DocumentStatus.pending_review:
        db.query(DocumentVersion).filter(
            DocumentVersion.version_id == created.version_id
        ).update({"status": final_status}, synchronize_session=False)

    if scan_outcome["scan"] is not None:
        db.add(DocumentScan(
            version_id=created.version_id,
            overall_score=scan_outcome["scan"]["overall_score"],
            criteria=scan_outcome["scan"]["criteria"],
            reform_triggered=scan_outcome["reformed_content"] is not None,
            reformed_content=scan_outcome["reformed_content"],
            review_status=review_status,
            injection_flagged=scan_outcome["injection"]["flagged"],
            injection_findings=scan_outcome["injection"]["findings"],
        ))

    if auto_approved:
        now = datetime.now(timezone.utc)
        db.query(WorkflowState).filter(
            WorkflowState.document_id == created.document_id
        ).update({
            "state": WorkflowStatus.approved,
            "approved_by": user_id,
            "approval_timestamp": now,
        }, synchronize_session=False)
        promote_version_on_approval(
            db, document_id=created.document_id, version_id=created.version_id,
        )
        from app.services.audit import record_audit
        audit_entry = record_audit(
            db, actor_id=user_id, action="APPROVE_DOCUMENT", resource_type="document",
            resource_id=created.document_id, details={
                "state": "approved",
                "version_id": str(created.version_id),
                "auto_approved": True,
                "role": role,
                "team_id": str(team_id),
            },
        )
        ver = db.get(DocumentVersion, created.version_id)
        if ver is not None:
            ver.approved_by = user_id
            ver.approved_at = now
            ver.provenance_event_id = audit_entry.log_id


    # Unconditional (not nested under the DocumentScan branch above): the
    # needs_attention status update must be committed even on a total
    # scoring failure (scan_outcome["scan"] is None) if injection was flagged.
    db.commit()

    if auto_approved and should_index(db, created.document_id):
        index_document(db, created.document_id)
        approved_document = db.get(Document, created.document_id)
        if approved_document is not None:
            notify_document_viewers(
                db, document=approved_document, exclude_user_ids={user_id},
            )
            db.commit()

    # Seed the review session's working file — same draft_workspace machinery
    # Phase 4 uses, just seeded with this upload's content instead of blank.
    # Still seeded even when auto-approved: the uploader may still want to
    # open the review chat afterward (e.g. to fix something despite passing,
    # or because they didn't intend the auto-approve) — that path is
    # unaffected by this fix.
    draft_workspace.write_working_draft(session_id, seed_content)

    return {
        "created": created,
        "scan": scan_outcome["scan"],
        "scan_error": scan_outcome["scan_error"],
        "reformed_content": scan_outcome["reformed_content"],
        "injection_flagged": scan_outcome["injection"]["flagged"],
        "injection_findings": scan_outcome["injection"]["findings"],
        "scan_skipped": scan_skipped,
        "scan_passed": passed,
        "failed_criteria": failed_criteria(
            scan_outcome["scan"].get("criteria") if scan_outcome["scan"] else None
        ),
        "status": final_status.value,
        "auto_approved": auto_approved,
    }
