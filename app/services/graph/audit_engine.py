import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from sqlalchemy.orm import Session

from app.models.graph import (
    AuditFinding,
    AuditRun,
    Edge,
    Node,
    ProjectMetricSnapshot,
    StageMetricSnapshot,
)
from app.models.project import Project
from app.models.stage import Stage
from app.models.required_document import RequiredDocument
from app.services.graph.audit_rules import (
    FindingSpec,
    evaluate_r001_missing_mandatory_requirements,
    evaluate_r002_unapproved_documents_in_gate_stages,
    evaluate_r003_broken_stage_dependencies,
    evaluate_r004_stale_document_references,
    evaluate_r005_dependency_cycles,
    evaluate_r006_permitted_stage_reference_violations,
    evaluate_r007_unassigned_stage_requirements,
    evaluate_r008_document_contradictions,
    evaluate_r009_pending_workflow_blockers,
    evaluate_r010_document_coherence,
    evaluate_r011_scanner_flagged_current_version,
)
from app.services.graph.sync import sync_project_graph

logger = logging.getLogger(__name__)

# Master Plan v2, item 13: bumped for the original R006's removal (an
# "orphan entity" rule, verified entirely unreachable under the current
# schema/upload path — every one of its three conditions required a state no
# real Document row can be in, given project_id/stage_id/uploaded_as_team_id
# are all non-nullable FK columns set once, together, at creation from the
# same validated team/stage context) and R002's overlap-with-the-pending-
# workflow-blocker-rule fix (R002 now excludes pending_review, matching its
# own docstring instead of contradicting it). UI_FIXES_2026-09-15.md #28/#30
# added a new Scanner-flagged-current-version rule — bumped again since a
# fresh rule changes what a given project audit can find. #32 then
# renumbered R007-R012 down to R006-R011 to close the gap the original R006's
# deletion left — the CODES below shifted, but every rule's actual logic is
# unchanged; this is a relabeling, not a behavior change. Bumped once more
# for that, since AUDIT_RULES_VERSION exists to let a caller tell "the same
# codes mean the same checks" apart from "the numbering changed."
AUDIT_RULES_VERSION = "1.4.0"

# Explicit inspectable rule registry for deterministic project audit.
RULE_REGISTRY = [
    ("R001", evaluate_r001_missing_mandatory_requirements),
    ("R002", evaluate_r002_unapproved_documents_in_gate_stages),
    ("R003", evaluate_r003_broken_stage_dependencies),
    ("R004", evaluate_r004_stale_document_references),
    ("R005", evaluate_r005_dependency_cycles),
    ("R006", evaluate_r006_permitted_stage_reference_violations),
    ("R007", evaluate_r007_unassigned_stage_requirements),
    ("R008", evaluate_r008_document_contradictions),
    ("R009", evaluate_r009_pending_workflow_blockers),
    ("R010", evaluate_r010_document_coherence),
    ("R011", evaluate_r011_scanner_flagged_current_version),
]



def get_upstream_stage_ids(
    db: Session, project_id: uuid.UUID, target_stage_id: uuid.UUID
) -> List[uuid.UUID]:
    """
    Directional traversal: gathers target stage S and all upstream ancestors U
    reachable via inverted PRECEDES edges (where U precedes ... precedes S).
    Strictly excludes future/downstream descendants.
    """
    # Active stages ordered by order_index
    active_stages = (
        db.query(Stage)
        .filter(Stage.project_id == project_id, Stage.deleted_at.is_(None))
        .order_by(Stage.order_index.asc(), Stage.created_at.asc())
        .all()
    )
    target_idx: Optional[int] = None
    for i, st in enumerate(active_stages):
        if st.stage_id == target_stage_id:
            target_idx = i
            break

    if target_idx is None:
        # Fallback if target stage not found in active list
        return [target_stage_id]

    # All stages up to and including target_idx are upstream or current
    return [s.stage_id for s in active_stages[: target_idx + 1]]


