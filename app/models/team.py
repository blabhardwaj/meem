import enum
import uuid

from sqlalchemy import String, ForeignKey, UniqueConstraint, Enum
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime, timezone
from sqlalchemy import DateTime

from app.database import Base


class TeamRole(str, enum.Enum):
    """
    Per-team role — cumulative hierarchy (each includes the one below):
      team_lead > contributor > viewer
    project_admin and org_admin are handled separately (not per-team roles).
    """
    viewer = "viewer"
    contributor = "contributor"
    team_lead = "team_lead"


class Team(Base):
    """A team within a project (e.g. Engineering, QA, Design, Product)."""
    __tablename__ = "teams"

    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)


class UserTeamMembership(Base):
    """
    Many-to-many: a user can belong to multiple teams within the same project.
    Role varies PER TEAM (e.g. contributor on QA, viewer on Design) — this is
    why role lives here, not on a separate project-level role table.
    """
    __tablename__ = "user_team_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "team_id", "project_id", name="uq_user_team_project"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.team_id"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id"), nullable=False
    )
    role: Mapped[TeamRole] = mapped_column(
        Enum(TeamRole, name="team_role"), nullable=False, default=TeamRole.viewer
    )


class ProjectAdmin(Base):
    """
    Project-wide admin — separate from team roles since admin rights don't
    fragment by team. A project_admin can act as admin on ANY team in the
    project without needing a team_memberships row.
    """
    __tablename__ = "project_admins"
    __table_args__ = (
        UniqueConstraint("user_id", "project_id", name="uq_user_project_admin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id"), nullable=False
    )


class AccessRequestStatus(str, enum.Enum):
    pending = "pending"
    approved = "approved"
    denied = "denied"
    revoked = "revoked"


class AccessRequestScope(str, enum.Enum):
    document = "document"
    stage = "stage"
    team = "team"


class GrantTier(str, enum.Enum):
    """
    Approver-chosen capability tier for a STAGE-scope grant only.
    viewer: see/search public+internal docs in the granted stage.
    contributor: viewer + create/upload/submit in the granted stage,
      attributed to the grant's own routing team (AccessRequest.team_id).
    contributor_confidential: contributor + confidential docs in the
      granted stage. Document/team-scope grants never set this — it
      stays None for those, and they remain always-read-only as before.
    """
    viewer = "viewer"
    contributor = "contributor"
    contributor_confidential = "contributor_confidential"


class GrantDuration(str, enum.Enum):
    """
    Approver-chosen duration label for a STAGE-scope grant, stored
    verbatim (not re-derived from expires_at - decided_at) so the UI can
    display it exactly. hours_72/week_1/month_1 are flat offsets (30 days
    for month_1, not calendar-month arithmetic); unlimited means
    expires_at stays None.
    """
    hours_72 = "hours_72"
    week_1 = "week_1"
    month_1 = "month_1"
    unlimited = "unlimited"


class AccessRequest(Base):
    """
    Contributor's request for elevated (confidential-tier) access with
    document, stage, or team scope, approved/denied by that team's team_lead.
    Approval sets expires_at (90 days out) — expired grants are treated as if
    no grant exists; the user must request again.
    """
    __tablename__ = "access_requests"

    request_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.team_id"), nullable=False
    )
    scope: Mapped[AccessRequestScope] = mapped_column(
        Enum(AccessRequestScope, name="access_request_scope"),
        default=AccessRequestScope.team,
        nullable=False,
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=True
    )
    stage_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stages.stage_id", ondelete="CASCADE"), nullable=True
    )
    status: Mapped[AccessRequestStatus] = mapped_column(
        Enum(AccessRequestStatus, name="access_request_status"),
        default=AccessRequestStatus.pending,
        nullable=False,
    )
    # Free-text justification the requester types when asking for access
    # (e.g. "need to review QA's test coverage before sign-off") — shown to
    # the approver alongside the request. Optional: nullable so existing
    # requests and any caller that doesn't pass one (e.g. programmatic
    # tool calls) are unaffected.
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    tier: Mapped["GrantTier | None"] = mapped_column(
        Enum(GrantTier, name="grant_tier"), nullable=True
    )
    duration: Mapped["GrantDuration | None"] = mapped_column(
        Enum(GrantDuration, name="grant_duration"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )