"""
Project-level manual completion marking -- the fourth, human-decided layer
in AUDIT_RULE_TAXONOMY_MATRIX.md (Coverage / Audit / Readiness / Completion).

Completion is NOT another detector. Coverage, Audit, and Readiness are all
recomputed on every audit run; Completion is a fact a project_admin or
org_admin asserts once ("this project is done") and that persists until
something that actually matters changes underneath it.

"Something that matters" is defined narrowly, on purpose: the set of
currently-BLOCKING findings, plus the coverage score. Non-blocking findings
(R004, R006, R009/duplication) are excluded from the signature entirely, so
their churn can never reopen a completed project -- consistent with
Readiness itself only ever being gated by the blocker set. A new draft
document also can't reopen anything: R001 only ever fires for missing
*approved* evidence, so uploading a draft changes neither the score nor the
blocker set, and therefore can't change the signature. That means there is
no separate "ignore drafts" rule to maintain -- it falls out of the model.
"""
import uuid
from typing import List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models.graph import AuditFinding, AuditRun
from app.models.project import Project


def compute_findings_signature(findings: List[AuditFinding]) -> str:
    """
    Deterministic signature of the current blocking-finding set: sorted
    "rule_code:affected_entity_id" pairs, blockers only. Two audit runs with
    the same blocking findings (by rule + entity) produce the same string
    regardless of finding_id/created_at/description churn -- so a completed
    project only reopens when the substantive blocker set actually differs.
    """
    parts = sorted(
        f"{f.rule_code}:{f.affected_entity_id}"
        for f in findings
        if f.is_blocker
    )
    return "|".join(parts)


def mark_project_complete(
    db: Session,
    *,
    project: Project,
    actor_id: uuid.UUID,
    latest_run: Optional[AuditRun],
) -> None:
    """Stages the completion fields on `project` (no commit -- caller commits)."""
    from datetime import datetime, timezone

    project.completed_at = datetime.now(timezone.utc)
    project.completed_by = actor_id

    if latest_run is not None:
        project.completion_snapshot_score = latest_run.completeness_score
        project.completion_snapshot_blockers = latest_run.summary.get(
            "blockers_count", len([f for f in latest_run.findings if f.is_blocker])
        )
        project.completion_snapshot_signature = compute_findings_signature(latest_run.findings)
    else:
        # No audit has ever run for this project -- nothing to compare
        # against later, so any future audit result counts as "changed".
        project.completion_snapshot_score = None
        project.completion_snapshot_blockers = None
        project.completion_snapshot_signature = None


def reopen_project_if_stale(db: Session, project: Project, latest_run: AuditRun) -> bool:
    """
    Called after every execute_project_audit() run for a completed project.
    Compares the fresh run's (score, blockers, signature) against the
    mark-time snapshot; clears completed_at/by if they differ. Returns True
    if it reopened the project.

    Deliberately does NOT compare findings_count or severity_breakdown --
    only the blocker signature and the coverage score, matching the "a
    metric should only claim what its underlying detector actually
    measures" principle: Completion only cares about what actually gates
    Readiness and Coverage, not every audit-run detail.
    """
    if project.completed_at is None:
        return False

    current_signature = compute_findings_signature(latest_run.findings)
    current_blockers = latest_run.summary.get(
        "blockers_count", len([f for f in latest_run.findings if f.is_blocker])
    )

    unchanged = (
        current_signature == (project.completion_snapshot_signature or "")
        and current_blockers == (project.completion_snapshot_blockers or 0)
        and latest_run.completeness_score == project.completion_snapshot_score
    )
    if unchanged:
        return False

    project.completed_at = None
    project.completed_by = None
    project.completion_snapshot_score = None
    project.completion_snapshot_blockers = None
    project.completion_snapshot_signature = None
    return True