def execute_project_audit(
    db: Session,
    project_id: uuid.UUID,
    target_stage_id: Optional[uuid.UUID] = None,
    triggered_by: Optional[uuid.UUID] = None,
) -> AuditRun:
    """
    Executes a deterministic, lifecycle-aware, stage-scoped audit.
    Generates immutable AuditRun, AuditFinding, and MetricSnapshots.
    """
    project = db.query(Project).filter(Project.project_id == project_id).first()
    if not project:
        raise ValueError(f"Project {project_id} not found")

    tenant_id = project.tenant_id

    # 1. Capture Immutable Lifecycle Snapshot
    active_stages = (
        db.query(Stage)
        .filter(Stage.project_id == project_id, Stage.deleted_at.is_(None))
        .order_by(Stage.order_index.asc(), Stage.created_at.asc())
        .all()
    )
    lifecycle_snapshot = {
        "stages": [
            {
                "stage_id": str(s.stage_id),
                "name": s.name,
                "order_index": s.order_index,
                "requires_approval": s.requires_approval,
            }
            for s in active_stages
        ],
        "audit_scope": str(target_stage_id) if target_stage_id else "full_project",
    }
    stage_name_map = {s.stage_id: s.name for s in active_stages}

    # 2. Determine Directional Evaluation Scope
    if target_stage_id:
        evaluated_stage_ids = get_upstream_stage_ids(db, project_id, target_stage_id)
    else:
        evaluated_stage_ids = [s.stage_id for s in active_stages]

    # 3. Evaluate Deterministic Rules via Explicit Registry
    all_findings: List[FindingSpec] = []
    rules_evaluated_count = len(RULE_REGISTRY)

    for rule_code, rule_fn in RULE_REGISTRY:
        rule_findings = rule_fn(
            db=db,
            tenant_id=tenant_id,
            project_id=project_id,
            evaluated_stage_ids=evaluated_stage_ids,
            stage_name_map=stage_name_map,
        )
        all_findings.extend(rule_findings)


    # 4. Compute Metrics: Completeness (Continuous) vs Readiness (Hard Gate)
    blockers = [f for f in all_findings if f.is_blocker]
    blockers_count = len(blockers)
    readiness_status = "READY" if blockers_count == 0 else "NOT_READY"

    # Completeness calculation based on mandatory requirement satisfaction
    total_reqs = (
        db.query(RequiredDocument)
        .filter(
            RequiredDocument.stage_id.in_(evaluated_stage_ids),
            RequiredDocument.is_mandatory.is_(True),
        )
        .count()
    )
    missing_reqs_count = len([f for f in all_findings if f.rule_code == "R001"])
    satisfied_reqs_count = max(0, total_reqs - missing_reqs_count)

    # total_reqs == 0 means "no mandatory requirements configured to measure
    # coverage against" -- NOT "100% satisfied". completeness_score stays a
    # float column (schema unchanged) so None is represented as -1.0, a value
    # the coverage formula can never otherwise produce (score is always in
    # [0, 100]); readers must treat completeness_score < 0 as "not configured"
    # rather than a real percentage. See AUDIT_RULE_TAXONOMY_MATRIX.md.
    if total_reqs > 0:
        completeness_score = round((satisfied_reqs_count / total_reqs) * 100.0, 1)
    else:
        completeness_score = -1.0

    severity_counts = {
        "CRITICAL": len([f for f in all_findings if f.severity == "CRITICAL"]),
        "HIGH": len([f for f in all_findings if f.severity == "HIGH"]),
        "MEDIUM": len([f for f in all_findings if f.severity == "MEDIUM"]),
        "LOW": len([f for f in all_findings if f.severity == "LOW"]),
    }

    # 5. Persist AuditRun
    audit_run = AuditRun(
        tenant_id=tenant_id,
        project_id=project_id,
        target_stage_id=target_stage_id,
        lifecycle_snapshot=lifecycle_snapshot,
        rules_version=AUDIT_RULES_VERSION,
        triggered_by=triggered_by,
        status="completed",
        rules_evaluated=rules_evaluated_count,
        findings_count=len(all_findings),
        readiness_status=readiness_status,
        completeness_score=completeness_score,
        summary={
            "blockers_count": blockers_count,
            "severity_breakdown": severity_counts,
            "total_mandatory_requirements": total_reqs,
            "satisfied_mandatory_requirements": satisfied_reqs_count,
        },
        completed_at=datetime.now(timezone.utc),
    )
    db.add(audit_run)
    db.flush()

    # 6. Persist Self-Contained AuditFindings
    for f in all_findings:
        finding = AuditFinding(
            run_id=audit_run.run_id,
            tenant_id=tenant_id,
            project_id=project_id,
            target_stage_id=f.target_stage_id,
            rule_code=f.rule_code,
            severity=f.severity,
            is_blocker=f.is_blocker,
            title=f.title,
            description=f.description,
            affected_entity_type=f.affected_entity_type,
            affected_entity_id=f.affected_entity_id,
            evidence_sources=f.evidence_sources,
            details=f.details,
        )
        db.add(finding)

    # 7. Persist Historical Project & Stage Metric Snapshots
    project_snapshot = ProjectMetricSnapshot(
        tenant_id=tenant_id,
        project_id=project_id,
        audit_run_id=audit_run.run_id,
        completeness_score=completeness_score,
        readiness_status=readiness_status,
        # Same not-configured sentinel as completeness_score above -- -1.0,
        # never a value the real ratio can produce.
        mandatory_requirement_coverage=(satisfied_reqs_count / total_reqs) if total_reqs > 0 else -1.0,
        approval_health=1.0 if len([f for f in all_findings if f.rule_code in ("R002", "R009")]) == 0 else 0.5,
        dependency_health=1.0 if len([f for f in all_findings if f.rule_code in ("R003", "R005", "R006")]) == 0 else 0.0,
        # Master Plan v2, item 13: document_health was a proxy for the
        # original R006's finding count (an "orphan entity" rule, deleted —
        # verified structurally unreachable; see the former
        # evaluate_r006_orphan_entities and this file's RULE_REGISTRY
        # comment). R006 is now a different, real rule (Cross-Stage
        # Reference Violation, after UI_FIXES_2026-09-15.md #32's
        # renumbering) but this metric was never rewired to it — it stays
        # fixed at 1.0 rather than left wired to an empty filter that would
        # silently always equal 1.0 anyway — the column stays (nullable=False,
        # dropping it is a schema migration outside this item's scope) but
        # its value is explicitly constant, not computed, so a future reader
        # isn't misled into thinking a check still backs it.
        document_health=1.0,
        # version_reference_health was a proxy for R004's finding count. R004
        # is registered but has never produced a finding (see its docstring:
        # neither extraction path ever writes a version-pinned edge
        # property), so this has always been 1.0 in practice and now is
        # documented as such rather than left implicit.
        version_reference_health=1.0,
        conflict_health=1.0 if len([f for f in all_findings if f.rule_code == "R008"]) == 0 else 0.0,
        open_findings_by_severity=severity_counts,
        blockers_count=blockers_count,
    )
    db.add(project_snapshot)

    for st_id in evaluated_stage_ids:
        stage_blockers = [f for f in blockers if f.target_stage_id == st_id]
        stage_reqs_total = (
            db.query(RequiredDocument)
            .filter(RequiredDocument.stage_id == st_id, RequiredDocument.is_mandatory.is_(True))
            .count()
        )
        stage_reqs_missing = len([f for f in all_findings if f.target_stage_id == st_id and f.rule_code == "R001"])
        stage_satisfied = max(0, stage_reqs_total - stage_reqs_missing)
        # Not-configured sentinel, same convention as the project-level score above.
        stage_comp = round((stage_satisfied / stage_reqs_total) * 100.0, 1) if stage_reqs_total > 0 else -1.0

        stage_snapshot = StageMetricSnapshot(
            tenant_id=tenant_id,
            project_id=project_id,
            stage_id=st_id,
            stage_name=stage_name_map.get(st_id, "Unknown Stage"),
            audit_run_id=audit_run.run_id,
            completeness_score=stage_comp,
            readiness_status="READY" if len(stage_blockers) == 0 else "NOT_READY",
            mandatory_requirements_total=stage_reqs_total,
            mandatory_requirements_satisfied=stage_satisfied,
            mandatory_requirements_missing=stage_reqs_missing,
            mandatory_requirements_partial=0,
            mandatory_requirements_blocked=len(stage_blockers),
            upstream_requirements_applicable=total_reqs - stage_reqs_total,
            upstream_requirements_satisfied=max(0, (total_reqs - stage_reqs_total) - (missing_reqs_count - stage_reqs_missing)),
            evidence_coverage=(-1.0 if stage_reqs_total == 0 else (1.0 if stage_reqs_missing == 0 else 0.0)),
            # See project_snapshot.document_health above — same reason, fixed
            # at 1.0 since nothing rewired this per-stage value to a real rule.
            document_health=1.0,
            approvals_satisfied=len([f for f in all_findings if f.target_stage_id == st_id and f.rule_code in ("R002", "R009")]) == 0,
            blockers_count=len(stage_blockers),
        )

        db.add(stage_snapshot)

    db.commit()
    return audit_run


