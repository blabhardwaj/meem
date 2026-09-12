"""
Core ABAC/RBAC permission functions.

has_permission() — the RBAC-style rank comparison, cumulative hierarchy,
with org_admin/project_admin full bypass and the contributor confidential-
access-grant exception.

classify_document_visibility() — the full ABAC decision for a specific
document (bypass, team visibility, sensitivity clearance), as a three-way
outcome. can_view_document() is a bool-collapsing wrapper over it — the
finer-grained outcome exists because Phase C retrieval needs to know WHY a
document was excluded (not visible at all, vs. visible but sensitivity-
blocked) to later offer a "request access" suggestion; nothing about the
underlying logic changed by adding it.

build_access_filter() — compound filter for listing/searching documents.

has_any_project_access() — the coarse "does this user have ANY relationship
to this project at all" gate (any team membership, or project/org admin),
used to hard-reject a total stranger before any further, more expensive
work (e.g. a Qdrant call in retrieval.py).
"""

from collections import defaultdict
import enum
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.team import UserTeamMembership, ProjectAdmin, TeamRole, AccessRequest, AccessRequestStatus
from app.models.document import Document, DocumentTeamVisibility, SensitivityLevel
from app.models.stage import Stage, TeamStageAccess
from app.services.authorization_context import AuthorizationContext


# Rank order — higher index = more privileged. Used for cumulative comparison.
_TEAM_ROLE_RANK = {
    TeamRole.viewer: 0,
    TeamRole.contributor: 1,
    TeamRole.team_lead: 2,
}

# Minimum TeamRole required per action (actions gated at team level).
_ACTION_MIN_ROLE = {
    "view": TeamRole.viewer,
    "upload": TeamRole.contributor,
    "edit": TeamRole.contributor,
    # Document approval workflow (MERGE_DECISIONS §3/4): submit is the same bar
    # as upload; approve/reject require team_lead+ on that document's team.
    "submit": TeamRole.contributor,
    "approve": TeamRole.team_lead,
    "reject": TeamRole.team_lead,
    "manage_team_members": TeamRole.team_lead,
    "approve_access_request": TeamRole.team_lead,
}

def _is_org_admin(db: Session, user_id: UUID) -> bool:
    user = db.get(User, user_id)
    return bool(user and user.is_org_admin)


def _is_project_admin(db: Session, user_id: UUID, project_id: UUID) -> bool:
    stmt = select(ProjectAdmin).where(
        ProjectAdmin.user_id == user_id, ProjectAdmin.project_id == project_id
    )
    return db.execute(stmt).scalar_one_or_none() is not None


def _get_team_membership(db: Session, user_id: UUID, team_id: UUID) -> UserTeamMembership | None:
    stmt = select(UserTeamMembership).where(
        UserTeamMembership.user_id == user_id, UserTeamMembership.team_id == team_id
    )
    return db.execute(stmt).scalar_one_or_none()


def has_permission(db: Session, user_id: UUID, action: str, team_id: UUID, project_id: UUID) -> bool:
    """
    Core rank-comparison check, cumulative hierarchy, with full bypass
    for org_admin (tenant-wide) and project_admin (project-wide).
    """
    if _is_org_admin(db, user_id):
        return True
    if _is_project_admin(db, user_id, project_id):
        return True

    membership = _get_team_membership(db, user_id, team_id)
    if membership is None:
        return False  # no membership on this team = no access, full stop

    required_role = _ACTION_MIN_ROLE.get(action)
    if required_role is None:
        raise ValueError(f"Unknown action: {action}")

    return _TEAM_ROLE_RANK[membership.role] >= _TEAM_ROLE_RANK[required_role]


