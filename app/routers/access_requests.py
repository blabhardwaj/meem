"""
Phase 6: the confidential-access request workflow.

This is the request/approval side of the "contributor with an active access
grant" exception in app/services/access_control.py — a viewer/contributor who
is blocked from a confidential document asks the team's lead for clearance;
once approved they get a 90-day grant that can_view_document() honours.

  POST /access-requests                    request confidential access on a team
                                           (any member who is not already cleared)
  GET  /access-requests/mine               the caller's own requests + live status
  GET  /access-requests/pending            requests the caller may decide
  POST /access-requests/{id}/approve       -> approved, expires in 90 days
  POST /access-requests/{id}/deny          -> denied

Approve / deny and the /pending listing are gated by
has_permission(..., "approve_access_request", team_id, project_id) — team_lead
on that team, or project_admin / org_admin.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db_with_tenant
from app.models.project import Project
from app.models.team import (
    AccessRequest,
    AccessRequestScope,
    AccessRequestStatus,
    GrantDuration,
    GrantTier,
    Team,
)
from app.models.user import User
from app.services.access_control import _get_team_membership, has_permission
from app.services.access_requests_service import (
    AccessRequestError,
    GRANT_TTL_DAYS as _GRANT_TTL_DAYS,
    is_expired as _is_expired,
    pending_requests_for_reviewer,
    request_confidential_access,
)
from app.services.audit import record_audit
from app.services.auth import ResolvedIdentity
from app.services.notifications import notify_access_request_decided

router = APIRouter(prefix="/access-requests", tags=["access-requests"])

_DURATION_LABELS: dict[GrantDuration, str] = {
    GrantDuration.hours_72: "72 hours",
    GrantDuration.week_1: "1 week",
    GrantDuration.month_1: "1 month",
    GrantDuration.unlimited: "no expiration",
}
_EXPIRES_AT_FOR_DURATION = {
    GrantDuration.hours_72: lambda now: now + timedelta(hours=72),
    GrantDuration.week_1: lambda now: now + timedelta(days=7),
    GrantDuration.month_1: lambda now: now + timedelta(days=30),
    GrantDuration.unlimited: lambda now: None,
}


class CreateAccessRequest(BaseModel):
    team_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    stage_id: uuid.UUID | None = None


class ApproveAccessRequestBody(BaseModel):
    tier: GrantTier | None = None
    duration: GrantDuration | None = None


class AccessRequestOut(BaseModel):
    request_id: str
    user_id: str
    requester_email: str | None
    requester_role: str | None  # the requester's TeamRole on `team_id` — approver visibility
    team_id: str
    team_name: str
    project_id: str
    project_name: str
    scope: str  # "document" | "stage" | "team"
    target_id: str  # document_id, stage_id, or team_id, matching `scope`
    target_name: str  # filename, stage name, or team name, matching `scope`
    tier: str | None  # GrantTier value — set only on a decided stage-scope request
    grant_duration_label: str  # human-readable duration, or a prompt to choose one if still pending
    status: str
    requested_at: str | None
    decided_at: str | None
    expires_at: str | None
    active: bool  # approved and not expired — i.e. currently grants clearance


class EffectiveAccessOut(BaseModel):
    status: str  # "none" | "pending" | "granted" | "denied" | "expired"
    via_grant: bool
    expires_at: str | None
    request_id: str | None


@router.get("/status", response_model=EffectiveAccessOut)
def access_status(
    document_id: uuid.UUID | None = None,
    stage_id: uuid.UUID | None = None,
    team_id: uuid.UUID | None = None,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    Effective-state endpoint, not a raw request-row lookup: resolves current
    grant coverage AND pending/terminal request state for the exact target
    in one call, via the same resolver document visibility and mutation
    enforcement use. The frontend renders this verdict directly — it never
    reconstructs scope/target/grant-precedence logic itself.

    For a stage target specifically: resolve_effective_access() only
    reports "granted" when the active grant is confidential-unlocking
    (contributor_confidential tier) — correct for its own purpose, but this
    endpoint also needs to reflect a plain viewer/contributor grant, which
    IS an active grant even though it confers no confidential access. When
    resolve_stage_grant() finds one and resolve_effective_access() didn't
    already report "granted" via some other path, this reports "granted"
    too, using that grant's own expires_at/request_id.
    """
    provided = [x for x in (document_id, stage_id, team_id) if x is not None]
    if len(provided) != 1:
        raise HTTPException(
            status_code=422, detail="Exactly one of document_id, stage_id, team_id is required."
        )
    from app.services.access_control import resolve_effective_access, resolve_stage_grant

    result = resolve_effective_access(
        db, identity.user_id, document_id=document_id, stage_id=stage_id, team_id=team_id,
    )
    if result.status != "granted" and stage_id is not None:
        stage_grant = resolve_stage_grant(db, identity.user_id, stage_id)
        if stage_grant is not None:
            return EffectiveAccessOut(
                status="granted", via_grant=True,
                expires_at=stage_grant.expires_at.isoformat() if stage_grant.expires_at else None,
                request_id=str(stage_grant.request_id),
            )
    return EffectiveAccessOut(
        status=result.status,
        via_grant=result.via_grant,
        expires_at=result.expires_at.isoformat() if result.expires_at else None,
        request_id=str(result.request_id) if result.request_id else None,
    )


