import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AdvisoryDismissal(Base):
    """
    Current durable state of an advisory finding dismissal.
    Keyed to (project_id, finding_fingerprint) so it persists across re-audits.
    Every state transition (dismiss, restore) is immutably recorded in the project AuditLog.
    initial_finding_id is retained purely for provenance/history.
    """
    __tablename__ = "advisory_dismissals"
    __table_args__ = (
        UniqueConstraint("project_id", "finding_fingerprint", name="uq_project_finding_fingerprint"),
        {"schema": "knowledge"},
    )

    dismissal_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id", ondelete="CASCADE"), nullable=False, index=True
    )
    finding_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    rule_code: Mapped[str] = mapped_column(String(16), nullable=False)
    affected_entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    affected_entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)

    # Initial finding provenance
    initial_finding_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), nullable=True)

    # Current dismissal state
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    dismissed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    dismissed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # Last transition info
    restored_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )
    restored_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