def has_any_project_access(db: Session, user_id: UUID, project_id: UUID) -> bool:
    """
    Coarse gate: does `user_id` have ANY relationship to `project_id` at all
    — org_admin, project_admin, or membership on at least one team in this
    project? A False here means a total stranger to the project; callers
    (e.g. retrieval.py) should hard-reject before doing anything more
    expensive (a Qdrant call, a DB scan of documents, etc.).

    Deliberately coarser than get_accessible_stages_for_user(): a legitimate
    team member whose team simply hasn't been granted any team_stage_access
    yet still passes this gate (they belong here, they just can't see
    anything yet) — that's a different, non-error case from a stranger.
    """
    if _is_org_admin(db, user_id):
        return True
    if _is_project_admin(db, user_id, project_id):
        return True
    # A user can belong to several teams in the same project (e.g. erin:
    # Engineering + Design) — this only needs to know AT LEAST ONE exists,
    # so cap it at 1 row rather than scalar_one_or_none(), which raises on
    # more than one.
    return db.execute(
        select(UserTeamMembership.id).where(
            UserTeamMembership.user_id == user_id,
            UserTeamMembership.project_id == project_id,
        ).limit(1)
    ).scalar_one_or_none() is not None


def has_stage_access(db: Session, user_id: UUID, team_id: UUID, stage_id: UUID, project_id: UUID) -> bool:
    """
    Phase A Part 3: does `team_id` have a team_stage_access grant for
    `stage_id`? org_admin/project_admin BYPASS entirely — same bypass
    pattern as has_permission(). No grant row == no access, full stop
    (there is no rank/hierarchy here; access is per (team, stage), not
    cumulative).

    Used as a THIRD gate in create_document(), alongside (not replacing)
    has_permission()'s role/team-project check.
    """
    if _is_org_admin(db, user_id):
        return True
    if _is_project_admin(db, user_id, project_id):
        return True

    stmt = select(TeamStageAccess).where(
        TeamStageAccess.team_id == team_id, TeamStageAccess.stage_id == stage_id
    )
    return db.execute(stmt).scalar_one_or_none() is not None


def get_accessible_stages_for_user(db: Session, user_id: UUID, project_id: UUID) -> list[UUID]:
    """
    The set of stage_ids `user_id` may see/upload to within `project_id`:
    the UNION of stages accessible via ANY of their team memberships in this
    project, via team_stage_access. org_admin/project_admin bypass — every
    active stage in the project, unfiltered.

    Used to filter stage visibility (Sources panel, stage dropdowns) for
    regular users, and is the resolver Phase C retrieval will consume to
    scope RAG to what a user is allowed to see.
    """
    if _is_org_admin(db, user_id) or _is_project_admin(db, user_id, project_id):
        return list(db.execute(
            select(Stage.stage_id).where(
                Stage.project_id == project_id, Stage.deleted_at.is_(None)
            )
        ).scalars())

    team_ids = list(db.execute(
        select(UserTeamMembership.team_id).where(
            UserTeamMembership.user_id == user_id,
            UserTeamMembership.project_id == project_id,
        )
    ).scalars())
    if not team_ids:
        return []

    return list(db.execute(
        select(TeamStageAccess.stage_id)
        .join(Stage, Stage.stage_id == TeamStageAccess.stage_id)
        .where(TeamStageAccess.team_id.in_(team_ids), Stage.deleted_at.is_(None))
        .distinct()
    ).scalars())


def _has_active_confidential_grant(db: Session, user_id: UUID, team_id: UUID) -> bool:
    stmt = select(AccessRequest).where(
        AccessRequest.user_id == user_id,
        AccessRequest.team_id == team_id,
        AccessRequest.status == AccessRequestStatus.approved,
    )
    grant = db.execute(stmt).scalar_one_or_none()
    if grant is None:
        return False
    if grant.expires_at and grant.expires_at < datetime.now(timezone.utc):
        return False  # expired — treated as no grant
    return True


class DocumentVisibility(str, enum.Enum):
    """
    The three-way outcome of classify_document_visibility().
      fully_allowed         -> the user may see this document, full stop.
      blocked_by_sensitivity -> on a team the document IS visible to, but
                                 this user's clearance doesn't reach its
                                 sensitivity tier. Worth surfacing later
                                 (e.g. "request access") — a real, known
                                 document the user is specifically blocked
                                 from, not an absence.
      not_visible            -> not on any team this document is visible to
                                 at all. No trace should be surfaced to the
                                 user for this case.
    """
    fully_allowed = "fully_allowed"
    blocked_by_sensitivity = "blocked_by_sensitivity"
    not_visible = "not_visible"


