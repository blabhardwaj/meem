"""
Phase 6: user directory + role assignment/editing + the audit-log read endpoint.
Refitted from the teammate's /admin/* routes to our per-team model.

  GET    /admin/users                          directory + each user's full access
  POST   /admin/users                          create a bare org user (org_admin only)
  POST   /admin/assign-roles                   add access:
                                                 mode=team_member  -> UserTeamMembership row(s),
                                                   a per-team role each (team_assignments=[{team_id,role}]).
                                                   Gated per team by has_permission("manage_team_members").
                                                 mode=project_admin -> ProjectAdmin row(s). ORG_ADMIN ONLY.
                                                 mode=org_admin      -> User.is_org_admin = True. ORG_ADMIN ONLY.
                                               New users get tenant_id from the target project(s) — never
                                               DEFAULT_SIGNUP_TENANT_ID.
  PATCH  /admin/team-membership/{id}           change one membership's role (manage_team_members)
  DELETE /admin/team-membership/{id}           delete one membership (manage_team_members)
  DELETE /admin/project-admin/{id}             remove a ProjectAdmin scope (org_admin only)
  POST   /admin/revoke-org-admin               revoke is_org_admin (org_admin only) — cannot revoke
                                               your own, nor the last org_admin in the tenant.
  POST   /admin/project-access                 legacy single-team assign (kept for compatibility)
  POST   /admin/access                         legacy grant-org-admin (kept for compatibility)
  GET    /admin/audit-log                      tenant-wide audit rows (org_admin only)
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user
from app.config import DEFAULT_NEW_USER_PASSWORD
from app.database import get_db
from app.models.project import Project
from app.models.team import ProjectAdmin, Team, TeamRole, UserTeamMembership
from app.models.user import User
from app.services.access_control import has_permission
from app.services.audit import AuditAccessError, list_audit_log, record_audit
from app.services.auth import ResolvedIdentity, hash_password

router = APIRouter(prefix="/admin", tags=["admin"])

_ROLE_ALIASES = {"member": "contributor"}
_TEAM_ROLES = {"viewer", "contributor", "team_lead"}


# --- request / response models ------------------------------------------------

class CreateUserRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    full_name: str | None = Field(default=None, max_length=255)


class OrgAdminRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)


class TeamAssignment(BaseModel):
    team_id: uuid.UUID
    role: str  # viewer | contributor | team_lead (| member alias)


class AssignRolesRequest(BaseModel):
    email: str = Field(min_length=5, max_length=254)
    mode: str  # team_member | project_admin | org_admin
    full_name: str | None = Field(default=None, max_length=255)  # only used if the user is new
    team_assignments: list[TeamAssignment] = Field(default_factory=list)
    project_ids: list[uuid.UUID] = Field(default_factory=list)
    all_projects: bool = False


class RoleUpdateRequest(BaseModel):
    role: str  # viewer | contributor | team_lead


class RevokeOrgAdminRequest(BaseModel):
    user_id: uuid.UUID


class ProjectAccessRequest(BaseModel):  # legacy
    email: str = Field(min_length=5, max_length=254)
    role: str
    team_id: uuid.UUID | None = None
    project_id: uuid.UUID | None = None
    team_name: str | None = Field(default=None, max_length=255)


class TeamMembershipOut(BaseModel):
    membership_id: str
    team_id: str
    team_name: str
    project_id: str
    project_name: str
    role: str


class ProjectAdminOut(BaseModel):
    id: str
    project_id: str
    project_name: str


class AdminUser(BaseModel):
    user_id: str
    username: str
    full_name: str
    team_name: str | None
    is_org_admin: bool
    roles: list[dict]  # [{project_id, role}] — kept for the flat list's expand table
    team_memberships: list[TeamMembershipOut]
    project_admin: list[ProjectAdminOut]


# --- helpers ---------------------------------------------------------------

def _require_directory_access(identity: ResolvedIdentity) -> None:
    if identity.is_org_admin:
        return
    manages = any(m.role == TeamRole.team_lead for m in identity.team_memberships) \
        or bool(identity.project_admin_project_ids)
    if not manages:
        raise HTTPException(status_code=403, detail="You do not have user-management access")


def _all_users(db: Session, tenant_id: uuid.UUID) -> list[AdminUser]:
    users = db.execute(select(User).where(User.tenant_id == tenant_id)).scalars().all()
    projects = {p.project_id: p for p in db.execute(select(Project)).scalars()}
    teams = {t.team_id: t for t in db.execute(select(Team)).scalars()}

    memberships: dict[uuid.UUID, list[UserTeamMembership]] = {}
    for m in db.execute(select(UserTeamMembership)).scalars():
        memberships.setdefault(m.user_id, []).append(m)
    padmin: dict[uuid.UUID, list[ProjectAdmin]] = {}
    for a in db.execute(select(ProjectAdmin)).scalars():
        padmin.setdefault(a.user_id, []).append(a)

    _rank = {"viewer": 1, "contributor": 2, "team_lead": 3}
    out: list[AdminUser] = []
    for u in users:
        best: dict[uuid.UUID, str] = {}
        tm_out: list[TeamMembershipOut] = []
        for m in memberships.get(u.user_id, []):
            team = teams.get(m.team_id)
            project = projects.get(m.project_id)
            tm_out.append(TeamMembershipOut(
                membership_id=str(m.id), team_id=str(m.team_id),
                team_name=team.name if team else "(unknown)",
                project_id=str(m.project_id),
                project_name=project.name if project else "(unknown)",
                role=m.role.value,
            ))
            if m.project_id not in best or _rank[m.role.value] > _rank[best[m.project_id]]:
                best[m.project_id] = m.role.value
        pa_out: list[ProjectAdminOut] = []
        for a in padmin.get(u.user_id, []):
            project = projects.get(a.project_id)
            pa_out.append(ProjectAdminOut(
                id=str(a.id), project_id=str(a.project_id),
                project_name=project.name if project else "(unknown)",
            ))
            best[a.project_id] = "project_admin"
        out.append(AdminUser(
            user_id=str(u.user_id), username=u.email, full_name=u.full_name or u.email,
            team_name=tm_out[0].team_name if tm_out else None,
            is_org_admin=bool(u.is_org_admin),
            roles=[{"project_id": str(pid), "role": r} for pid, r in best.items()],
            team_memberships=tm_out, project_admin=pa_out,
        ))
    return out


def _clean_name(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None


def _get_or_create_target(db, identity, email, tenant_id, *, is_org_admin=False, full_name=None):
    target = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if target is not None:
        if target.tenant_id != identity.tenant_id:
            raise HTTPException(status_code=409, detail="That email belongs to another organization")
        # fill in a name if we were given one and the existing row has none
        if full_name and not target.full_name:
            target.full_name = full_name
        if not target.password_hash:
            target.password_hash = hash_password(DEFAULT_NEW_USER_PASSWORD)
        return target, False
    target = User(
        email=email, tenant_id=tenant_id, password_hash=hash_password(DEFAULT_NEW_USER_PASSWORD),
        is_org_admin=is_org_admin, full_name=full_name,
    )
    db.add(target)
    db.flush()
    record_audit(db, actor_id=identity.user_id, action="INVITE_USER", resource_type="user",
                 resource_id=target.user_id, details={"email": email})
    return target, True


def _load_membership(db, identity, membership_id) -> tuple[UserTeamMembership, Team, Project]:
    m = db.get(UserTeamMembership, membership_id)
    if m is None:
        raise HTTPException(status_code=404, detail="Membership not found")
    team = db.get(Team, m.team_id)
    project = db.get(Project, m.project_id)
    if team is None or project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Membership not found")
    if not has_permission(db, identity.user_id, "manage_team_members", team.team_id, project.project_id):
        raise HTTPException(status_code=403, detail=f"You cannot manage members of team '{team.name}'")
    return m, team, project


# --- directory ------------------------------------------------------------

@router.get("/users", response_model=list[AdminUser])
def list_admin_users(
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_directory_access(identity)
    return _all_users(db, identity.tenant_id)


@router.post("/users", status_code=201)
def create_org_user(
    body: CreateUserRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not identity.is_org_admin:
        raise HTTPException(status_code=403, detail="Only organization admins can add users")
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    if db.execute(select(User).where(User.email == email)).scalar_one_or_none() is not None:
        raise HTTPException(status_code=409, detail="An account with that email already exists")
    full_name = _clean_name(body.full_name)
    user = User(
        email=email, tenant_id=identity.tenant_id, password_hash=hash_password(DEFAULT_NEW_USER_PASSWORD),
        is_org_admin=False, full_name=full_name,
    )
    db.add(user)
    db.flush()
    record_audit(db, actor_id=identity.user_id, action="CREATE_USER", resource_type="user",
                 resource_id=user.user_id, details={"email": email})
    db.commit()
    db.refresh(user)
    return {"status": "created", "user_id": str(user.user_id), "email": email,
            "full_name": user.full_name, "default_password": DEFAULT_NEW_USER_PASSWORD}


# --- add access ---------------------------------------------------------------

@router.post("/assign-roles")
def assign_roles(
    body: AssignRolesRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    email = body.email.strip().lower()
    if "@" not in email:
        raise HTTPException(status_code=422, detail="Enter a valid email address")
    full_name = _clean_name(body.full_name)

    # ---- team_member -> per-team UserTeamMembership rows ----
    if body.mode == "team_member":
        norm: list[tuple[Team, Project, str]] = []
        for a in body.team_assignments:
            role = _ROLE_ALIASES.get(a.role, a.role)
            if role not in _TEAM_ROLES:
                raise HTTPException(status_code=422, detail=f"Invalid team role: {a.role}")
            team = db.get(Team, a.team_id)
            project = db.get(Project, team.project_id) if team else None
            if team is None or project is None or project.tenant_id != identity.tenant_id:
                raise HTTPException(status_code=404, detail="Team not found")
            norm.append((team, project, role))
        if not norm:
            raise HTTPException(status_code=422, detail="Pick a role for at least one team")

        for team, project, _role in norm:
            if not has_permission(db, identity.user_id, "manage_team_members", team.team_id, project.project_id):
                raise HTTPException(status_code=403, detail=f"You cannot manage members of team '{team.name}'")

        tenant_for_new = norm[0][1].tenant_id
        target, new_user = _get_or_create_target(db, identity, email, tenant_for_new, full_name=full_name)

        assigned = []
        for team, project, role in norm:
            m = db.execute(select(UserTeamMembership).where(
                UserTeamMembership.user_id == target.user_id,
                UserTeamMembership.team_id == team.team_id,
                UserTeamMembership.project_id == project.project_id,
            )).scalar_one_or_none()
            if m is None:
                db.add(UserTeamMembership(user_id=target.user_id, team_id=team.team_id,
                                          project_id=project.project_id, role=TeamRole(role)))
            else:
                m.role = TeamRole(role)
            record_audit(db, actor_id=identity.user_id, action="ASSIGN_ROLE", resource_type="user",
                         resource_id=target.user_id,
                         details={"email": email, "team_id": str(team.team_id),
                                  "project_id": str(project.project_id), "role": role})
            assigned.append(f"{role} on {team.name}")
        db.commit()
        return {"status": "assigned", "user_id": str(target.user_id), "email": email,
                "new_user": new_user, "message": f"{email}: {', '.join(assigned)}."}

    # ---- project_admin -> ProjectAdmin rows (org_admin only) ----
    if body.mode == "project_admin":
        if not identity.is_org_admin:
            raise HTTPException(status_code=403, detail="Only organization admins can grant Project Admin")
        tenant_projects = db.execute(
            select(Project).where(Project.tenant_id == identity.tenant_id)).scalars().all()
        projects = tenant_projects if body.all_projects else [
            p for p in tenant_projects if p.project_id in set(body.project_ids)
        ]
        if not projects:
            raise HTTPException(status_code=422, detail="Select at least one project")
        target, new_user = _get_or_create_target(db, identity, email, identity.tenant_id, full_name=full_name)
        names = []
        for project in projects:
            if db.execute(select(ProjectAdmin).where(
                    ProjectAdmin.user_id == target.user_id,
                    ProjectAdmin.project_id == project.project_id)).scalar_one_or_none() is None:
                db.add(ProjectAdmin(user_id=target.user_id, project_id=project.project_id))
            record_audit(db, actor_id=identity.user_id, action="GRANT_PROJECT_ADMIN",
                         resource_type="user", resource_id=target.user_id,
                         details={"email": email, "project_id": str(project.project_id)})
            names.append(project.name)
        db.commit()
        return {"status": "assigned", "user_id": str(target.user_id), "email": email,
                "new_user": new_user, "message": f"{email} is now Project Admin of {', '.join(names)}."}

    # ---- org_admin -> is_org_admin flag (org_admin only) ----
    if body.mode == "org_admin":
        if not identity.is_org_admin:
            raise HTTPException(status_code=403, detail="Only organization admins can grant Org Admin")
        target, new_user = _get_or_create_target(
            db, identity, email, identity.tenant_id, is_org_admin=True, full_name=full_name
        )
        target.is_org_admin = True
        record_audit(db, actor_id=identity.user_id, action="GRANT_ORG_ADMIN", resource_type="user",
                     resource_id=target.user_id, details={"email": email})
        db.commit()
        return {"status": "assigned", "user_id": str(target.user_id), "email": email,
                "new_user": new_user, "message": f"{email} is now an Organization Admin."}

    raise HTTPException(status_code=422, detail=f"Unknown mode: {body.mode}")


# --- edit / remove existing access ------------------------------------------

@router.patch("/team-membership/{membership_id}")
def update_team_role(
    membership_id: uuid.UUID,
    body: RoleUpdateRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    role = _ROLE_ALIASES.get(body.role, body.role)
    if role not in _TEAM_ROLES:
        raise HTTPException(status_code=422, detail=f"Invalid team role: {body.role}")
    m, team, project = _load_membership(db, identity, membership_id)
    m.role = TeamRole(role)
    record_audit(db, actor_id=identity.user_id, action="UPDATE_ROLE", resource_type="user",
                 resource_id=m.user_id,
                 details={"team_id": str(team.team_id), "project_id": str(project.project_id), "role": role})
    db.commit()
    return {"status": "updated", "membership_id": str(m.id), "role": role}


@router.delete("/team-membership/{membership_id}")
def remove_team_membership(
    membership_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    m, team, project = _load_membership(db, identity, membership_id)
    record_audit(db, actor_id=identity.user_id, action="REMOVE_ROLE", resource_type="user",
                 resource_id=m.user_id,
                 details={"team_id": str(team.team_id), "project_id": str(project.project_id),
                          "role": m.role.value})
    db.delete(m)
    db.commit()
    return {"status": "removed", "membership_id": str(membership_id)}


@router.delete("/project-admin/{scope_id}")
def remove_project_admin(
    scope_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not identity.is_org_admin:
        raise HTTPException(status_code=403, detail="Only organization admins can remove Project Admin")
    a = db.get(ProjectAdmin, scope_id)
    if a is None:
        raise HTTPException(status_code=404, detail="Project-admin scope not found")
    project = db.get(Project, a.project_id)
    if project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Project-admin scope not found")
    record_audit(db, actor_id=identity.user_id, action="REVOKE_PROJECT_ADMIN", resource_type="user",
                 resource_id=a.user_id, details={"project_id": str(a.project_id)})
    db.delete(a)
    db.commit()
    return {"status": "removed", "id": str(scope_id)}


@router.post("/revoke-org-admin")
def revoke_org_admin(
    body: RevokeOrgAdminRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not identity.is_org_admin:
        raise HTTPException(status_code=403, detail="Only organization admins can revoke Org Admin")
    if body.user_id == identity.user_id:
        raise HTTPException(status_code=409, detail="You cannot revoke your own organization-admin status.")
    target = db.get(User, body.user_id)
    if target is None or target.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="User was not found in your organization")
    if not target.is_org_admin:
        raise HTTPException(status_code=409, detail="That user is not an organization admin.")
    org_admin_count = db.execute(
        select(func.count()).select_from(User)
        .where(User.tenant_id == identity.tenant_id, User.is_org_admin.is_(True))
    ).scalar_one()
    if org_admin_count <= 1:
        raise HTTPException(
            status_code=409,
            detail="Cannot remove organization-admin from the last remaining organization admin.",
        )
    target.is_org_admin = False
    record_audit(db, actor_id=identity.user_id, action="REVOKE_ORG_ADMIN", resource_type="user",
                 resource_id=target.user_id, details={"email": target.email})
    db.commit()
    return {"status": "revoked", "user_id": str(target.user_id)}


# --- legacy (kept for compatibility) ---------------------------------------

@router.post("/access")
def grant_org_admin(
    body: OrgAdminRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not identity.is_org_admin:
        raise HTTPException(status_code=403, detail="Only organization admins can grant admin access")
    email = body.email.strip().lower()
    target = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if target is None or target.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="User was not found in your organization")
    target.is_org_admin = True
    record_audit(db, actor_id=identity.user_id, action="GRANT_ORG_ADMIN", resource_type="user",
                 resource_id=target.user_id, details={"email": email})
    db.commit()
    return {"status": "updated", "user_id": str(target.user_id), "email": email}


@router.post("/project-access")
def assign_project_access(
    body: ProjectAccessRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    role = _ROLE_ALIASES.get(body.role, body.role)
    if role not in _TEAM_ROLES:
        raise HTTPException(status_code=422, detail="role must be viewer, contributor or team_lead")
    team = None
    if body.team_id is not None:
        team = db.get(Team, body.team_id)
    elif body.project_id is not None and body.team_name:
        team = db.execute(select(Team).where(
            Team.project_id == body.project_id, Team.name == body.team_name.strip(),
        )).scalar_one_or_none()
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    project = db.get(Project, team.project_id)
    if project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Team not found")
    if not has_permission(db, identity.user_id, "manage_team_members", team.team_id, project.project_id):
        raise HTTPException(status_code=403, detail="You cannot manage members of this team")
    email = body.email.strip().lower()
    target, new_user = _get_or_create_target(db, identity, email, project.tenant_id)
    m = db.execute(select(UserTeamMembership).where(
        UserTeamMembership.user_id == target.user_id,
        UserTeamMembership.team_id == team.team_id,
        UserTeamMembership.project_id == project.project_id,
    )).scalar_one_or_none()
    if m is None:
        db.add(UserTeamMembership(user_id=target.user_id, team_id=team.team_id,
                                  project_id=project.project_id, role=TeamRole(role)))
    else:
        m.role = TeamRole(role)
    record_audit(db, actor_id=identity.user_id, action="ASSIGN_ROLE", resource_type="user",
                 resource_id=target.user_id,
                 details={"email": email, "team_id": str(team.team_id),
                          "project_id": str(project.project_id), "role": role, "new_user": new_user})
    db.commit()
    return {"status": "assigned", "user_id": str(target.user_id), "email": email,
            "team_id": str(team.team_id), "project_id": str(project.project_id),
            "role": role, "new_user": new_user}


@router.get("/audit-log")
def get_audit_log(
    limit: int = 200,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Account / permission-management audit trail, scoped by role:
      org_admin -> tenant-wide; project_admin -> their project(s);
      team_lead -> their team(s). contributor / viewer get a 403.
    """
    try:
        return list_audit_log(db, identity, limit=min(max(limit, 1), 1000))
    except AuditAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