def sync_and_audit_project(
    project_id: uuid.UUID,
    tenant_id: uuid.UUID,
    triggered_by: Optional[uuid.UUID] = None,
) -> None:
    """
    Master Plan v2, item 5: the combined "make the graph/audit reflect
    current state" background task.

    sync_project_graph() was previously called ONLY from seed/test scripts —
    no live app code path ever ran it. That means for any project created and
    used through the real app (not the seeded demo), knowledge.nodes/edges was
    always empty, and every audit rule that reads the graph (R001's evidence
    edges, R006's ALLOWED_REFERENCE, etc.) had nothing to find — not stale
    data, just NO data. Re-running execute_project_audit alone would not have
    fixed that; the sync has to happen first, every time.

    Runs on its OWN fresh session (never the request's session — a
    BackgroundTasks callable can execute after the request's session is
    already closed) with tenant context set BEFORE the first query, per
    database.py's RLS-enforcing after_begin listener. Swallows and logs
    exceptions rather than raising: this runs detached from any request, so
    there is no caller left to hand an exception to.
    """
    from app.database import SessionLocal  # local import: avoid a module-load-time cycle

    from app.services.graph.completion import reopen_project_if_stale

    db = SessionLocal()
    db.info["tenant_id"] = str(tenant_id)
    try:
        sync_project_graph(db, project_id)
        audit_run = execute_project_audit(db, project_id, triggered_by=triggered_by)

        project = db.get(Project, project_id)
        if project is not None and project.completed_at is not None:
            if reopen_project_if_stale(db, project, audit_run):
                db.commit()
    except Exception:
        logger.exception(
            "Background sync_and_audit_project failed for project_id=%s", project_id
        )
    finally:
        db.close()