def classify_document_visibility(db: Session, user_id: UUID, document: Document) -> DocumentVisibility:
    """
    The full ABAC decision for viewing a specific document — bypass, team
    visibility, and sensitivity clearance — as a three-way outcome rather
    than can_view_document()'s bool. Same logic, same order, nothing added.
    """
    if _is_org_admin(db, user_id):
        return DocumentVisibility.fully_allowed
    if _is_project_admin(db, user_id, document.project_id):
        return DocumentVisibility.fully_allowed

    # Team visibility — document must be visible to a team the user belongs to
    visible_team_ids = {
        row.team_id for row in db.execute(
            select(DocumentTeamVisibility).where(DocumentTeamVisibility.document_id == document.document_id)
        ).scalars()
    }
    memberships = [
        m for m in (_get_team_membership(db, user_id, team_id) for team_id in visible_team_ids)
        if m is not None
    ]

    if not memberships:
        return DocumentVisibility.not_visible  # not on any team this document is visible to

    # Sensitivity clearance
    if document.sensitivity_level in (SensitivityLevel.public, SensitivityLevel.internal):
        return DocumentVisibility.fully_allowed  # viewer+ can always see these

    # confidential tier
    if any(_TEAM_ROLE_RANK[m.role] >= _TEAM_ROLE_RANK[TeamRole.team_lead] for m in memberships):
        return DocumentVisibility.fully_allowed  # team_lead+ sees confidential automatically

    if any(_has_active_confidential_grant(db, user_id, m.team_id) for m in memberships):
        return DocumentVisibility.fully_allowed

    return DocumentVisibility.blocked_by_sensitivity  # viewer or contributor with no grant


def classify_documents_visibility(
    db: Session,
    auth_context: AuthorizationContext,
    document_ids: list[UUID] | set[UUID],
) -> dict[UUID, DocumentVisibility]:
    """
    Batch ABAC evaluation for candidate documents.
    Executes in 1-2 queries total instead of N queries per candidate document.

    Returns:
        dict mapping each requested document_id to DocumentVisibility:
          - fully_allowed
          - blocked_by_sensitivity
          - not_visible
    """
    if not document_ids:
        return {}

    unique_doc_ids = list(set(document_ids))
    out: dict[UUID, DocumentVisibility] = {
        doc_id: DocumentVisibility.not_visible for doc_id in unique_doc_ids
    }

    # Step 1: Batch-fetch all matching candidate documents in project
    docs = db.execute(
        select(Document).where(
            Document.document_id.in_(unique_doc_ids),
            Document.tenant_id == auth_context.tenant_id,
            Document.project_id == auth_context.project_id,
        )
    ).scalars().all()

    if not docs:
        return out

    doc_map = {d.document_id: d for d in docs}

    # Step 2: Org Admin & Project Admin bypass
    if auth_context.is_admin:
        for doc_id in doc_map:
            out[doc_id] = DocumentVisibility.fully_allowed
        return out

    # Step 3: Batch-fetch document team visibilities in ONE query
    dtv_rows = db.execute(
        select(DocumentTeamVisibility).where(
            DocumentTeamVisibility.document_id.in_(list(doc_map.keys()))
        )
    ).scalars().all()

    doc_visible_teams: dict[UUID, set[UUID]] = defaultdict(set)
    for row in dtv_rows:
        doc_visible_teams[row.document_id].add(row.team_id)

    # Step 4: Evaluate visibility & sensitivity in-memory per document
    for doc_id, document in doc_map.items():
        visible_teams = doc_visible_teams.get(doc_id, set())
        overlapping_teams = visible_teams & auth_context.team_ids

        if not overlapping_teams:
            out[doc_id] = DocumentVisibility.not_visible
            continue

        # Sensitivity check: Public & Internal
        if document.sensitivity_level in (SensitivityLevel.public, SensitivityLevel.internal):
            out[doc_id] = DocumentVisibility.fully_allowed
            continue

        # Sensitivity check: Confidential
        if document.sensitivity_level == SensitivityLevel.confidential:
            # Check highest role on any overlapping team
            roles = [auth_context.team_roles.get(tid) for tid in overlapping_teams]
            max_rank = max((_TEAM_ROLE_RANK[r] for r in roles if r in _TEAM_ROLE_RANK), default=-1)

            if max_rank >= _TEAM_ROLE_RANK[TeamRole.team_lead]:
                out[doc_id] = DocumentVisibility.fully_allowed
                continue

            # Active approved grant on any overlapping team (viewer or contributor)
            if overlapping_teams & auth_context.active_confidential_grant_team_ids:
                out[doc_id] = DocumentVisibility.fully_allowed
                continue

            out[doc_id] = DocumentVisibility.blocked_by_sensitivity
            continue

        # Restricted or unhandled sensitivity
        out[doc_id] = DocumentVisibility.blocked_by_sensitivity

    return out


