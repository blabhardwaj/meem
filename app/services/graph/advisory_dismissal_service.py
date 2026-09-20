import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.advisory_dismissal import AdvisoryDismissal
from app.models.document import Document
from app.models.graph import AuditFinding
from app.services.access_control import can_view_document
from app.services.audit import record_audit
from app.services.auth import ResolvedIdentity
from app.services.pending_approvals import reviews_project


def compute_finding_fingerprint(
    rule_code: str,
    affected_entity_id: uuid.UUID | str,
    details: Optional[Dict[str, Any]] = None,
    evidence_sources: Optional[List[Any]] = None,
) -> str:
    """
    Computes a strictly deterministic semantic fingerprint for an audit finding.
    
    IMPORTANT: This identity MUST survive re-audits across time. It is based ONLY
    on immutable structural identity attributes that define WHAT the finding is,
    and NEVER on volatile execution attributes like timestamps, similarity scores,
    LLM confidence ratings, or generated natural-language explanations.

    Discriminators:
    - R009: rule_code + affected_entity_id + issue_type + normalized conflicting/reference doc identity or section
    - R004: rule_code + affected_entity_id (consumer doc) + target_document_id
    - R006: rule_code + affected_entity_id (requirement_id)
    - Default: rule_code + affected_entity_id
    """
    details = details or {}
    evidence_sources = evidence_sources or []
    discriminator_parts: List[str] = []

    clean_rule = str(rule_code).strip().upper()
    clean_aff_id = str(affected_entity_id).strip().lower()

    if clean_rule == "R009":
        # 1. Issue type (e.g. duplicate, contradiction, unmet_requirement)
        issue_type = str(details.get("issue_type") or "").strip().lower()
        if issue_type:
            discriminator_parts.append(f"type:{issue_type}")

        # 2. Conflicting/referenced evidence document (if present in evidence_sources)
        conflicting_doc_id = None
        for src in evidence_sources:
            if isinstance(src, dict):
                src_doc_id = src.get("document_id")
                if src_doc_id and str(src_doc_id).lower() != clean_aff_id:
                    conflicting_doc_id = str(src_doc_id).lower()
                    break
        if conflicting_doc_id:
            discriminator_parts.append(f"conflicting_doc:{conflicting_doc_id}")
        else:
            # Fallback: extract normalized section title from related_context if present
            # e.g. "Existing document content (3. Differentiation from Competitors)" -> "3. differentiation from competitors"
            rel_ctx = str(details.get("related_context") or "")
            section_match = re.search(r'\(([\d\w\s\.\-]+)\)', rel_ctx)
            if section_match:
                norm_sec = section_match.group(1).strip().lower()
                discriminator_parts.append(f"sec:{norm_sec}")

    elif clean_rule == "R004":
        # Stale reference target document
        target_doc = details.get("target_document_id") or details.get("target_doc_id")
        if target_doc:
            discriminator_parts.append(f"target:{str(target_doc).lower()}")
        elif evidence_sources:
            for src in evidence_sources:
                if isinstance(src, dict) and src.get("document_id"):
                    s_id = str(src["document_id"]).lower()
                    if s_id != clean_aff_id:
                        discriminator_parts.append(f"target:{s_id}")
                        break

    elif clean_rule == "R006":
        # Unassigned requirement
        stage_name = str(details.get("stage_name") or "").strip().lower()
        if stage_name:
            discriminator_parts.append(f"stage:{stage_name}")

    discriminator_str = "|".join(discriminator_parts)
    raw_key = f"{clean_rule}:{clean_aff_id}:{discriminator_str}"
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()[:32]


