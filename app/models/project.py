import uuid
from datetime import datetime, timezone

from sqlalchemy import String, DateTime, Float, Integer, Text, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Project(Base):
    """A project within a tenant. Every project belongs to exactly one tenant."""
    __tablename__ = "projects"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Manual completion marking (project_admin/org_admin only). A persisted
    # human decision, not a computed detector -- see
    # AUDIT_RULE_TAXONOMY_MATRIX.md's fourth layer. completed_at is the sole
    # source of truth for "is this project marked complete"; the three
    # completion_snapshot_* columns exist only to answer "has anything that
    # matters changed since" on the next audit run (app/services/graph/completion.py).
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id", ondelete="SET NULL"), nullable=True
    )
    completion_snapshot_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    completion_snapshot_blockers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_snapshot_signature: Mapped[str | None] = mapped_column(Text, nullable=True)
