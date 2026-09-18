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

build_access_filter() — compound filter for listing/searching documents
(team visibility + stage access).

has_any_project_access() — the coarse "does this user have ANY relationship
to this project at all" gate (any team membership, or project/org admin),
used to hard-reject a total stranger before any further, more expensive
work (e.g. a Qdrant call in retrieval.py).
"""

from dataclasses import dataclass
import enum
import uuid
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.user import User
from app.models.team import (
    UserTeamMembership,
    ProjectAdmin,
    Team,
    TeamRole,
    AccessRequest,
    AccessRequestScope,
    AccessRequestStatus,
    GrantTier,
)
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


def can_edit_document(db: Session, user_id: UUID, document) -> bool:
    """
    UI_FIXES_2026-09-15.md: who may edit a document's CONTENT (the chat-based
    revision flow — start_version_review / start_version_review_from_current
    — not the plain "upload"/"submit" actions, which stay at contributor).
    Deliberately narrower than has_permission(..., "upload", ...): the
    document's own uploader, or team_lead+ on the document's team (which
    org_admin/project_admin bypass to, same as has_permission).
    """
    if document.uploaded_by == user_id:
        return True
    return has_permission(db, user_id, "manage_team_members", document.uploaded_as_team_id, document.project_id)


def is_grant_only_confidential_access(db: Session, user_id: UUID, document) -> bool:
    """
    True iff `document` is confidential-tier AND user_id's access to it is
    coming from an approved confidential-access grant rather than native
    role-based access — i.e. this document must be READ-ONLY for this user
    regardless of their normal team role. Thin wrapper over
    resolve_effective_access() (the single shared resolver) — never
    re-derives native-vs-grant precedence independently.

    The one exception: a stage-scope grant at the contributor_confidential
    tier is explicitly NOT read-only (that tier's entire point is to allow
    writes) — every other kind of grant-derived confidential access
    (document/team-scope, or a stage grant at any other tier reaching this
    function some other way) stays read-only, exactly as before.
    """
    if document.sensitivity_level != SensitivityLevel.confidential:
        return False
    result = resolve_effective_access(db, user_id, document_id=document.document_id)
    if not (result.status == "granted" and result.via_grant):
        return False
    if result.scope == AccessRequestScope.stage and result.tier == GrantTier.contributor_confidential:
        return False
    return True


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

    native_stage_ids: set[UUID] = set()
    if team_ids:
        native_stage_ids = set(db.execute(
            select(TeamStageAccess.stage_id)
            .join(Stage, Stage.stage_id == TeamStageAccess.stage_id)
            .where(TeamStageAccess.team_id.in_(team_ids), Stage.deleted_at.is_(None))
            .distinct()
        ).scalars())

    # Stage grants (spec §1.2/§4): a viewer/contributor/contributor_confidential
    # grant makes its stage visible even with zero native TeamStageAccess.
    # get_active_stage_grants_for_user() is not project-scoped (it's a
    # per-user lookup), so its result is re-filtered to THIS project here.
    grant_stage_ids = set(get_active_stage_grants_for_user(db, user_id).keys())
    if grant_stage_ids:
        grant_stage_ids = set(db.execute(
            select(Stage.stage_id).where(
                Stage.stage_id.in_(grant_stage_ids),
                Stage.project_id == project_id,
                Stage.deleted_at.is_(None),
            )
        ).scalars())

    return list(native_stage_ids | grant_stage_ids)


@dataclass
class StageGrant:
    """
    tier + team_id together are the grant's full entitlement (spec §3.2):
    the tier is only ever exercised AS this specific team, never any
    other team the grant holder might separately belong to. Any code
    checking `tier` for a mutation must also check `team_id` against
    the team the mutation is being attributed to — never one without
    the other.
    """
    tier: GrantTier
    team_id: UUID
    expires_at: datetime | None
    request_id: UUID


def get_active_stage_grants_for_user(db: Session, user_id: UUID) -> dict[UUID, "StageGrant"]:
    """
    stage_id -> StageGrant for every stage where user_id currently holds
    an active grant. The single source every consumer reads from.

    "Active" means: the LATEST terminal event (approval or revocation,
    ranked by COALESCE(revoked_at, decided_at) DESC, request_id DESC as
    a deterministic tie-breaker) for this (user, stage) is itself an
    approval, and that approval is not expired, and the stage itself is
    not soft-deleted.

    Deliberately does NOT simply filter to status == approved and take
    the newest such row — an older approved row must not "reactivate"
    after a newer grant covering the same stage is revoked. Ranking by
    the latest EVENT (regardless of whether that event was an approval
    or a revocation) and only then checking whether it was an approval
    is what makes revocation permanent instead of a fallback to
    whatever was approved before it.
    """
    rows = db.execute(
        select(AccessRequest)
        .join(Stage, Stage.stage_id == AccessRequest.stage_id)
        .where(
            AccessRequest.user_id == user_id,
            AccessRequest.scope == AccessRequestScope.stage,
            AccessRequest.tier.is_not(None),
            AccessRequest.status.in_([AccessRequestStatus.approved, AccessRequestStatus.revoked]),
            Stage.deleted_at.is_(None),
        )
        .order_by(
            func.coalesce(AccessRequest.revoked_at, AccessRequest.decided_at).desc(),
            AccessRequest.request_id.desc(),
        )
    ).scalars().all()

    now = datetime.now(timezone.utc)
    result: dict[UUID, StageGrant] = {}
    seen_stage_ids: set[UUID] = set()
    for r in rows:
        if r.stage_id in seen_stage_ids:
            continue  # a later (in this ordering) row already settled this stage
        seen_stage_ids.add(r.stage_id)
        if r.status != AccessRequestStatus.approved:
            continue  # the latest terminal event for this stage was a revocation
        if r.expires_at is not None:
            exp = r.expires_at if r.expires_at.tzinfo else r.expires_at.replace(tzinfo=timezone.utc)
            if exp < now:
                continue
        result[r.stage_id] = StageGrant(
            tier=r.tier, team_id=r.team_id, expires_at=r.expires_at, request_id=r.request_id,
        )
    return result


def resolve_stage_grant(db: Session, user_id: UUID, stage_id: UUID) -> "StageGrant | None":
    return get_active_stage_grants_for_user(db, user_id).get(stage_id)


@dataclass
class EffectiveAccessResult:
    """
    The resolved answer to "how, if at all, does this user currently have
    access to this specific target" — the single shared verdict consumed by
    both is_grant_only_confidential_access() (mutation enforcement) and
    GET /access-requests/status (UI state). Neither re-derives this
    precedence independently.
    """
    status: str  # "granted" | "pending" | "denied" | "none"
    via_grant: bool = False  # only meaningful when status == "granted"
    expires_at: datetime | None = None
    request_id: UUID | None = None
    scope: "AccessRequestScope | None" = None  # the winning grant's scope, when via_grant
    tier: "GrantTier | None" = None  # the winning grant's tier, when it's a stage-scope grant


def resolve_effective_access(
    db: Session,
    user_id: UUID,
    *,
    document_id: UUID | None = None,
    stage_id: UUID | None = None,
    team_id: UUID | None = None,
) -> EffectiveAccessResult:
    """
    Exactly one of document_id/stage_id/team_id is provided — the caller is
    asking about one specific target. Resolution order (deliberate, and the
    only place this precedence is decided):

      1. Native role access (org admin / project admin / team-lead+ on an
         applicable team) -> granted, via_grant=False. Checked FIRST so a
         user with both native access and an active grant is never treated
         as grant-only (the grant does not downgrade an existing role).
      2. An active (approved, unexpired) grant covering this target ->
         granted, via_grant=True. Checked document-scope, then stage-scope,
         then team-scope — narrowest scope wins when more than one grant
         could apply (e.g. a document covered by both a document grant and
         an overlapping stage grant).
      3. A live pending request for this exact target -> pending.
      4. The latest denied request for this exact target -> denied.
      5. Nothing found (including an approved grant that has since expired,
         with no live denial on record) -> none.

    A stale denied request never suppresses a currently-active grant or a
    currently-pending request — steps 1-2 are checked before request
    history is ever consulted. Likewise, a lapsed (expired) grant is not
    surfaced as a distinct status — it is indistinguishable from having
    never requested at all, unless a denial is also on record.
    """
    if sum(x is not None for x in (document_id, stage_id, team_id)) != 1:
        raise ValueError("Exactly one of document_id, stage_id, team_id must be given")

    if _is_org_admin(db, user_id):
        return EffectiveAccessResult(status="granted", via_grant=False)

    # Resolve the project_id needed for the project_admin check, and the set
    # of teams this target is "native-visible" through (team-lead+ on any of
    # these means native access), per target kind.
    if document_id is not None:
        document = db.get(Document, document_id)
        if document is None:
            return EffectiveAccessResult(status="none")
        if _is_project_admin(db, user_id, document.project_id):
            return EffectiveAccessResult(status="granted", via_grant=False)
        native_team_ids = {
            row.team_id for row in db.execute(
                select(DocumentTeamVisibility).where(DocumentTeamVisibility.document_id == document_id)
            ).scalars()
        }
    elif stage_id is not None:
        stage = db.get(Stage, stage_id)
        if stage is None:
            return EffectiveAccessResult(status="none")
        if _is_project_admin(db, user_id, stage.project_id):
            return EffectiveAccessResult(status="granted", via_grant=False)
        native_team_ids = {
            row.team_id for row in db.execute(
                select(TeamStageAccess).where(TeamStageAccess.stage_id == stage_id)
            ).scalars()
        }
    else:
        team = db.get(Team, team_id)
        if team is None:
            return EffectiveAccessResult(status="none")
        if _is_project_admin(db, user_id, team.project_id):
            return EffectiveAccessResult(status="granted", via_grant=False)
        native_team_ids = {team_id}

    memberships = [
        m for m in (_get_team_membership(db, user_id, tid) for tid in native_team_ids) if m is not None
    ]
    if any(_TEAM_ROLE_RANK[m.role] >= _TEAM_ROLE_RANK[TeamRole.team_lead] for m in memberships):
        return EffectiveAccessResult(status="granted", via_grant=False)

    now = datetime.now(timezone.utc)

    # A target can be covered by a grant of ANY of the three scopes — a
    # document by its own document_id, its stage_id, or any of its native
    # team_ids; a stage by its own stage_id or any of its native team_ids;
    # a team only by its own team_id. Fetch every row that could possibly
    # cover this target in one query rather than one round trip per scope.
    if document_id is not None:
        candidate_team_ids = list(native_team_ids) or [uuid.UUID(int=0)]  # never-matching sentinel if empty
        rows = db.execute(
            select(AccessRequest).where(
                AccessRequest.user_id == user_id,
                (AccessRequest.document_id == document_id)
                | (AccessRequest.stage_id == document.stage_id)
                | (
                    (AccessRequest.team_id.in_(candidate_team_ids))
                    & (AccessRequest.scope == AccessRequestScope.team)
                ),
            )
        ).scalars().all()
    elif stage_id is not None:
        candidate_team_ids = list(native_team_ids) or [uuid.UUID(int=0)]
        rows = db.execute(
            select(AccessRequest).where(
                AccessRequest.user_id == user_id,
                (AccessRequest.stage_id == stage_id)
                | (
                    (AccessRequest.team_id.in_(candidate_team_ids))
                    & (AccessRequest.scope == AccessRequestScope.team)
                ),
            )
        ).scalars().all()
    else:
        rows = db.execute(
            select(AccessRequest).where(
                AccessRequest.user_id == user_id, AccessRequest.team_id == team_id,
                AccessRequest.scope == AccessRequestScope.team,
            )
        ).scalars().all()

    def _is_active_grant(r: AccessRequest) -> bool:
        if r.status != AccessRequestStatus.approved:
            return False
        if r.expires_at is None:
            return True
        exp = r.expires_at if r.expires_at.tzinfo else r.expires_at.replace(tzinfo=timezone.utc)
        return exp >= now

    active_grants = [r for r in rows if _is_active_grant(r)]
    if active_grants:
        # Narrowest-scope-wins tie-break (spec §4.0.2): document, then
        # stage, then team. Only relevant when resolving a document target
        # that could be covered by an overlapping stage/team grant too —
        # a stage or team target query only ever matches its own scope.
        by_scope = {AccessRequestScope.document: [], AccessRequestScope.stage: [], AccessRequestScope.team: []}
        for r in active_grants:
            by_scope[r.scope].append(r)
        # A stage-scope grant only confers CONFIDENTIAL access — this
        # resolver's only concern — when its approver chose the
        # contributor_confidential tier. viewer/contributor stage grants
        # make the stage itself visible (get_accessible_stages_for_user)
        # but never unlock confidential documents through this path.
        # Document/team-scope grants have no tier concept and remain
        # always-confidential-read as originally implemented.
        by_scope[AccessRequestScope.stage] = [
            r for r in by_scope[AccessRequestScope.stage] if r.tier == GrantTier.contributor_confidential
        ]
        for scope in (AccessRequestScope.document, AccessRequestScope.stage, AccessRequestScope.team):
            if by_scope[scope]:
                winner = by_scope[scope][0]
                return EffectiveAccessResult(
                    status="granted", via_grant=True,
                    expires_at=winner.expires_at, request_id=winner.request_id,
                    scope=winner.scope, tier=winner.tier,
                )

    pending = [r for r in rows if r.status == AccessRequestStatus.pending]
    if pending:
        latest_pending = max(pending, key=lambda r: r.requested_at)
        return EffectiveAccessResult(status="pending", request_id=latest_pending.request_id)

    # NOTE: an approved-but-expired grant is deliberately NOT surfaced as a
    # distinct "expired" status here — a lapsed grant with no MORE RECENT
    # denial on record is treated the same as never having requested at all
    # (falls through to "none"). Only an explicit denial is surfaced as
    # terminal history, and only when it is the chronologically LATEST
    # terminal event for this target — an old denial must not outrank a
    # later grant that was subsequently approved and has since lapsed (that
    # sequence reads as "none", same as a lone lapsed grant), so denied and
    # expired-approved rows are ranked together by (decided_at or
    # requested_at) before deciding which status to report.
    terminal_candidates = [
        r for r in rows
        if r.status == AccessRequestStatus.denied
        or (r.status == AccessRequestStatus.approved and not _is_active_grant(r))
    ]
    if terminal_candidates:
        latest = max(terminal_candidates, key=lambda r: (r.decided_at or r.requested_at))
        if latest.status == AccessRequestStatus.denied:
            return EffectiveAccessResult(status="denied", request_id=latest.request_id)

    return EffectiveAccessResult(status="none")


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
    The full ABAC decision for viewing a specific document — bypass, stage
    access, and sensitivity clearance — as a three-way outcome rather than
    can_view_document()'s bool.

    Stage access is the ONLY gate for sub-confidential (public/internal)
    documents: a user has it via team_stage_access (any team they belong
    to in this project) OR a per-user stage grant (resolve_stage_grant,
    via the request-access workflow — lets one specific user see a
    stage's content even when their own team has no native access).
    DocumentTeamVisibility plays NO role in this decision — stage
    assignment IS the document-access grant, not a separate layer on top
    of it.

    Confidential-tier documents return blocked_by_sensitivity (not
    not_visible) when the user has stage access but insufficient
    clearance — the Search Agent (app/services/rag/retrieval.py,
    app/tools/rag_tools.py) relies on this exact distinction to tell a
    user "there's relevant confidential content here, want me to request
    access?" without ever naming the document. The document-LIST endpoint
    (app/routers/documents.py) is a separate consumer that deliberately
    treats blocked_by_sensitivity as excluded from the list entirely
    (never surfaced as a filename or a locked stub) — see its own comment
    for why. Both readings are intentional; this function's three-way
    output does not change based on which consumer is asking.
    """
    if _is_org_admin(db, user_id):
        return DocumentVisibility.fully_allowed
    if _is_project_admin(db, user_id, document.project_id):
        return DocumentVisibility.fully_allowed

    if document.stage_id is None:
        return DocumentVisibility.not_visible

    user_team_ids = list(db.execute(
        select(UserTeamMembership.team_id).where(
            UserTeamMembership.user_id == user_id,
            UserTeamMembership.project_id == document.project_id,
        )
    ).scalars())

    has_native_stage_access = False
    if user_team_ids:
        has_native_stage_access = db.execute(
            select(TeamStageAccess).where(
                TeamStageAccess.team_id.in_(user_team_ids),
                TeamStageAccess.stage_id == document.stage_id,
            )
        ).first() is not None

    stage_grant = resolve_stage_grant(db, user_id, document.stage_id)

    if not has_native_stage_access and stage_grant is None:
        return DocumentVisibility.not_visible  # no stage access via team or per-user grant

    # Sensitivity clearance
    if document.sensitivity_level in (SensitivityLevel.public, SensitivityLevel.internal):
        return DocumentVisibility.fully_allowed  # stage access alone is enough here

    # Confidential tier: team_lead+ on a team with stage access to this
    # stage sees it automatically; otherwise fall back to the shared
    # resolver for an explicit grant.
    memberships = [m for m in (_get_team_membership(db, user_id, tid) for tid in user_team_ids) if m is not None]
    if has_native_stage_access and any(
        _TEAM_ROLE_RANK[m.role] >= _TEAM_ROLE_RANK[TeamRole.team_lead] for m in memberships
    ):
        return DocumentVisibility.fully_allowed

    result = resolve_effective_access(db, user_id, document_id=document.document_id)
    if result.status == "granted":
        return DocumentVisibility.fully_allowed
    return DocumentVisibility.blocked_by_sensitivity


