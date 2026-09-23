import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AIUsage(Base):
    """
    Change 4 (PRODUCTION_READINESS_PLAN.md): one row per tenant, a running
    counter against a configurable limit. Deliberately minimal — no
    time-bucketed reset, no per-feature breakdown — the smallest thing that
    lets check_and_consume_ai_usage() reject a tenant cleanly once
    exhausted. tenant_id IS the primary key (one row per tenant, not an
    append-only log).
    """
    __tablename__ = "ai_usage"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    calls_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    calls_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=2000)
    period_started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
