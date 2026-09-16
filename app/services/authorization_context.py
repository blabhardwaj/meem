"""
Request-scoped authorization context for DocFlow AI.

Eliminates N+1 query loops across ABAC and RAG retrieval by pre-loading:
  - User identity, tenant_id, clearance, org-admin status
  - Project-admin status
  - All team memberships and roles for (user_id, project_id)
  - All accessible stage IDs
  - Active confidential access grants for the user's teams

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
    active_confidential_grant_stage_ids: set[uuid.UUID] = field(default_factory=set)

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
    in a small, bounded number of queries (maximum 4).
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

    # 3. Accessible stages
    if is_org_admin or is_project_admin:
        stage_rows = db.execute(
            select(Stage.stage_id).where(
                Stage.project_id == project_id,
                Stage.deleted_at.is_(None),
            )
        ).scalars().all()
        accessible_stage_ids = set(stage_rows)
    elif team_ids:
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
    else:
        accessible_stage_ids = set()

    # 4. Active non-expired confidential grants of ANY scope for this user —
    # one query, partitioned in memory by scope, mirroring
    # resolve_effective_access()'s single-target query shape but as a bulk
    # precompute for the N-document batch case (classify_documents_visibility
    # below) so this stays the "no N+1 queries" path its own docstring
    # promises. Team-scope grants are still narrowed to this project's teams;
    # document/stage-scope grants are fetched unfiltered by team since a
    # grant's own document_id/stage_id is the only thing that needs to match
    # later, per-document, in classify_documents_visibility.
    active_confidential_grant_team_ids = set()
    active_confidential_grant_document_ids = set()
    active_confidential_grant_stage_ids = set()
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
            elif g.scope == AccessRequestScope.stage and g.stage_id is not None:
                active_confidential_grant_stage_ids.add(g.stage_id)

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
        active_confidential_grant_stage_ids=active_confidential_grant_stage_ids,
    )
