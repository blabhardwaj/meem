"""
Audit logging (adapted from the teammate's database.py::audit_log +
get_audit_log). Phase 1 created the audit_log table but nothing wrote to it —
this closes that gap.

record_audit() adds a row to the CALLER'S session without committing, so the
audit entry lands in the same transaction as the action it describes (an
upload that rolls back leaves no audit row). details is stored as JSONB.

The read side (GET /admin/audit-log) is the finalized "Audit Log" design:

  * Visible to org_admin, project_admin and team_lead ONLY. contributor /
    viewer get a 403 (AuditAccessError) — enforced in the router.
  * Scoped by role:
      - org_admin      -> every entry in the tenant
      - project_admin  -> entries tied to a project they administer
      - team_lead      -> entries tied to a team they lead
  * Only account / permission-management actions are surfaced (AUDIT_LOG_ACTIONS
    below). LOGIN is neither captured (see app/routers/auth.py) nor shown.
  * Access-request entries carry a resolved outcome ("pending" / "approved" /
    "denied") so the reader sees how the request was ultimately decided.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit import AuditLog
from app.models.project import Project
from app.models.team import (
    AccessRequest,
    AccessRequestStatus,
    Team,
    TeamRole,
)
from app.models.user import User


class AuditAccessError(Exception):
    """The caller's role carries no audit-log visibility at all (-> 403)."""


# Account / permission-management actions only. Deliberately excludes LOGIN,
# document workflow (UPLOAD/SUBMIT/APPROVE/REJECT_DOCUMENT — those belong to
# Project Activity), CREATE_PROJECT, etc.
AUDIT_LOG_ACTIONS = (
    "SIGNUP",              # self-service account creation
    "CREATE_USER",
    "INVITE_USER",
    "ASSIGN_ROLE",
    "UPDATE_ROLE",
    "REMOVE_ROLE",
    "GRANT_PROJECT_ADMIN",
    "REVOKE_PROJECT_ADMIN",
    "GRANT_ORG_ADMIN",
    "REVOKE_ORG_ADMIN",
    "REQUEST_CONFIDENTIAL_ACCESS",
    "APPROVE_ACCESS_REQUEST",
    "DENY_ACCESS_REQUEST",
    "DISMISS_ADVISORY_FINDING",
    "RESTORE_ADVISORY_FINDING",
)

_ACCESS_REQUEST_OUTCOME = {
    AccessRequestStatus.pending: "pending",
    AccessRequestStatus.approved: "approved",
    AccessRequestStatus.denied: "denied",
}


def record_audit(
    db: Session,
    *,
    actor_id: uuid.UUID,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | str | None = None,
    details: dict | None = None,
) -> AuditLog:
    """Stage an audit row on `db` (no commit — caller commits with its work)."""
    if isinstance(resource_id, str):
        try:
            resource_id = uuid.UUID(resource_id)
        except ValueError:
            resource_id = None
    entry = AuditLog(
        user_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
    )
    db.add(entry)
    db.flush()
    return entry


def _details_to_text(details: dict | None) -> str | None:
    if not details:
        return None
    return "; ".join(f"{k}={v}" for k, v in details.items())


def _user_display(u: User) -> str:
    return u.full_name or u.email


def _as_uuid(value) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError:
            return None
    return None


def list_audit_for_tenant(db: Session, tenant_id: uuid.UUID, limit: int = 100) -> list[dict]:
    """Newest-first audit rows for every user in `tenant_id` (unscoped — kept
    for compatibility; the HTTP surface uses list_audit_log)."""
    rows = db.execute(
        select(AuditLog, User.email)
        .join(User, User.user_id == AuditLog.user_id)
        .where(User.tenant_id == tenant_id)
        .order_by(AuditLog.created_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "log_id": str(a.log_id),
            "user_id": str(a.user_id),
            "actor_email": email,
            "action": a.action,
            "resource_type": a.resource_type,
            "resource_id": str(a.resource_id) if a.resource_id else None,
            "details": _details_to_text(a.details),
            "status": None,
            "created_at": a.created_at.isoformat(),
            "timestamp": a.created_at.isoformat(),
        }
        for a, email in rows
    ]


