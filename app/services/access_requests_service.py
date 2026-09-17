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
    AccessRequestScope,
    AccessRequestStatus,
    Team,
    TeamRole,
    UserTeamMembership,
)
from app.services.access_control import _is_org_admin, _is_project_admin, _get_team_membership
from app.services.audit import record_audit
from app.services.notifications import notify_access_request_created

GRANT_TTL_DAYS = 90
DOCUMENT_GRANT_TTL_HOURS = 72


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


def active_stage_grants_for_reviewer(
    db: Session,
    *,
    reviewer_id: uuid.UUID,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID | None = None,
) -> list[AccessRequest]:
    """
    Every currently-active (approved, unexpired), stage-scope grant
    `reviewer_id` is allowed to revoke — same tenant/team-permission gate as
    pending_requests_for_reviewer(), but status == approved and not expired,
    scope == stage. Only stage-scope grants have a tier/duration and a
    working Revoke action in the UI (document/team-scope grants keep the
    original always-read-only, fixed-TTL behavior with no revoke path).
    Newest-decided first. Optionally narrowed to one project.
    """
    from app.services.access_control import has_permission  # avoid import cycle

    now = datetime.now(timezone.utc)
    rows = db.execute(
        select(AccessRequest)
        .where(
            AccessRequest.status == AccessRequestStatus.approved,
            AccessRequest.scope == AccessRequestScope.stage,
        )
        .order_by(AccessRequest.decided_at.desc())
    ).scalars().all()

    out: list[AccessRequest] = []
    for r in rows:
        if r.expires_at is not None:
            exp = r.expires_at if r.expires_at.tzinfo else r.expires_at.replace(tzinfo=timezone.utc)
            if exp < now:
                continue
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


def _derive_document_team(db: Session, document_id: uuid.UUID) -> uuid.UUID | None:
    from app.models.document import Document
    document = db.get(Document, document_id)
    return document.uploaded_as_team_id if document else None


def _derive_stage_team(db: Session, user_id: uuid.UUID, stage_id: uuid.UUID) -> uuid.UUID | None:
    """
    The team responsible for approving a stage-scoped request: among the
    teams with TeamStageAccess to this stage, the one the requester
    themselves already belongs to (viewer/contributor), earliest grant
    first if more than one qualifies. team_id here is approval-routing
    metadata only — it is never part of the stage request's own identity
    (that's stage_id alone; dedup/effective-state resolution is keyed by
    stage_id, not by this derived team_id).
    """
    from app.models.stage import TeamStageAccess

    access_rows = db.execute(
        select(TeamStageAccess)
        .where(TeamStageAccess.stage_id == stage_id)
        .order_by(TeamStageAccess.created_at.asc())
    ).scalars().all()
    for row in access_rows:
        if _get_team_membership(db, user_id, row.team_id) is not None:
            return row.team_id
    return None


