import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from sqlalchemy import desc, or_
from sqlalchemy.orm import Session

from app.models.graph import (
    AuditFinding,
    AuditRun,
    ProjectMetricSnapshot,
    StageMetricSnapshot,
)
from app.models.audit import AuditLog
from app.models.document import Document
from app.models.stage import Stage


def get_project_progress_history(
    db: Session, project_id: uuid.UUID, limit: int = 50
) -> List[Dict[str, Any]]:
    """
    Returns time-series history of project completeness, readiness, and dimension scores.
    Ordered chronologically (oldest to newest).
    """
    snapshots = (
        db.query(ProjectMetricSnapshot)
        .filter(ProjectMetricSnapshot.project_id == project_id)
        .order_by(desc(ProjectMetricSnapshot.snapshot_at))
        .limit(limit)
        .all()
    )

    # Reverse to return chronologically ascending
    snapshots.reverse()

    return [
        {
            "snapshot_id": str(s.snapshot_id),
            "project_id": str(s.project_id),
            "audit_run_id": str(s.audit_run_id) if s.audit_run_id else None,
            "completeness_score": s.completeness_score,
            "readiness_status": s.readiness_status,
            "mandatory_requirement_coverage": s.mandatory_requirement_coverage,
            "approval_health": s.approval_health,
            "dependency_health": s.dependency_health,
            "document_health": s.document_health,
            "version_reference_health": s.version_reference_health,
            "conflict_health": s.conflict_health,
            "open_findings_by_severity": s.open_findings_by_severity,
            "blockers_count": s.blockers_count,
            "snapshot_at": s.snapshot_at.isoformat(),
        }
        for s in snapshots
    ]