def extract_sync_and_audit_document(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    project_id: uuid.UUID,
    tenant_id: uuid.UUID,
    triggered_by: Optional[uuid.UUID] = None,
) -> None:
    """
    Master Plan v2, item 9: the combined background task for a document
    finalize — extraction, then sync, then audit, all on one fresh session.

    Like sync_project_graph() before item 5, relationship_extractor.py's
    extract_document_relationships() and claims_analyzer.py's claim
    extraction were previously called ONLY from seed/test scripts — never
    from live app code. That means knowledge.claims and the regex-derived
    REFERENCES/EVIDENCES/etc. edges were always empty for a real project too,
    on top of the graph itself being empty (item 5's finding). Wiring THIS
    into the finalize path is what makes the LLM-augmented extraction below
    (and the regex pass it's additive to) actually run for real documents.

    Order matters: extraction creates document/requirement nodes as a side
    effect (idempotent upserts) but doesn't need the full graph to already
    exist; sync_project_graph() re-affirms the baseline structural graph
    (which extraction didn't touch); the audit needs both to be current.
    """
    from app.database import SessionLocal  # local import: avoid a module-load-time cycle
    from app.models.document import Document, DocumentVersion
    from app.services.graph.claims_analyzer import extract_claims_from_text, persist_claims, persist_llm_claims
    from app.services.graph.coherence import run_document_coherence_check
    from app.services.graph.llm_extraction import extract_claims_llm
    from app.services.graph.relationship_extractor import extract_document_relationships

    db = SessionLocal()
    db.info["tenant_id"] = str(tenant_id)
    try:
        version = db.get(DocumentVersion, version_id)
        document = db.get(Document, document_id)
        if version is None or document is None:
            logger.warning(
                "extract_sync_and_audit_document: document/version missing, skipping extraction "
                "(document_id=%s, version_id=%s)", document_id, version_id,
            )
        else:
            content = version.file_data
            if isinstance(content, bytes):
                content = content.decode("utf-8", errors="replace")

            # Regex + LLM reference/dependency/evidence edges (relationship_extractor.py
            # runs both passes internally under one ExtractionRun).
            extraction_result = extract_document_relationships(db, document_id, version_id, content)

            # Regex claims (fixed patterns) + LLM claims (free-form), both tagged
            # by source in Claim.source_locator — see persist_claims/persist_llm_claims.
            regex_claims = extract_claims_from_text(content, tenant_id, project_id, document_id, version_id)
            persist_claims(
                db, tenant_id, project_id, document_id, version_id, regex_claims,
                extraction_run_id=extraction_result.run_id,
            )
            llm_claims = extract_claims_llm(content)
            persist_llm_claims(
                db, tenant_id, project_id, document_id, version_id, llm_claims,
                extraction_run_id=extraction_result.run_id,
            )
            db.commit()

            # Item 10: cache this version's coherence assessment once — R010
            # reads the cache on every audit sweep, never calls the LLM itself.
            run_document_coherence_check(db, document_id, version_id, content)

        sync_project_graph(db, project_id)
        execute_project_audit(db, project_id, triggered_by=triggered_by)
    except Exception:
        logger.exception(
            "Background extract_sync_and_audit_document failed for document_id=%s", document_id
        )
    finally:
        db.close()
