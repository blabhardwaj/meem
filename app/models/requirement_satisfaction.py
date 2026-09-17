import uuid
from datetime import datetime, timezone

from sqlalchemy import String, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RequirementSatisfaction(Base):
    """
    Current-state projection of "which document satisfies which requirement"
    — NOT an immutable historical audit record like AuditFinding/AuditRun.
    requirement_id is UNIQUE: at most one satisfier per requirement at any
    time. Rewritten wholesale (delete-then-insert) every audit run by
    execute_project_audit, so a row can never outlive the evidence that
    produced it. audit_run_id is provenance only ("which run last confirmed
    this"), not a history key — do not add queries assuming multiple rows
    per requirement_id can coexist.
    """
    __tablename__ = "requirement_satisfactions"

    satisfaction_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    requirement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("required_documents.requirement_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.document_id", ondelete="CASCADE"), nullable=False
    )
    matched_via: Mapped[str] = mapped_column(String(32), nullable=False)
    audit_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("knowledge.audit_runs.run_id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