def get_stage_health_history(
    db: Session,
    project_id: uuid.UUID,
    stage_id: Optional[uuid.UUID] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """
    Returns time-series history of stage completeness, readiness, and requirements fulfillment.
    Historical snapshots remain queryable even if the live stage is subsequently hard-deleted.
    """
    query = db.query(StageMetricSnapshot).filter(
        StageMetricSnapshot.project_id == project_id
    )

    if stage_id:
        query = query.filter(StageMetricSnapshot.stage_id == stage_id)

    snapshots = (
        query.order_by(desc(StageMetricSnapshot.snapshot_at))
        .limit(limit)
        .all()
    )
    snapshots.reverse()

    return [
        {
            "snapshot_id": str(s.snapshot_id),
            "project_id": str(s.project_id),
            "stage_id": str(s.stage_id),
            "stage_name": s.stage_name,
            "audit_run_id": str(s.audit_run_id) if s.audit_run_id else None,
            "completeness_score": s.completeness_score,
            "readiness_status": s.readiness_status,
            "mandatory_requirements_total": s.mandatory_requirements_total,
            "mandatory_requirements_satisfied": s.mandatory_requirements_satisfied,
            "mandatory_requirements_missing": s.mandatory_requirements_missing,
            "mandatory_requirements_partial": s.mandatory_requirements_partial,
            "mandatory_requirements_blocked": s.mandatory_requirements_blocked,
            "upstream_requirements_applicable": s.upstream_requirements_applicable,
            "upstream_requirements_satisfied": s.upstream_requirements_satisfied,
            "evidence_coverage": s.evidence_coverage,
            "document_health": s.document_health,
            "approvals_satisfied": s.approvals_satisfied,
            "blockers_count": s.blockers_count,
            "snapshot_at": s.snapshot_at.isoformat(),
        }
        for s in snapshots
    ]


def get_project_timeline(
    db: Session, project_id: uuid.UUID, limit: int = 100
) -> List[Dict[str, Any]]:
    """
    Synthesizes project lifecycle events combining:
    1. Historical audit runs with readiness transitions
    2. AuditLog user activities (documents uploaded, approvals granted, stages created)
    Sorted chronologically descending (newest first).
    """
    timeline_items: List[Dict[str, Any]] = []

    # 1. Audit Runs
    audit_runs = (
        db.query(AuditRun)
        .filter(AuditRun.project_id == project_id)
        .order_by(desc(AuditRun.started_at))
        .limit(limit)
        .all()
    )
    for ar in audit_runs:
        ts = ar.completed_at or ar.started_at
        timeline_items.append({
            "event_type": "AUDIT_RUN",
            "timestamp": ts.isoformat() if ts else datetime.utcnow().isoformat(),
            "title": f"Audit Run ({ar.status.upper()})",
            "description": f"Audited {ar.rules_evaluated} rules with status {ar.status}.",
            "actor_user_id": str(ar.triggered_by) if ar.triggered_by else None,
            "metadata": {
                "run_id": str(ar.run_id),
                "target_stage_id": str(ar.target_stage_id) if ar.target_stage_id else None,
                "rules_evaluated": ar.rules_evaluated,
                "status": ar.status,
            },
        })

    # 2. Document IDs in project for matching AuditLogs
    doc_ids = [
        d.document_id
        for d in db.query(Document.document_id).filter(Document.project_id == project_id).all()
    ]

    # Query AuditLog entries
    audit_log_filters = [
        (AuditLog.resource_type == "project") & (AuditLog.resource_id == project_id)
    ]
    if doc_ids:
        audit_log_filters.append(
            (AuditLog.resource_type == "document") & (AuditLog.resource_id.in_(doc_ids))
        )

    logs = (
        db.query(AuditLog)
        .filter(or_(*audit_log_filters))
        .order_by(desc(AuditLog.created_at))
        .limit(limit)
        .all()
    )

    for log in logs:
        timeline_items.append({
            "event_type": "USER_ACTION",
            "timestamp": log.created_at.isoformat() if log.created_at else datetime.utcnow().isoformat(),
            "title": f"{log.action.replace('_', ' ').title()}",
            "description": f"Action '{log.action}' performed on {log.resource_type}.",
            "actor_user_id": str(log.user_id),
            "metadata": {
                "log_id": str(log.log_id),
                "resource_type": log.resource_type,
                "resource_id": str(log.resource_id) if log.resource_id else None,
                "details": log.details or {},
            },
        })

    # Sort combined timeline descending
    timeline_items.sort(key=lambda x: x["timestamp"], reverse=True)
    return timeline_items[:limit]


def get_latest_project_health(
    db: Session, project_id: uuid.UUID
) -> Optional[Dict[str, Any]]:
    """
    Returns the latest project and stage health snapshots.
    """
    latest_project_snapshot = (
        db.query(ProjectMetricSnapshot)
        .filter(ProjectMetricSnapshot.project_id == project_id)
        .order_by(desc(ProjectMetricSnapshot.snapshot_at))
        .first()
    )
    if not latest_project_snapshot:
        return None

    stage_snapshots = (
        db.query(StageMetricSnapshot)
        .filter(
            StageMetricSnapshot.project_id == project_id,
            StageMetricSnapshot.audit_run_id == latest_project_snapshot.audit_run_id,
        )
        .all()
    )

    # StageMetricSnapshot.stage_id is deliberately not FK'd (history survives
    # a stage's deletion) — look up order_index/requires_approval for
    # whichever stages are still live, so item 8's frontend pipeline can sort
    # in real project order instead of arbitrary snapshot-insertion order.
    live_stages = {
        s.stage_id: s
        for s in db.query(Stage).filter(
            Stage.stage_id.in_([s.stage_id for s in stage_snapshots])
        ).all()
    } if stage_snapshots else {}

    stage_snapshots.sort(
        key=lambda s: (
            live_stages[s.stage_id].order_index if s.stage_id in live_stages else float("inf")
        )
    )

    return {
        "project_metric": {
            "snapshot_id": str(latest_project_snapshot.snapshot_id),
            "completeness_score": latest_project_snapshot.completeness_score,
            "readiness_status": latest_project_snapshot.readiness_status,
            "mandatory_requirement_coverage": latest_project_snapshot.mandatory_requirement_coverage,
            "approval_health": latest_project_snapshot.approval_health,
            "dependency_health": latest_project_snapshot.dependency_health,
            "document_health": latest_project_snapshot.document_health,
            "version_reference_health": latest_project_snapshot.version_reference_health,
            "conflict_health": latest_project_snapshot.conflict_health,
            "open_findings_by_severity": latest_project_snapshot.open_findings_by_severity,
            "blockers_count": latest_project_snapshot.blockers_count,
            "snapshot_at": latest_project_snapshot.snapshot_at.isoformat(),
        },
        "stages": [
            {
                "stage_id": str(s.stage_id),
                "stage_name": s.stage_name,
                "completeness_score": s.completeness_score,
                "readiness_status": s.readiness_status,
                "mandatory_requirements_total": s.mandatory_requirements_total,
                "mandatory_requirements_satisfied": s.mandatory_requirements_satisfied,
                "mandatory_requirements_missing": s.mandatory_requirements_missing,
                "blockers_count": s.blockers_count,
                "order_index": live_stages[s.stage_id].order_index if s.stage_id in live_stages else None,
                "requires_approval": live_stages[s.stage_id].requires_approval if s.stage_id in live_stages else None,
            }
            for s in stage_snapshots
        ],
    }