def list_audit_log(db: Session, identity, limit: int = 200) -> list[dict]:
    """
    Role-scoped audit log for the finalized design.

    `identity` is a services.auth.ResolvedIdentity. Raises AuditAccessError if
    the caller is neither org_admin, nor a project_admin, nor a team_lead
    anywhere.
    """
    lead_team_ids = {
        m.team_id for m in identity.team_memberships if m.role == TeamRole.team_lead
    }
    admin_project_ids = set(identity.project_admin_project_ids)

    if not identity.is_org_admin and not admin_project_ids and not lead_team_ids:
        raise AuditAccessError(
            "The audit log is available to organization admins, project admins "
            "and team leads only."
        )

    candidates = db.execute(
        select(AuditLog, User.email)
        .join(User, User.user_id == AuditLog.user_id)
        .where(
            User.tenant_id == identity.tenant_id,
            AuditLog.action.in_(AUDIT_LOG_ACTIONS),
        )
        .order_by(AuditLog.created_at.desc())
    ).all()

    # --- scope-resolution helpers, preloaded to avoid per-row queries ---------
    projects = {
        p.project_id: p for p in db.execute(
            select(Project).where(Project.tenant_id == identity.tenant_id)
        ).scalars()
    }
    tenant_project_ids = set(projects)
    teams = {
        t.team_id: t for t in db.execute(
            select(Team).where(Team.project_id.in_(tenant_project_ids))
        ).scalars()
    } if tenant_project_ids else {}
    access_requests = {
        r.request_id: r for r in db.execute(select(AccessRequest)).scalars()
        if r.team_id in teams
    }
    # user_id -> User, for resolving details/resource ids that name a person
    # (target of a role change, requester of an access grant, ...) to a
    # readable email/name instead of the raw UUID the Audit Log used to show.
    users = {
        u.user_id: u for u in db.execute(
            select(User).where(User.tenant_id == identity.tenant_id)
        ).scalars()
    }
    reqs_by_user_team: dict[tuple, list] = {}
    for r in access_requests.values():
        reqs_by_user_team.setdefault((r.user_id, r.team_id), []).append(r)

    def _row_scope(a: AuditLog) -> tuple[uuid.UUID | None, uuid.UUID | None]:
        """(project_id, team_id) this entry belongs to; either may be None."""
        d = a.details or {}
        team_id = None
        project_id = None
        if a.action in ("ASSIGN_ROLE", "UPDATE_ROLE", "REMOVE_ROLE"):
            team_id = _as_uuid(d.get("team_id"))
            project_id = _as_uuid(d.get("project_id"))
        elif a.action in ("GRANT_PROJECT_ADMIN", "REVOKE_PROJECT_ADMIN", "DISMISS_ADVISORY_FINDING", "RESTORE_ADVISORY_FINDING"):
            project_id = _as_uuid(d.get("project_id"))
        elif a.action == "REQUEST_CONFIDENTIAL_ACCESS":
            team_id = a.resource_id  # resource_id IS the team for this action
        elif a.action in ("APPROVE_ACCESS_REQUEST", "DENY_ACCESS_REQUEST"):
            req = access_requests.get(a.resource_id)
            if req is not None:
                team_id = req.team_id
        if team_id is not None and project_id is None:
            team = teams.get(team_id)
            if team is not None:
                project_id = team.project_id
        return project_id, team_id

    def _visible(a: AuditLog) -> bool:
        if identity.is_org_admin:
            return True
        project_id, team_id = _row_scope(a)
        if project_id is not None and project_id in admin_project_ids:
            return True
        if team_id is not None and team_id in lead_team_ids:
            return True
        if project_id is not None and any(t.project_id == project_id for t_id, t in teams.items() if t_id in lead_team_ids):
            return True
        return False

    # Detail keys that name another record by raw UUID -> (lookup dict, display fn).
    # Used both to relabel the Resource column and to swap these same UUIDs out
    # of the Details text for something a human can actually read.
    _ID_DETAIL_KEYS = {
        "team_id": ("team", lambda tid: teams[tid].name if tid in teams else None),
        "project_id": ("project", lambda pid: projects[pid].name if pid in projects else None),
        "requester_id": ("requester", lambda uid: _user_display(users[uid]) if uid in users else None),
    }

    def _resource_display(a: AuditLog) -> str:
        """Human-readable 'Resource' cell: the type plus a name/email instead
        of the bare resource_id UUID, falling back to the UUID only when no
        name can be resolved (e.g. the record was since deleted)."""
        d = a.details or {}
        rid = a.resource_id
        label = None
        if a.resource_type == "user":
            label = d.get("email") or (_user_display(users[rid]) if rid in users else None)
        elif a.resource_type == "team":
            label = d.get("team") or (teams[rid].name if rid in teams else None)
        elif a.resource_type == "access_request":
            label = d.get("team")
        elif a.resource_type == "audit_finding":
            label = d.get("rule_code")
        elif a.resource_type == "project":
            label = d.get("project") or (projects[rid].name if rid in projects else None)
        if label is None and rid is not None:
            label = str(rid)
        return f"{a.resource_type} · {label}" if label else a.resource_type

    def _humanized_dict(a: AuditLog) -> dict:
        """a.details with team_id/project_id/requester_id UUIDs swapped for
        the team/project/requester name they refer to (falling back to the
        raw id if it can't be resolved), so nothing downstream ever has to
        decode a UUID by hand."""
        d = a.details or {}
        resolved: dict[str, object] = {}
        for k, v in d.items():
            if k in _ID_DETAIL_KEYS and v:
                new_key, resolver = _ID_DETAIL_KEYS[k]
                if new_key in d:
                    continue  # a human-readable version is already present -- drop the raw id
                resolved[new_key] = resolver(_as_uuid(v)) or v
                continue
            resolved[k] = v
        return resolved

    def _humanize_details(a: AuditLog) -> str | None:
        resolved = _humanized_dict(a)
        return "; ".join(f"{k}={v}" for k, v in resolved.items()) if resolved else None

    def _target_display(a: AuditLog, d: dict) -> str:
        """Best-effort name/email of the row's TARGET -- the account being
        assigned/removed/granted, as opposed to the actor who did it."""
        if a.resource_type == "user":
            if d.get("email"):
                return d["email"]
            if a.resource_id in users:
                return _user_display(users[a.resource_id])
        return "the user"

    # One human sentence per action, describing what was done -- the actor's
    # name is prepended by the caller. Deliberately covers exactly
    # AUDIT_LOG_ACTIONS; anything else falls through to the generic branch.
    def _action_summary(a: AuditLog, d: dict, outcome: str | None) -> str:
        role = d.get("role")
        team = d.get("team")
        project = d.get("project")
        target = _target_display(a, d)

        if a.action == "SIGNUP":
            method = {
                "password": "email and password", "google": "Google sign-in",
                "register_org": "registering a new organization", "accept_invite": "accepting an invitation",
            }.get(d.get("method"), d.get("method") or "signing up")
            return f"signed up via {method}."
        if a.action in ("INVITE_USER", "CREATE_USER"):
            verb = "invited" if a.action == "INVITE_USER" else "created"
            where = f" as {role} to team {team} ({project})" if role and team else (f" to {project}" if project else "")
            return f"{verb} {target}{where}."
        if a.action == "ASSIGN_ROLE":
            note = " A new account was created for them." if d.get("new_user") else ""
            return f"assigned {target} as {role} on team {team} ({project}).{note}"
        if a.action == "UPDATE_ROLE":
            return f"changed {target}'s role to {role} on team {team} ({project})."
        if a.action == "REMOVE_ROLE":
            return f"removed {target} ({role}) from team {team} ({project})."
        if a.action == "GRANT_PROJECT_ADMIN":
            return f"granted {target} Project Admin on {project}."
        if a.action == "REVOKE_PROJECT_ADMIN":
            return f"revoked {target}'s Project Admin on {project}."
        if a.action == "GRANT_ORG_ADMIN":
            return f"granted {target} Organization Admin."
        if a.action == "REVOKE_ORG_ADMIN":
            return f"revoked {target}'s Organization Admin."
        if a.action == "REQUEST_CONFIDENTIAL_ACCESS":
            scope = f" — {d['scope']} scope" if d.get("scope") else ""
            reason = f" Reason: {d['reason']}" if d.get("reason") else ""
            status = f" Status: {outcome}." if outcome else ""
            return f"requested confidential access to team {team} ({project}){scope}.{reason}{status}"
        if a.action in ("APPROVE_ACCESS_REQUEST", "DENY_ACCESS_REQUEST"):
            verb = "approved" if a.action == "APPROVE_ACCESS_REQUEST" else "denied"
            requester = d.get("requester", "a user")
            return f"{verb} {requester}'s access request for team {team}."
        if a.action in ("DISMISS_ADVISORY_FINDING", "RESTORE_ADVISORY_FINDING"):
            verb = "dismissed" if a.action == "DISMISS_ADVISORY_FINDING" else "restored"
            rule = d.get("rule_code")
            where = f" {rule} " if rule else " an "
            return f"{verb}{where}advisory finding on {project}."

        kv = "; ".join(f"{k}={v}" for k, v in d.items())
        return f"performed {a.action.replace('_', ' ').lower()} on {_resource_display(a)}" + (f" ({kv})" if kv else "") + "."

    def _outcome(a: AuditLog) -> str | None:
        if a.action == "APPROVE_ACCESS_REQUEST":
            return "approved"
        if a.action == "DENY_ACCESS_REQUEST":
            return "denied"
        if a.action == "REQUEST_CONFIDENTIAL_ACCESS":
            reqs = reqs_by_user_team.get((a.user_id, a.resource_id), [])
            if not reqs:
                return "pending"
            best = min(
                reqs,
                key=lambda r: abs((r.requested_at - a.created_at).total_seconds())
                if r.requested_at else float("inf"),
            )
            return _ACCESS_REQUEST_OUTCOME.get(best.status)
        return None

    out: list[dict] = []
    for a, email in candidates:
        if not _visible(a):
            continue
        actor_name = _user_display(users[a.user_id]) if a.user_id in users else email
        outcome = _outcome(a)
        summary = _action_summary(a, _humanized_dict(a), outcome)
        out.append({
            "log_id": str(a.log_id),
            "user_id": str(a.user_id),
            "actor_email": email,
            "actor_name": actor_name,
            "action": a.action,
            "resource_type": a.resource_type,
            "resource_id": str(a.resource_id) if a.resource_id else None,
            "resource_label": _resource_display(a),
            # "Details" merges the old Resource + Details columns into one
            # structured sentence -- who (actor_name) did what (summary) --
            # instead of forcing the reader to decode a resource type plus a
            # raw key=value dump. `details` stays available as the plain
            # fallback (actor + summary, one string) for any other consumer.
            "summary": summary,
            "details": f"{actor_name} {summary}",
            "status": outcome,
            "created_at": a.created_at.isoformat(),
            "timestamp": a.created_at.isoformat(),
        })
        if len(out) >= limit:
            break
    return out
