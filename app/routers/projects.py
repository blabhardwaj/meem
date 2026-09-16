"""
Phase 6: project listing + creation (adapted from the teammate's
GET/POST /projects in backend/app/api/search.py).

Visibility:
  - org_admin: every project in their tenant (and only org_admin may create)
  - everyone else: projects where they hold a project_admin scope OR any team
    membership

Creating a project also seeds a small default stage list and one "Engineering"
team so the project is immediately usable for uploads and invites.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db_with_tenant
from app.models.document import Document
from app.models.project import Project
from app.models.stage import Stage
from app.models.team import ProjectAdmin, Team, UserTeamMembership
from app.models.user import User
from app.services.audit import record_audit
from app.services.auth import ResolvedIdentity

router = APIRouter(prefix="/projects", tags=["projects"])

_DEFAULT_STAGES = ["Requirements", "Design", "Development", "Testing"]
_DEFAULT_TEAM = "Engineering"


class CreateProjectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=2000)


class ProjectMember(BaseModel):
    user_id: str
    name: str
    username: str
    role: str
    team_name: str | None


class ProjectSummary(BaseModel):
    project_id: str
    project_name: str
    description: str | None
    document_count: int
    created_at: str | None
    members: list[ProjectMember]
    assigned_teams: list[str]


def _visible_project_ids(db: Session, identity: ResolvedIdentity) -> set[uuid.UUID]:
    ids: set[uuid.UUID] = set(identity.project_admin_project_ids)
    ids.update(m.project_id for m in identity.team_memberships)
    if identity.is_org_admin:
        ids.update(db.execute(
            select(Project.project_id).where(Project.tenant_id == identity.tenant_id)
        ).scalars())
    return ids


def _summarize(db: Session, project: Project) -> ProjectSummary:
    doc_count = db.execute(
        select(func.count()).select_from(Document).where(Document.project_id == project.project_id)
    ).scalar_one()

    teams = {t.team_id: t.name for t in db.execute(
        select(Team).where(Team.project_id == project.project_id)
    ).scalars()}

    members: list[ProjectMember] = []
    for m, email in db.execute(
        select(UserTeamMembership, User.email)
        .join(User, User.user_id == UserTeamMembership.user_id)
        .where(UserTeamMembership.project_id == project.project_id)
    ).all():
        members.append(ProjectMember(
            user_id=str(m.user_id), name=email, username=email,
            role=m.role.value, team_name=teams.get(m.team_id),
        ))
    for a, email in db.execute(
        select(ProjectAdmin, User.email)
        .join(User, User.user_id == ProjectAdmin.user_id)
        .where(ProjectAdmin.project_id == project.project_id)
    ).all():
        members.append(ProjectMember(
            user_id=str(a.user_id), name=email, username=email,
            role="project_admin", team_name=None,
        ))

    return ProjectSummary(
        project_id=str(project.project_id),
        project_name=project.name,
        description=project.description,
        document_count=doc_count,
        created_at=project.created_at.isoformat() if project.created_at else None,
        members=members,
        assigned_teams=sorted(teams.values()),
    )


@router.get("", response_model=list[ProjectSummary])
def list_projects(
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    out = []
    for pid in _visible_project_ids(db, identity):
        project = db.get(Project, pid)
        if project is not None and project.tenant_id == identity.tenant_id:
            out.append(_summarize(db, project))
    out.sort(key=lambda p: p.project_name.lower())
    return out


@router.post("", response_model=ProjectSummary, status_code=201)
def create_project(
    body: CreateProjectRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    if not identity.is_org_admin:
        raise HTTPException(status_code=403, detail="Only organization admins can create projects")
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Project name cannot be empty")

    project = Project(tenant_id=identity.tenant_id, name=name, description=body.description)
    db.add(project)
    db.flush()

    for i, stage_name in enumerate(_DEFAULT_STAGES):
        db.add(Stage(project_id=project.project_id, name=stage_name, order_index=i))
    db.add(Team(project_id=project.project_id, name=_DEFAULT_TEAM))

    record_audit(
        db, actor_id=identity.user_id, action="CREATE_PROJECT", resource_type="project",
        resource_id=project.project_id, details={"name": name},
    )
    db.commit()
    db.refresh(project)
    return _summarize(db, project)
