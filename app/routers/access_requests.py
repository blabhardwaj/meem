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
    AccessRequestStatus,
    Team,
)
from app.models.user import User
from app.services.access_control import has_permission
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


class CreateAccessRequest(BaseModel):
    team_id: uuid.UUID


class AccessRequestOut(BaseModel):
    request_id: str
    user_id: str
    requester_email: str | None
    team_id: str
    team_name: str
    project_id: str
    project_name: str
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
    """
    provided = [x for x in (document_id, stage_id, team_id) if x is not None]
    if len(provided) != 1:
        raise HTTPException(
            status_code=422, detail="Exactly one of document_id, stage_id, team_id is required."
        )
    from app.services.access_control import resolve_effective_access

    result = resolve_effective_access(
        db, identity.user_id, document_id=document_id, stage_id=stage_id, team_id=team_id,
    )
    return EffectiveAccessOut(
        status=result.status,
        via_grant=result.via_grant,
        expires_at=result.expires_at.isoformat() if result.expires_at else None,
        request_id=str(result.request_id) if result.request_id else None,
    )


def _serialize(db: Session, r: AccessRequest, *, team=None, project=None, requester=None) -> AccessRequestOut:
    team = team or db.get(Team, r.team_id)
    project = project or (db.get(Project, team.project_id) if team else None)
    requester = requester or db.get(User, r.user_id)
    return AccessRequestOut(
        request_id=str(r.request_id),
        user_id=str(r.user_id),
        requester_email=requester.email if requester else None,
        team_id=str(r.team_id),
        team_name=team.name if team else "(unknown)",
        project_id=str(team.project_id) if team else "",
        project_name=project.name if project else "(unknown)",
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
            expected_tenant_id=identity.tenant_id,
        )
    except AccessRequestError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

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


def _decide(db: Session, identity: ResolvedIdentity, request_id: uuid.UUID, *, approve: bool) -> AccessRequestOut:
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

    now = datetime.now(timezone.utc)
    r.status = AccessRequestStatus.approved if approve else AccessRequestStatus.denied
    r.decided_by = identity.user_id
    r.decided_at = now
    r.expires_at = now + timedelta(days=_GRANT_TTL_DAYS) if approve else None
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
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    return _decide(db, identity, request_id, approve=True)


@router.post("/{request_id}/deny", response_model=AccessRequestOut)
def deny_access_request(
    request_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    return _decide(db, identity, request_id, approve=False)