def _serialize(db: Session, r: AccessRequest, *, team=None, project=None, requester=None) -> AccessRequestOut:
    from app.models.document import Document
    from app.models.stage import Stage
    from app.services.access_requests_service import DOCUMENT_GRANT_TTL_HOURS, GRANT_TTL_DAYS

    team = team or db.get(Team, r.team_id)
    project = project or (db.get(Project, team.project_id) if team else None)
    requester = requester or db.get(User, r.user_id)
    requester_membership = _get_team_membership(db, r.user_id, r.team_id)

    if r.scope == AccessRequestScope.document:
        target_id = str(r.document_id)
        doc = db.get(Document, r.document_id)
        target_name = doc.original_filename if doc else "(deleted document)"
        grant_duration_label = f"{DOCUMENT_GRANT_TTL_HOURS} hours"
    elif r.scope == AccessRequestScope.stage:
        target_id = str(r.stage_id)
        stage = db.get(Stage, r.stage_id)
        target_name = stage.name if stage else "(deleted stage)"
        grant_duration_label = (
            _DURATION_LABELS[r.duration] if r.duration is not None
            else "Approver chooses tier & duration"
        )
    else:
        target_id = str(r.team_id)
        target_name = team.name if team else "(unknown)"
        grant_duration_label = f"{GRANT_TTL_DAYS} days"

    return AccessRequestOut(
        request_id=str(r.request_id),
        user_id=str(r.user_id),
        requester_email=requester.email if requester else None,
        requester_role=requester_membership.role.value if requester_membership else None,
        team_id=str(r.team_id),
        team_name=team.name if team else "(unknown)",
        project_id=str(team.project_id) if team else "",
        project_name=project.name if project else "(unknown)",
        scope=r.scope.value,
        target_id=target_id,
        target_name=target_name,
        tier=r.tier.value if r.tier else None,
        grant_duration_label=grant_duration_label,
        status=r.status.value,
        requested_at=r.requested_at.isoformat() if r.requested_at else None,
        decided_at=r.decided_at.isoformat() if r.decided_at else None,
        expires_at=r.expires_at.isoformat() if r.expires_at else None,
        active=(r.status == AccessRequestStatus.approved and not _is_expired(r)),
    )


