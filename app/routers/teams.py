"""
Team management within a project — creating the teams THEMSELVES (not assigning
users to them; that's /admin/assign-roles). Until now teams only ever existed
via the one seeded on project creation, or raw SQL — same gap stages had.

  GET  /projects/{project_id}/teams                    list this project's teams (+ member counts)
  GET  /projects/{project_id}/teams/{team_id}/members  list a team's members     (any project member)
  POST /projects/{project_id}/teams                    create a team              (project_admin+)

"project_admin+" = org_admin (tenant-wide) or a ProjectAdmin scope on this
project — the same gate as stage creation. A newly created team shows up
immediately in GET /workspace (which lists every team of a project for its
admins), so the "Assign Roles" modal's team dropdown picks it up with no extra
wiring.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db_with_tenant
from app.models.project import Project
from app.models.team import Team, UserTeamMembership
from app.models.user import User
from app.services.audit import record_audit
from app.services.auth import ResolvedIdentity

router = APIRouter(prefix="/projects", tags=["teams"])


class CreateTeamRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class TeamOut(BaseModel):
    team_id: str
    project_id: str
    name: str
    member_count: int


class TeamMemberOut(BaseModel):
    user_id: str
    name: str
    role: str


def _load_project(db: Session, identity: ResolvedIdentity, project_id: uuid.UUID) -> Project:
    project = db.get(Project, project_id)
    if project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def _load_team(db: Session, project_id: uuid.UUID, team_id: uuid.UUID) -> Team:
    team = db.get(Team, team_id)
    if team is None or team.project_id != project_id:
        raise HTTPException(status_code=404, detail="Team not found")
    return team


def _can_see_project(identity: ResolvedIdentity, project_id: uuid.UUID) -> bool:
    if identity.is_org_admin or project_id in identity.project_admin_project_ids:
        return True
    return any(m.project_id == project_id for m in identity.team_memberships)


def _require_project_admin(identity: ResolvedIdentity, project_id: uuid.UUID) -> None:
    if identity.is_org_admin or project_id in identity.project_admin_project_ids:
        return
    raise HTTPException(
        status_code=403,
        detail="Only project admins (or organization admins) can do this.",
    )


def _member_counts(db: Session, project_id: uuid.UUID) -> dict[uuid.UUID, int]:
    rows = db.execute(
        select(UserTeamMembership.team_id, func.count())
        .where(UserTeamMembership.project_id == project_id)
        .group_by(UserTeamMembership.team_id)
    ).all()
    return {tid: n for tid, n in rows}


def _serialize(team: Team, member_count: int) -> TeamOut:
    return TeamOut(
        team_id=str(team.team_id),
        project_id=str(team.project_id),
        name=team.name,
        member_count=member_count,
    )


@router.get("/{project_id}/teams", response_model=list[TeamOut])
def list_teams(
    project_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    _load_project(db, identity, project_id)
    if not _can_see_project(identity, project_id):
        raise HTTPException(status_code=403, detail="You do not have access to this project")
    counts = _member_counts(db, project_id)
    teams = db.execute(
        select(Team).where(Team.project_id == project_id).order_by(Team.name)
    ).scalars().all()
    return [_serialize(t, counts.get(t.team_id, 0)) for t in teams]


@router.get("/{project_id}/teams/{team_id}/members", response_model=list[TeamMemberOut])
def list_team_members(
    project_id: uuid.UUID,
    team_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    _load_project(db, identity, project_id)
    _load_team(db, project_id, team_id)
    if not _can_see_project(identity, project_id):
        raise HTTPException(status_code=403, detail="You do not have access to this project")

    rows = db.execute(
        select(UserTeamMembership, User.email)
        .join(User, User.user_id == UserTeamMembership.user_id)
        .where(
            UserTeamMembership.project_id == project_id,
            UserTeamMembership.team_id == team_id,
        )
        .order_by(User.email)
    ).all()
    return [
        TeamMemberOut(user_id=str(m.user_id), name=email, role=m.role.value)
        for m, email in rows
    ]


@router.post("/{project_id}/teams", response_model=TeamOut, status_code=201)
def create_team(
    project_id: uuid.UUID,
    body: CreateTeamRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    _load_project(db, identity, project_id)
    _require_project_admin(identity, project_id)

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Team name cannot be empty")

    existing = db.execute(
        select(Team).where(Team.project_id == project_id)
    ).scalars().all()
    if any(t.name.lower() == name.lower() for t in existing):
        raise HTTPException(status_code=409, detail=f"A team named '{name}' already exists in this project")

    team = Team(project_id=project_id, name=name)
    db.add(team)
    db.flush()
    record_audit(
        db, actor_id=identity.user_id, action="CREATE_TEAM", resource_type="team",
        resource_id=team.team_id, details={"project_id": str(project_id), "name": name},
    )
    db.commit()
    db.refresh(team)
    return _serialize(team, 0)
