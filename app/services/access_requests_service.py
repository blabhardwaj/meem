"""
Confidential-access request workflow — the service logic, shared by every
entry point that can create one:

  * POST /access-requests           (app/routers/access_requests.py) — the
    direct UI action / button.
  * request_confidential_access tool (app/tools/rag_tools.py) — the RAG Agent
    offering to request access on the user's behalf after retrieval or a
    summary was blocked by sensitivity, and the user saying yes.

Both call request_confidential_access() below — there is exactly one
implementation of "who may request, when it's a duplicate, what gets
audited". The router maps AccessRequestError.status_code onto an
HTTPException; the tool relays AccessRequestError.message as a plain string.

This is the request side of the "contributor with an active access grant"
exception in app/services/access_control.py: a viewer/contributor blocked
from confidential content asks the team's lead for clearance; once approved
they get a 90-day grant that can_view_document() honours.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.project import Project
from app.models.team import (
    AccessRequest,
    AccessRequestStatus,
    Team,
    TeamRole,
    UserTeamMembership,
)
from app.services.access_control import _is_org_admin, _is_project_admin, _get_team_membership
from app.services.audit import record_audit
from app.services.notifications import notify_access_request_created

GRANT_TTL_DAYS = 90


class AccessRequestError(Exception):
    """
    A request that can't be created — not a member of the team, already
    cleared, or a duplicate. `status_code` is what the HTTP router should
    return; `message` is safe to show a user verbatim.
    """

    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def is_expired(r: AccessRequest) -> bool:
    return bool(r.expires_at and r.expires_at < datetime.now(timezone.utc))


def live_request(db: Session, user_id: uuid.UUID, team_id: uuid.UUID) -> AccessRequest | None:
    """The caller's current pending — or approved-and-still-valid — request for a team."""
    rows = db.execute(
        select(AccessRequest)
        .where(AccessRequest.user_id == user_id, AccessRequest.team_id == team_id)
        .order_by(AccessRequest.requested_at.desc())
    ).scalars().all()
    for r in rows:
        if r.status == AccessRequestStatus.pending:
            return r
        if r.status == AccessRequestStatus.approved and not is_expired(r):
            return r
    return None


def pending_requests_for_reviewer(
    db: Session,
    *,
    reviewer_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
) -> list[AccessRequest]:
    """
    Every pending confidential-access request `reviewer_id` is allowed to
    decide: status == pending, same tenant, and the reviewer passes
    has_permission(..., "approve_access_request", ...) on that request's team.

    This is the exact query behind GET /access-requests/pending — shared so
    the Query Agent's list_pending_approvals tool reports precisely what the
    real Pending Approvals tab shows. Newest first. Optionally narrowed to one
    project.
    """
    from app.services.access_control import has_permission  # avoid import cycle

    rows = db.execute(
        select(AccessRequest)
        .where(AccessRequest.status == AccessRequestStatus.pending)
        .order_by(AccessRequest.requested_at.desc())
    ).scalars().all()

    out: list[AccessRequest] = []
    for r in rows:
        team = db.get(Team, r.team_id)
        project = db.get(Project, team.project_id) if team else None
        if team is None or project is None or project.tenant_id != tenant_id:
            continue
        if project_id is not None and project.project_id != project_id:
            continue
        if not has_permission(
            db, reviewer_id, "approve_access_request", team.team_id, team.project_id
        ):
            continue
        out.append(r)
    return out


def request_confidential_access(
    db: Session,
    *,
    user_id: uuid.UUID,
    team_id: uuid.UUID,
    expected_tenant_id: uuid.UUID | None = None,
) -> AccessRequest:
    """
    Create a pending confidential-access request for `user_id` on `team_id`.

    Commits on success and returns the fresh AccessRequest row.

    Raises:
        AccessRequestError — team unknown / cross-tenant (404), not a member
        of the team (403), already cleared or a live request already exists
        (409).
    """
    team = db.get(Team, team_id)
    project = db.get(Project, team.project_id) if team else None
    if team is None or project is None:
        raise AccessRequestError("Team not found", status_code=404)
    if expected_tenant_id is not None and project.tenant_id != expected_tenant_id:
        raise AccessRequestError("Team not found", status_code=404)

    is_org_admin = _is_org_admin(db, user_id)
    is_proj_admin = _is_project_admin(db, user_id, project.project_id)
    membership: UserTeamMembership | None = _get_team_membership(db, user_id, team_id)

    if membership is None and not is_proj_admin and not is_org_admin:
        raise AccessRequestError("You are not a member of this team", status_code=403)

    # team_lead / project_admin / org_admin already see confidential automatically.
    if is_org_admin or is_proj_admin or (
        membership is not None and membership.role == TeamRole.team_lead
    ):
        raise AccessRequestError(
            "You already have confidential access on this team.", status_code=409
        )

    existing = live_request(db, user_id, team_id)
    if existing is not None:
        raise AccessRequestError(
            f"You already have a {existing.status.value} confidential-access "
            f"request for this team.",
            status_code=409,
        )

    req = AccessRequest(
        user_id=user_id, team_id=team_id, status=AccessRequestStatus.pending
    )
    db.add(req)
    db.flush()
    record_audit(
        db,
        actor_id=user_id,
        action="REQUEST_CONFIDENTIAL_ACCESS",
        resource_type="team",
        resource_id=team.team_id,
        details={"team": team.name, "project": project.name},
    )
    notify_access_request_created(
        db, team_id=team.team_id, team_name=team.name, project_id=project.project_id,
        requester_id=user_id, request_id=req.request_id,
    )
    db.commit()
    db.refresh(req)
    return req