def request_confidential_access(
    db: Session,
    *,
    user_id: uuid.UUID,
    team_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    stage_id: uuid.UUID | None = None,
    expected_tenant_id: uuid.UUID | None = None,
) -> AccessRequest:
    """
    Create a pending confidential-access request for `user_id`, scoped to
    exactly one of team_id/document_id/stage_id (the caller passes exactly
    one; this is the same shape app/tools/rag_tools.py's existing tool call
    already uses for team_id, so that call site needs no changes).

    For document/stage scope, `team_id` is NEVER accepted from the caller —
    it is derived server-side from the target, per the spec's trust
    boundary (a client can request access to a target, never nominate which
    team approves it).

    Commits on success and returns the fresh AccessRequest row.

    Raises:
        AccessRequestError — team/target unknown or cross-tenant (404), not
        a member of the resolved team (403), already cleared or a live
        request already exists for this exact target (409).
    """
    from app.models.team import AccessRequestScope

    provided = [x for x in (team_id, document_id, stage_id) if x is not None]
    if len(provided) != 1:
        raise ValueError("Exactly one of team_id, document_id, stage_id must be given")

    # TEMPORARY POLICY RESTRICTION (2026-09-17): only stage-scope requests are
    # currently permitted — document- and team-scope requests are rejected
    # here even though their full implementation (below, and in
    # _derive_document_team / the team-scope branch) is left completely
    # intact for when this restriction is lifted. Enforced at this single
    # choke point rather than only in the UI so a direct API/tool call can't
    # bypass it either.
    if document_id is not None or team_id is not None:
        raise AccessRequestError(
            "Only stage-level confidential-access requests are currently supported.",
            status_code=403,
        )

    if document_id is not None:
        scope = AccessRequestScope.document
        resolved_team_id = _derive_document_team(db, document_id)
        if resolved_team_id is None:
            raise AccessRequestError("Document not found", status_code=404)
    elif stage_id is not None:
        scope = AccessRequestScope.stage
        resolved_team_id = _derive_stage_team(db, user_id, stage_id)
        if resolved_team_id is None:
            raise AccessRequestError("You are not a member of any team with access to this stage", status_code=403)
    else:
        scope = AccessRequestScope.team
        resolved_team_id = team_id

    team = db.get(Team, resolved_team_id)
    project = db.get(Project, team.project_id) if team else None
    if team is None or project is None:
        raise AccessRequestError("Team not found", status_code=404)
    if expected_tenant_id is not None and project.tenant_id != expected_tenant_id:
        raise AccessRequestError("Team not found", status_code=404)

    is_org_admin = _is_org_admin(db, user_id)
    is_proj_admin = _is_project_admin(db, user_id, project.project_id)
    membership: UserTeamMembership | None = _get_team_membership(db, user_id, resolved_team_id)

    if membership is None and not is_proj_admin and not is_org_admin:
        raise AccessRequestError("You are not a member of this team", status_code=403)

    if is_org_admin or is_proj_admin or (
        membership is not None and membership.role == TeamRole.team_lead
    ):
        raise AccessRequestError(
            "You already have confidential access on this team.", status_code=409
        )

    if scope == AccessRequestScope.stage:
        # Stage-scope dedup (spec §5.1): only a duplicate PENDING request is
        # blocked here — an existing ACTIVE grant (any tier) no longer blocks
        # a fresh request, since the whole point of tiers is requesting an
        # upgrade without waiting for the current grant to expire. Task 1's
        # partial unique index (uq_access_requests_one_pending_stage_request)
        # backs this up against a race between two simultaneous requests.
        existing_pending = db.execute(
            select(AccessRequest).where(
                AccessRequest.user_id == user_id,
                AccessRequest.scope == AccessRequestScope.stage,
                AccessRequest.stage_id == stage_id,
                AccessRequest.status == AccessRequestStatus.pending,
            )
        ).scalar_one_or_none()
        if existing_pending is not None:
            raise AccessRequestError(
                "You already have a pending confidential-access request for this target.",
                status_code=409,
            )
    else:
        from app.services.access_control import resolve_effective_access
        effective = resolve_effective_access(
            db, user_id,
            document_id=document_id if scope == AccessRequestScope.document else None,
            team_id=resolved_team_id if scope == AccessRequestScope.team else None,
        )
        if effective.status in ("granted", "pending"):
            raise AccessRequestError(
                f"You already have a {effective.status} confidential-access request for this target.",
                status_code=409,
            )

    req = AccessRequest(
        user_id=user_id, team_id=resolved_team_id, scope=scope,
        document_id=document_id, stage_id=stage_id,
        status=AccessRequestStatus.pending,
    )
    db.add(req)
    db.flush()
    record_audit(
        db,
        actor_id=user_id,
        action="REQUEST_CONFIDENTIAL_ACCESS",
        resource_type="team",
        resource_id=team.team_id,
        details={"team": team.name, "project": project.name, "scope": scope.value},
    )
    notify_access_request_created(
        db, team_id=team.team_id, team_name=team.name, project_id=project.project_id,
        requester_id=user_id, request_id=req.request_id,
    )
    db.commit()
    db.refresh(req)
    return req