def can_view_document(db: Session, user_id: UUID, document: Document) -> bool:
    """
    Full ABAC check for viewing a specific document: bypass, team
    visibility, and sensitivity clearance combined. Thin bool wrapper over
    classify_document_visibility() — see that function for the underlying
    (and, for retrieval.py's purposes, more informative) three-way outcome.
    """
    return classify_document_visibility(db, user_id, document) == DocumentVisibility.fully_allowed


def build_access_filter(db: Session, user_id: UUID, project_id: UUID):
    """
    Returns a SQLAlchemy filter condition for querying documents within a
    project — the COARSE narrowing only: tenant, project, and team-visibility.
    Sensitivity is deliberately NOT filtered here; that decision belongs
    entirely to can_view_document()'s per-row check (team_lead+ sees
    confidential automatically, contributor only with an active grant, viewer
    never). Callers must still run each returned row through can_view_document().

    For org_admin/project_admin, returns a filter scoped only to
    tenant/project (full visibility within that scope).
    """
    from sqlalchemy import and_

    user = db.get(User, user_id)

    if user.is_org_admin:
        return Document.tenant_id == user.tenant_id

    if _is_project_admin(db, user_id, project_id):
        return and_(Document.tenant_id == user.tenant_id, Document.project_id == project_id)

    # Regular user: must be on a team the document is visible to
    user_team_ids = [
        row.team_id for row in db.execute(
            select(UserTeamMembership).where(
                UserTeamMembership.user_id == user_id,
                UserTeamMembership.project_id == project_id,
            )
        ).scalars()
    ]
    if not user_team_ids:
        return Document.document_id == None  # no access — matches nothing

    visible_doc_ids_subquery = (
        select(DocumentTeamVisibility.document_id)
        .where(DocumentTeamVisibility.team_id.in_(user_team_ids))
    )

    return and_(
        Document.tenant_id == user.tenant_id,
        Document.project_id == project_id,
        Document.document_id.in_(visible_doc_ids_subquery),
        # NOTE: no sensitivity condition — every doc visible to the user's
        # teams (public, internal AND confidential) passes through here;
        # can_view_document() makes the final per-row call.
    )

def _coerce_sensitivity(value: "SensitivityLevel | int | str") -> SensitivityLevel:
    """
    Accept a SensitivityLevel, its int value (0/1/2), or its name
    (case-insensitive, e.g. "internal") — Phase 1 made SensitivityLevel an
    IntEnum, so callers may hand us any of these forms.
    """
    if isinstance(value, SensitivityLevel):
        return value
    if isinstance(value, bool):  # bool is a subclass of int — reject explicitly
        raise ValueError(f"Invalid sensitivity level: {value!r}")
    if isinstance(value, int):
        return SensitivityLevel(value)
    if isinstance(value, str):
        try:
            return SensitivityLevel[value.strip().lower()]
        except KeyError:
            raise ValueError(f"Unknown sensitivity level: {value!r}") from None
    raise ValueError(f"Unsupported sensitivity value: {value!r}")


def resolve_sensitivity(requested: "SensitivityLevel | int | str", role: str) -> SensitivityLevel:
    """
    Caps requested sensitivity by role. viewer/contributor capped at internal;
    team_lead+/org_admin/project_admin can set confidential directly. Because
    SensitivityLevel is an IntEnum, the cap is a plain comparison.
    """
    requested_level = _coerce_sensitivity(requested)
    role_name = role.value if hasattr(role, "value") else str(role)
    if role_name in ("team_lead", "org_admin", "project_admin"):
        return requested_level
    if requested_level > SensitivityLevel.internal:
        return SensitivityLevel.internal
    return requested_level