def dismiss_advisory_finding(
    db: Session,
    identity: ResolvedIdentity,
    project_id: uuid.UUID,
    finding_id: uuid.UUID,
    reason: str,
) -> AdvisoryDismissal:
    """
    Dismisses an advisory-level audit finding.
    
    Strict Authorization Pipeline:
    1. Finding exists in DB.
    2. Finding belongs to specified project.
    3. Caller has review role in project (Team Lead+, Project Admin, Org Admin).
    4. Gating Blocker check: finding.is_blocker MUST be False.
    5. Document ABAC clearance check (can_view_document).
    6. Persist durable AdvisoryDismissal keyed by (project_id, finding_fingerprint).
    7. Append-only AuditLog record.
    """
    clean_reason = reason.strip() if reason else ""
    if len(clean_reason) < 5:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="A substantive reason (at least 5 characters) is required to dismiss a finding.",
        )

    finding = db.get(AuditFinding, finding_id)
    if not finding:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Finding not found",
        )

    if finding.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Finding does not belong to this project",
        )

    # 3. Role check: Team Lead+ on any project team, project_admin, or org_admin
    if not reviews_project(db, identity.user_id, project_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only team leads, project administrators, and organization administrators can dismiss advisory findings.",
        )

    # 4. Gating blocker check: ONLY advisories (is_blocker == False) are eligible
    if finding.is_blocker:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Gating blockers cannot be dismissed. Only advisory-level findings can be dismissed.",
        )

    # 5. Document ABAC check
    if finding.affected_entity_type == "document" and finding.affected_entity_id:
        doc = db.get(Document, finding.affected_entity_id)
        if doc and not can_view_document(db, identity.user_id, doc):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have clearance to view the document affected by this finding.",
            )

    # 6. Compute stable semantic fingerprint
    fingerprint = compute_finding_fingerprint(
        finding.rule_code,
        finding.affected_entity_id,
        finding.details,
        finding.evidence_sources,
    )

    # 7. Upsert AdvisoryDismissal (durable state across re-audits)
    dismissal = (
        db.query(AdvisoryDismissal)
        .filter(
            AdvisoryDismissal.project_id == project_id,
            AdvisoryDismissal.finding_fingerprint == fingerprint,
        )
        .first()
    )

    now = datetime.now(timezone.utc)
    if dismissal:
        dismissal.is_active = True
        dismissal.reason = clean_reason
        dismissal.dismissed_by = identity.user_id
        dismissal.dismissed_at = now
        dismissal.restored_by = None
        dismissal.restored_at = None
    else:
        dismissal = AdvisoryDismissal(
            tenant_id=identity.tenant_id,
            project_id=project_id,
            finding_fingerprint=fingerprint,
            rule_code=finding.rule_code,
            affected_entity_type=finding.affected_entity_type,
            affected_entity_id=finding.affected_entity_id,
            initial_finding_id=finding.finding_id,
            is_active=True,
            reason=clean_reason,
            dismissed_by=identity.user_id,
            dismissed_at=now,
        )
        db.add(dismissal)

    db.flush()

    # 8. Immutable append-only audit trail
    record_audit(
        db,
        actor_id=identity.user_id,
        action="DISMISS_ADVISORY_FINDING",
        resource_type="audit_finding",
        resource_id=finding.finding_id,
        details={
            "project_id": str(project_id),
            "rule_code": finding.rule_code,
            "affected_entity_id": str(finding.affected_entity_id),
            "fingerprint": fingerprint,
            "title": finding.title,
            "reason": clean_reason,
        },
    )

    db.commit()
    return dismissal


def restore_advisory_finding(
    db: Session,
    identity: ResolvedIdentity,
    project_id: uuid.UUID,
    finding_id: uuid.UUID,
) -> AdvisoryDismissal:
    """
    Restores a previously dismissed advisory finding back to active status.
    """
    finding = db.get(AuditFinding, finding_id)
    if not finding:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Finding not found",
        )

    if finding.project_id != project_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Finding does not belong to this project",
        )

    if not reviews_project(db, identity.user_id, project_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only team leads, project administrators, and organization administrators can restore dismissed findings.",
        )

    if finding.affected_entity_type == "document" and finding.affected_entity_id:
        doc = db.get(Document, finding.affected_entity_id)
        if doc and not can_view_document(db, identity.user_id, doc):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have clearance to view the document affected by this finding.",
            )

    fingerprint = compute_finding_fingerprint(
        finding.rule_code,
        finding.affected_entity_id,
        finding.details,
        finding.evidence_sources,
    )

    dismissal = (
        db.query(AdvisoryDismissal)
        .filter(
            AdvisoryDismissal.project_id == project_id,
            AdvisoryDismissal.finding_fingerprint == fingerprint,
        )
        .first()
    )

    if not dismissal or not dismissal.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Finding is not currently dismissed.",
        )

    now = datetime.now(timezone.utc)
    dismissal.is_active = False
    dismissal.restored_by = identity.user_id
    dismissal.restored_at = now

    db.flush()

    record_audit(
        db,
        actor_id=identity.user_id,
        action="RESTORE_ADVISORY_FINDING",
        resource_type="audit_finding",
        resource_id=finding.finding_id,
        details={
            "project_id": str(project_id),
            "rule_code": finding.rule_code,
            "affected_entity_id": str(finding.affected_entity_id),
            "fingerprint": fingerprint,
            "title": finding.title,
        },
    )

    db.commit()
    return dismissal


def get_active_dismissals_map(db: Session, project_id: uuid.UUID) -> Dict[str, AdvisoryDismissal]:
    """
    Returns a mapping of finding_fingerprint -> AdvisoryDismissal for all currently active dismissals.
    """
    rows = (
        db.query(AdvisoryDismissal)
        .filter(
            AdvisoryDismissal.project_id == project_id,
            AdvisoryDismissal.is_active.is_(True),
        )
        .all()
    )
    return {row.finding_fingerprint: row for row in rows}
