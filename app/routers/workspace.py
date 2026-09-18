"""
Phase 6: read-only workspace context for the frontend.

The frontend needs project / team / stage NAMES and IDs to render its
dropdowns and tables — GET /auth/me only returns bare IDs. This endpoint
assembles, for the authenticated user, the projects they can act in, each with
the teams they may act as and that project's stages.

Read-only, scoped to the caller's memberships (+ project_admin scopes,
+ everything in the tenant for an org_admin).
"""

import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db_with_tenant
from app.models.project import Project
from app.models.stage import Stage, StageReference, TeamStageAccess
from app.models.team import Team
from app.services.access_control import get_accessible_stages_for_user
from app.services.auth import ResolvedIdentity

router = APIRouter(prefix="/workspace", tags=["workspace"])


class TeamOut(BaseModel):
    team_id: str
    name: str
    role: str  # the caller's effective role on this team


class StageOut(BaseModel):
    stage_id: str
    name: str
    order_index: int
    # Whether the caller has team_stage_access to this specific stage (or
    # bypasses via org_admin/project_admin) — mirrors app/routers/stages.py's
    # StageOut.has_access exactly (this is a second, duplicate serialization
    # of the same Stage rows for the frontend's workspace bootstrap call).
    # A stage's NAME is visible to every project member regardless; when
    # false, the fields below are deliberately zeroed, not the real values.
    has_access: bool = True
    requires_approval: bool
    references: list[str] = []  # stage_ids this stage references (one-way)
    team_access: list[str] = []  # team_ids granted access to this stage


class ProjectOut(BaseModel):
    project_id: str
    name: str
    teams: list[TeamOut]
    stages: list[StageOut]


class WorkspaceResponse(BaseModel):
    user_id: str
    email: str
    is_org_admin: bool
    projects: list[ProjectOut]


@router.get("", response_model=WorkspaceResponse)
def get_workspace(
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    # Which projects can this user act in?
    project_ids: set[uuid.UUID] = set(identity.project_admin_project_ids)
    project_ids.update(m.project_id for m in identity.team_memberships)
    if identity.is_org_admin:
        project_ids.update(
            db.execute(
                select(Project.project_id).where(Project.tenant_id == identity.tenant_id)
            ).scalars()
        )

    memberships_by_project: dict[uuid.UUID, list] = {}
    for m in identity.team_memberships:
        memberships_by_project.setdefault(m.project_id, []).append(m)

    projects_out: list[ProjectOut] = []
    for pid in project_ids:
        project = db.get(Project, pid)
        if project is None or project.tenant_id != identity.tenant_id:
            continue

        all_teams = db.execute(
            select(Team).where(Team.project_id == pid)
        ).scalars().all()
        teams_by_id = {t.team_id: t for t in all_teams}

        admin_here = identity.is_org_admin or pid in identity.project_admin_project_ids
        if admin_here:
            admin_role = "org_admin" if identity.is_org_admin else "project_admin"
            teams_out = [
                TeamOut(team_id=str(t.team_id), name=t.name, role=admin_role)
                for t in all_teams
            ]
        else:
            teams_out = [
                TeamOut(
                    team_id=str(m.team_id),
                    name=teams_by_id[m.team_id].name if m.team_id in teams_by_id else "(unknown team)",
                    role=m.role.value,
                )
                for m in memberships_by_project.get(pid, [])
            ]

        # ENFORCEMENT POINT B: every active stage in the project is returned
        # to every project member -- a stage's name/existence is not
        # privileged, only its contents/roster are. has_access (per stage)
        # reflects team_stage_access (ANY of the caller's team memberships)
        # union'd with any per-user stage grant, via
        # get_accessible_stages_for_user(); org_admin/project_admin bypass
        # entirely. references/team_access are zeroed below for any stage
        # the caller lacks access to, so this never leaks a locked stage's
        # roster/references -- only that it exists, letting the frontend
        # offer a "request access" affordance for it. Mirrors
        # app/routers/stages.py's list_stages() exactly -- this endpoint is
        # a second, duplicate serialization of the same Stage rows for the
        # frontend's workspace bootstrap call.
        accessible_ids = set(get_accessible_stages_for_user(db, identity.user_id, pid))
        all_stages = db.execute(
            select(Stage)
            .where(Stage.project_id == pid, Stage.deleted_at.is_(None))
            .order_by(Stage.order_index)
        ).scalars().all()
        accessible_stages = [s for s in all_stages if s.stage_id in accessible_ids]
        refs_by_stage: dict[uuid.UUID, list[str]] = {}
        for sr in db.execute(
            select(StageReference).where(
                StageReference.stage_id.in_([s.stage_id for s in accessible_stages])
            )
        ).scalars():
            refs_by_stage.setdefault(sr.stage_id, []).append(str(sr.references_stage_id))
        team_access_by_stage: dict[uuid.UUID, list[str]] = {}
        for ta in db.execute(
            select(TeamStageAccess).where(
                TeamStageAccess.stage_id.in_([s.stage_id for s in accessible_stages])
            )
        ).scalars():
            team_access_by_stage.setdefault(ta.stage_id, []).append(str(ta.team_id))

        projects_out.append(ProjectOut(
            project_id=str(pid),
            name=project.name,
            teams=teams_out,
            stages=[
                StageOut(
                    stage_id=str(s.stage_id),
                    name=s.name,
                    order_index=s.order_index,
                    has_access=(s.stage_id in accessible_ids),
                    requires_approval=s.requires_approval if s.stage_id in accessible_ids else False,
                    references=refs_by_stage.get(s.stage_id, []) if s.stage_id in accessible_ids else [],
                    team_access=team_access_by_stage.get(s.stage_id, []) if s.stage_id in accessible_ids else [],
                )
                for s in all_stages
            ],
        ))

    projects_out.sort(key=lambda p: p.name.lower())
    return WorkspaceResponse(
        user_id=str(identity.user_id),
        email=identity.email,
        is_org_admin=identity.is_org_admin,
        projects=projects_out,
    )