def classify_documents_visibility(
    db: Session,
    auth_context: AuthorizationContext,
    document_ids: list[UUID] | set[UUID],
) -> dict[UUID, DocumentVisibility]:
    """
    Batch ABAC evaluation for candidate documents.
    Executes in 1-2 queries total instead of N queries per candidate document.

    Stage access (native team_stage_access OR a per-user stage grant,
    both folded into auth_context.accessible_stage_ids) is the ONLY gate
    for sub-confidential documents — DocumentTeamVisibility plays no role.
    Confidential documents are fully_allowed only for team_lead+ on a team
    that natively holds this stage's access, or via an explicit grant;
    otherwise blocked_by_sensitivity — a real, known document the caller
    is specifically blocked from (used by the Search Agent / RAG retrieval
    to offer a "request access" suggestion; app/routers/documents.py's
    document-LIST endpoint is a separate consumer that deliberately
    excludes blocked_by_sensitivity documents from what it returns).

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

    # Step 3: Evaluate visibility & sensitivity in-memory per document
    for doc_id, document in doc_map.items():
        stage_tier = auth_context.stage_grant_tiers.get(document.stage_id)
        has_stage_access = document.stage_id in auth_context.accessible_stage_ids

        if not has_stage_access:
            out[doc_id] = DocumentVisibility.not_visible
            continue

        # Sensitivity check: Public & Internal — stage access alone is
        # enough here, independent of DocumentTeamVisibility.
        if document.sensitivity_level in (SensitivityLevel.public, SensitivityLevel.internal):
            out[doc_id] = DocumentVisibility.fully_allowed
            continue

        # Sensitivity check: Confidential
        if document.sensitivity_level == SensitivityLevel.confidential:
            # team_lead+ on a team that NATIVELY holds this stage (a stage
            # reachable only via a per-user grant does not itself confer
            # team_lead auto-unlock — only real team membership does).
            native_team_ids = auth_context.native_stage_team_ids.get(document.stage_id, set())
            roles = [auth_context.team_roles.get(tid) for tid in native_team_ids]
            max_rank = max((_TEAM_ROLE_RANK[r] for r in roles if r in _TEAM_ROLE_RANK), default=-1)

            if max_rank >= _TEAM_ROLE_RANK[TeamRole.team_lead]:
                out[doc_id] = DocumentVisibility.fully_allowed
                continue

            # Active approved grant: document/team scope (tier-less, always
            # confidential-read), or a stage grant specifically at the
            # contributor_confidential tier.
            if (
                doc_id in auth_context.active_confidential_grant_document_ids
                or auth_context.team_ids & auth_context.active_confidential_grant_team_ids
                or stage_tier == GrantTier.contributor_confidential
            ):
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
    project — the COARSE narrowing only: tenant, project, and stage access.
    Sensitivity is deliberately NOT filtered here; that decision belongs
    entirely to can_view_document()'s per-row check (team_lead+ on a team
    with native stage access sees confidential automatically, otherwise
    only with an active grant). Callers must still run each returned row
    through can_view_document().

    For org_admin/project_admin, returns a filter scoped only to
    tenant/project (full visibility within that scope).

    Stage access (native team_stage_access via any of the user's teams, OR
    a per-user stage grant from the request-access workflow) is the ONLY
    gate here — a stage assignment IS the document-access grant, not a
    separate DocumentTeamVisibility layer on top of it. A document whose
    stage the user cannot access never reaches this filter at all,
    regardless of sensitivity; can_view_document() then makes the final
    per-row sensitivity call for documents whose stage DID pass through.
    """
    from sqlalchemy import and_

    user = db.get(User, user_id)

    if user.is_org_admin:
        return Document.tenant_id == user.tenant_id

    if _is_project_admin(db, user_id, project_id):
        return and_(Document.tenant_id == user.tenant_id, Document.project_id == project_id)

    accessible_stage_ids = get_accessible_stages_for_user(db, user_id, project_id)
    if not accessible_stage_ids:
        return Document.document_id == None  # no access — matches nothing

    return and_(
        Document.tenant_id == user.tenant_id,
        Document.project_id == project_id,
        Document.stage_id.in_(accessible_stage_ids),
        # NOTE: no sensitivity condition — every doc in an accessible stage
        # (public, internal AND confidential) passes through here;
        # can_view_document() makes the final per-row sensitivity call.
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