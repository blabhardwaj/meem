import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class NotificationType(str, enum.Enum):
    """
    What kind of event this notification reports — drives the frontend's
    icon/routing, not access control (that's entirely recipient_user_id +
    RLS; the event's own visibility check already ran before this row was
    created, so a notification row is never itself sensitive beyond "this
    happened to/near this specific user").
    """
    document_approved = "document_approved"
    document_rejected = "document_rejected"
    document_pending_review = "document_pending_review"
    document_available = "document_available"
    access_request_created = "access_request_created"
    access_request_approved = "access_request_approved"
    access_request_denied = "access_request_denied"
    role_changed = "role_changed"


class Notification(Base):
    """
    One row per (event, recipient) — the same event fans out to multiple
    rows when it has multiple recipients (e.g. a newly-indexed document
    notifies everyone with view access), never one shared row multiple
    users each mark read independently. resource_id is a bare UUID (no FK),
    matching AuditLog's convention: a notification must survive deletion of
    the thing it refers to.
    """
    __tablename__ = "notifications"

    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    notification_type: Mapped[NotificationType] = mapped_column(
        Enum(NotificationType, name="notification_type"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    resource_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id"), nullable=True
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
