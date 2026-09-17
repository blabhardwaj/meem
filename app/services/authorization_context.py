"""
Request-scoped authorization context for DocFlow AI.

Eliminates N+1 query loops across ABAC and RAG retrieval by pre-loading:
  - User identity, tenant_id, clearance, org-admin status
  - Project-admin status
  - All team memberships and roles for (user_id, project_id)
  - All accessible stage IDs (native TeamStageAccess + active stage grants)
  - Active confidential access grants for the user's teams/documents
  - Active stage grant tiers, for confidential unlock within a stage grant

Authoritative decisions remain in PostgreSQL. This context is strictly internal
and NEVER exposed as an argument to LLMs.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import (
    AccessRequest,
    AccessRequestScope,
    AccessRequestStatus,
    GrantTier,
    ProjectAdmin,
    TeamRole,
    UserTeamMembership,
)
from app.models.user import User


@dataclass
class AuthorizationContext:
    user_id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID

    is_org_admin: bool
    is_project_admin: bool

    team_ids: set[uuid.UUID] = field(default_factory=set)
    team_roles: dict[uuid.UUID, TeamRole] = field(default_factory=dict)

    accessible_stage_ids: set[uuid.UUID] = field(default_factory=set)
    clearance_level: SensitivityLevel | str | None = None
    active_confidential_grant_team_ids: set[uuid.UUID] = field(default_factory=set)
    active_confidential_grant_document_ids: set[uuid.UUID] = field(default_factory=set)
    # stage_id -> GrantTier for every active stage grant this user holds in
    # this project. Replaces the old tier-blind active_confidential_grant_stage_ids
    # set — confidential unlock now requires tier == contributor_confidential,
    # checked by the caller (classify_documents_visibility), not here.
    stage_grant_tiers: dict[uuid.UUID, GrantTier] = field(default_factory=dict)

    @property
    def is_admin(self) -> bool:
        return self.is_org_admin or self.is_project_admin


def build_authorization_context(
    db: Session,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
) -> AuthorizationContext:
    """
    Constructs a request-scoped AuthorizationContext for (user_id, project_id)
    in a small, bounded number of queries (no N+1 over documents).
    """
    user = db.get(User, user_id)
    if not user:
        raise ValueError(f"User {user_id} does not exist.")

    tenant_id = user.tenant_id
    is_org_admin = bool(user.is_org_admin)
    clearance = getattr(user, "clearance_level", None)

    # 1. Project Admin check
    is_project_admin = db.execute(
        select(ProjectAdmin.id).where(
            ProjectAdmin.user_id == user_id,
            ProjectAdmin.project_id == project_id,
        ).limit(1)
    ).scalar_one_or_none() is not None

    # 2. Team memberships within this project
    memberships = db.execute(
        select(UserTeamMembership).where(
            UserTeamMembership.user_id == user_id,
            UserTeamMembership.project_id == project_id,
        )
    ).scalars().all()

    team_ids = {m.team_id for m in memberships}
    team_roles = {m.team_id: m.role for m in memberships}

    # 3. Accessible stages — native TeamStageAccess, plus stage grants of
    # any tier (imported lazily to avoid a circular import with
    # access_control.py, which imports AuthorizationContext from here).
    from app.services.access_control import get_active_stage_grants_for_user

    stage_grant_tiers: dict[uuid.UUID, GrantTier] = {}
    if is_org_admin or is_project_admin:
        stage_rows = db.execute(
            select(Stage.stage_id).where(
                Stage.project_id == project_id,
                Stage.deleted_at.is_(None),
            )
        ).scalars().all()
        accessible_stage_ids = set(stage_rows)
    else:
        accessible_stage_ids = set()
        if team_ids:
            stage_rows = db.execute(
                select(TeamStageAccess.stage_id)
                .join(Stage, Stage.stage_id == TeamStageAccess.stage_id)
                .where(
                    TeamStageAccess.team_id.in_(team_ids),
                    Stage.deleted_at.is_(None),
                )
                .distinct()
            ).scalars().all()
            accessible_stage_ids = set(stage_rows)

        stage_grants = get_active_stage_grants_for_user(db, user_id)
        if stage_grants:
            grant_stage_ids_in_project = set(db.execute(
                select(Stage.stage_id).where(
                    Stage.stage_id.in_(stage_grants.keys()),
                    Stage.project_id == project_id,
                    Stage.deleted_at.is_(None),
                )
            ).scalars())
            accessible_stage_ids |= grant_stage_ids_in_project
            stage_grant_tiers = {
                sid: grant.tier for sid, grant in stage_grants.items()
                if sid in grant_stage_ids_in_project
            }

    # 4. Active non-expired confidential grants of document/team scope for
    # this user — stage-scope grants are handled entirely above via
    # stage_grant_tiers, which is tier-aware and revocation-safe in a way
    # this simpler per-row scan is not (see get_active_stage_grants_for_user's
    # docstring for why a plain "status == approved" scan is insufficient).
    active_confidential_grant_team_ids = set()
    active_confidential_grant_document_ids = set()
    if not (is_org_admin or is_project_admin):
        grants = db.execute(
            select(AccessRequest).where(
                AccessRequest.user_id == user_id,
                AccessRequest.status == AccessRequestStatus.approved,
            )
        ).scalars().all()

        now = datetime.now(timezone.utc)

        def _is_active(g: AccessRequest) -> bool:
            if g.expires_at is None:
                return True
            exp = g.expires_at if g.expires_at.tzinfo else g.expires_at.replace(tzinfo=timezone.utc)
            return exp >= now

        for g in grants:
            if not _is_active(g):
                continue
            if g.scope == AccessRequestScope.team and g.team_id in team_ids:
                active_confidential_grant_team_ids.add(g.team_id)
            elif g.scope == AccessRequestScope.document and g.document_id is not None:
                active_confidential_grant_document_ids.add(g.document_id)

    return AuthorizationContext(
        user_id=user_id,
        tenant_id=tenant_id,
        project_id=project_id,
        is_org_admin=is_org_admin,
        is_project_admin=is_project_admin,
        team_ids=team_ids,
        team_roles=team_roles,
        accessible_stage_ids=accessible_stage_ids,
        clearance_level=clearance,
        active_confidential_grant_team_ids=active_confidential_grant_team_ids,
        active_confidential_grant_document_ids=active_confidential_grant_document_ids,
        stage_grant_tiers=stage_grant_tiers,
    )