@router.post("", response_model=AccessRequestOut, status_code=201)
def create_access_request(
    body: CreateAccessRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    try:
        req = request_confidential_access(
            db,
            user_id=identity.user_id,
            team_id=body.team_id,
            document_id=body.document_id,
            stage_id=body.stage_id,
            expected_tenant_id=identity.tenant_id,
        )
    except AccessRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    except ValueError as exc:  # zero or multiple targets given
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return _serialize(db, req, requester=db.get(User, identity.user_id))


@router.get("/mine", response_model=list[AccessRequestOut])
def my_access_requests(
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    rows = db.execute(
        select(AccessRequest)
        .where(AccessRequest.user_id == identity.user_id)
        .order_by(AccessRequest.requested_at.desc())
    ).scalars().all()
    out: list[AccessRequestOut] = []
    for r in rows:
        team = db.get(Team, r.team_id)
        project = db.get(Project, team.project_id) if team else None
        if team is None or project is None or project.tenant_id != identity.tenant_id:
            continue
        out.append(_serialize(db, r, team=team, project=project))
    return out


@router.get("/pending", response_model=list[AccessRequestOut])
def pending_access_requests(
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    rows = pending_requests_for_reviewer(
        db, reviewer_id=identity.user_id, tenant_id=identity.tenant_id
    )
    return [_serialize(db, r) for r in rows]


def _decide(
    db: Session, identity: ResolvedIdentity, request_id: uuid.UUID, *,
    approve: bool, tier: GrantTier | None = None, duration: GrantDuration | None = None,
) -> AccessRequestOut:
    r = db.get(AccessRequest, request_id)
    team = db.get(Team, r.team_id) if r else None
    project = db.get(Project, team.project_id) if team else None
    if r is None or team is None or project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Request not found")
    if not has_permission(
        db, identity.user_id, "approve_access_request", team.team_id, team.project_id
    ):
        raise HTTPException(
            status_code=403, detail=f"You cannot review access requests for team '{team.name}'"
        )
    if r.status != AccessRequestStatus.pending:
        raise HTTPException(status_code=409, detail=f"This request has already been {r.status.value}.")

    if approve and r.scope == AccessRequestScope.stage and (tier is None or duration is None):
        raise HTTPException(
            status_code=422,
            detail="Approving a stage-scope request requires both tier and duration.",
        )

    from app.services.access_requests_service import DOCUMENT_GRANT_TTL_HOURS

    now = datetime.now(timezone.utc)
    r.status = AccessRequestStatus.approved if approve else AccessRequestStatus.denied
    r.decided_by = identity.user_id
    r.decided_at = now
    if approve:
        if r.scope == AccessRequestScope.stage:
            r.tier = tier
            r.duration = duration
            r.expires_at = _EXPIRES_AT_FOR_DURATION[duration](now)
        else:
            r.expires_at = (
                now + timedelta(hours=DOCUMENT_GRANT_TTL_HOURS) if r.scope == AccessRequestScope.document
                else now + timedelta(days=_GRANT_TTL_DAYS)
            )
    else:
        r.expires_at = None
    record_audit(
        db, actor_id=identity.user_id,
        action="APPROVE_ACCESS_REQUEST" if approve else "DENY_ACCESS_REQUEST",
        resource_type="access_request", resource_id=r.request_id,
        details={"team": team.name, "requester_id": str(r.user_id)},
    )
    notify_access_request_decided(
        db, requester_id=r.user_id, team_name=team.name, project_id=project.project_id,
        request_id=r.request_id, approved=approve,
    )
    db.commit()
    db.refresh(r)
    return _serialize(db, r, team=team, project=project)


@router.post("/{request_id}/approve", response_model=AccessRequestOut)
def approve_access_request(
    request_id: uuid.UUID,
    body: ApproveAccessRequestBody = ApproveAccessRequestBody(),
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    return _decide(db, identity, request_id, approve=True, tier=body.tier, duration=body.duration)


@router.post("/{request_id}/deny", response_model=AccessRequestOut)
def deny_access_request(
    request_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    return _decide(db, identity, request_id, approve=False)


@router.post("/{request_id}/revoke", response_model=AccessRequestOut)
def revoke_access_request(
    request_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    """
    approved -> revoked, immediately and permanently (spec §5.3 — a later
    older grant for the same stage does NOT reactivate; see
    get_active_stage_grants_for_user()'s docstring). Row-locked via
    SELECT ... FOR UPDATE so two concurrent revoke calls can't both pass
    the status check and double-write the audit trail.
    """
    r = db.execute(
        select(AccessRequest).where(AccessRequest.request_id == request_id).with_for_update()
    ).scalar_one_or_none()
    team = db.get(Team, r.team_id) if r else None
    project = db.get(Project, team.project_id) if team else None
    if r is None or team is None or project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Request not found")
    if not has_permission(
        db, identity.user_id, "approve_access_request", team.team_id, team.project_id
    ):
        raise HTTPException(
            status_code=403, detail=f"You cannot review access requests for team '{team.name}'"
        )
    if r.status != AccessRequestStatus.approved:
        raise HTTPException(
            status_code=409, detail=f"This request is {r.status.value}, not approved — nothing to revoke."
        )

    r.status = AccessRequestStatus.revoked
    r.revoked_at = datetime.now(timezone.utc)
    r.revoked_by = identity.user_id
    record_audit(
        db, actor_id=identity.user_id, action="REVOKE_ACCESS_GRANT",
        resource_type="access_request", resource_id=r.request_id,
        details={"team": team.name, "requester_id": str(r.user_id)},
    )
    db.commit()
    db.refresh(r)
    return _serialize(db, r, team=team, project=project)
