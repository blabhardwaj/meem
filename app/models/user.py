import uuid
from datetime import datetime, timezone

from sqlalchemy import String, DateTime, Boolean, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class User(Base):
    """
    Minimal user record. Authentication (login/session) is handled by an
    external provider (choice deferred, per Phase 0) — this table just
    stores the verified identity + org-level role used by our own ABAC logic.
    """
    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # Human-readable name for the admin user directory. Nullable — the UI falls
    # back to the email when it's missing.
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_org_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    # Local-auth fields — populated and used starting Phase 2 (password login +
    # reset flow adopted from the teammate's implementation). Nullable so
    # existing/external-identity users are unaffected until then.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reset_token: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reset_token_expires: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Phase 11 — bumped on password change to invalidate every previously
    # issued session token at once (embedded in the token, checked on verify).
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Phase 11 — login lockout after repeated failed attempts.
    failed_login_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
