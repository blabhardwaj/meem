import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, Integer, DateTime, ForeignKey, Enum, LargeBinary
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from sqlalchemy import Boolean, Text
from sqlalchemy.dialects.postgresql import JSONB

from app.database import Base


class SensitivityLevel(enum.IntEnum):
    """
    Tiered sensitivity — used in ABAC clearance checks (user clearance >= doc
    level). An IntEnum so the check is a plain integer comparison
    (public < internal < confidential) with no separate rank lookup, per the
    merge decision (§1). Stored as a plain integer column (0/1/2), NOT a
    Postgres enum type.
    """
    public = 0
    internal = 1
    confidential = 2


class SensitivityLevelType(TypeDecorator):
    """
    Persists SensitivityLevel as its integer value (0/1/2) and hydrates it
    back into a SensitivityLevel on load, so the mapped attribute stays a
    proper enum member rather than a bare int.
    """
    impl = Integer
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return int(SensitivityLevel(value))

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return SensitivityLevel(value)


class DocumentStatus(str, enum.Enum):
    """
    Per-VERSION status (lives on document_versions, not documents) since each
    new version needs its own independent review pass.
    """
    indexed = "indexed"
    pending_review = "pending_review"
    needs_attention = "needs_attention"


class Document(Base):
    """
    Stable document identity — describes "this document" conceptually
    (project, stage, sensitivity). Actual file bytes live in DocumentVersion.

    tenant_id is deliberately DENORMALIZED here (not solely inferred via
    project_id -> tenant_id join) for defense-in-depth tenant isolation:
    every query can filter tenant_id directly without depending on a join
    being correct.
    """
    __tablename__ = "documents"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.tenant_id"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("projects.project_id"), nullable=False
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stages.stage_id"), nullable=False
    )
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    # The team the uploader was "acting as" at upload time (per Phase 1 decision).
    # Also seeds the initial row in document_team_visibility.
    uploaded_as_team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.team_id"), nullable=False
    )
    sensitivity_level: Mapped[SensitivityLevel] = mapped_column(
        SensitivityLevelType,
        default=SensitivityLevel.internal,
        nullable=False,
    )
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(
       UUID(as_uuid=True),
       ForeignKey("document_versions.version_id", use_alter=True, name="fk_documents_current_version_id"),
       nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    def logical_path(self, ext: str) -> str:
        """
        Computed logical path — not a real filesystem/S3 path today (we're on
        Postgres blobs per Phase 0), but pre-computing this now means a future
        MinIO/S3 migration has zero path-design ambiguity.
        """
        return f"{self.tenant_id}/{self.project_id}/{self.document_id}/original{ext}"

class ScanReviewStatus(str, enum.Enum):
    """
    Review status for a document_scans row.
    'not_required' -> score was >= threshold, no reform, no review needed
    'pending'      -> score was < threshold, reform generated, awaiting human decision
    'accepted'     -> reviewer accepted the reformed content as-is
    'edited_accepted' -> reviewer edited the reformed content, then accepted
    'rejected'     -> reviewer rejected the reform; original stays as-is, nothing indexed
    """
    not_required = "not_required"
    pending = "pending"
    accepted = "accepted"
    edited_accepted = "edited_accepted"
    rejected = "rejected"


class DocumentScan(Base):
    """
    One row per Structure Scanner run on a document_version. Holds the
    LLM-generated score/rubric breakdown, the reformed content (if reform
    was triggered), and the human review decision. Original file bytes in
    DocumentVersion.file_data are never touched by any of this.
    """
    __tablename__ = "document_scans"

    scan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("document_versions.version_id"), nullable=False
    )
    overall_score: Mapped[int] = mapped_column(Integer, nullable=False)
    criteria: Mapped[dict] = mapped_column(JSONB, nullable=False)  # [{name, score, note}, ...]

    reform_triggered: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reformed_content: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Injection Scanner (Phase B gap-closer's third check, alongside
    # score/reform) — separate axis from structural quality. A flagged scan
    # blocks `indexed` status regardless of overall_score; see
    # app.services.injection_scan.
    injection_flagged: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    injection_findings: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    review_status: Mapped[ScanReviewStatus] = mapped_column(
        Enum(ScanReviewStatus, name="scan_review_status"),
        default=ScanReviewStatus.not_required,
        nullable=False,
    )
    edited_content: Mapped[str | None] = mapped_column(Text, nullable=True)

    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    
class VersionApprovalOutcome(str, enum.Enum):
    """
    A version's resolved fate, independent of DocumentStatus (which is
    purely the Scanner's verdict on content quality). None until resolved —
    a version sitting in draft/pending_review/needs_attention has no
    outcome yet. 'approved' is set on exactly the version approve_document()
    promotes to current (which also assigns it its version_number).
    'rejected' covers BOTH an explicit human reject() call AND a version
    that was still unresolved when a later version got approved instead
    (auto-superseded) — deliberately the same value/label for both, per
    product decision: the versions list doesn't distinguish "a reviewer
    said no" from "a newer draft won instead," both are dead ends shown as
    "Rejected".
    """
    approved = "approved"
    rejected = "rejected"


class DocumentVersion(Base):
    """
    Actual file bytes for one version of a document. Structure Scanner review
    status, chunking, and embedding all operate PER VERSION. Only the latest
    indexed version is searchable at any time; older versions are preserved
    (never deleted) but excluded from active RAG retrieval.

    New versions are only created via an explicit "upload new version" action
    from within an existing document's context — never automatically/via hash
    detection. The normal add-document flow always creates a new Document.

    version_number means "the Nth version of this document to ever be
    APPROVED" — nullable, assigned ONLY by approve_document(), never at
    creation/finalize time. A version with no number yet is a draft/pending/
    needs_attention attempt that hasn't been resolved either way; the
    versions list shows it unnumbered (as "Draft"), distinct from the
    numbered "v1, v2, v3..." lineage of what was actually approved over
    time. See approval_outcome for how a version's fate is resolved.
    """
    __tablename__ = "document_versions"

    version_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.document_id"), nullable=False
    )
    version_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=False
    )
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(DocumentStatus, name="document_status"),
        default=DocumentStatus.pending_review,
        nullable=False,
    )
    approval_outcome: Mapped[VersionApprovalOutcome | None] = mapped_column(
        Enum(VersionApprovalOutcome, name="version_approval_outcome"),
        nullable=True,
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.user_id"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provenance_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("audit_log.log_id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )



class DocumentTeamVisibility(Base):
    """Many-to-many: which teams can see a document, per the ABAC model (Phase 0 §5)."""
    __tablename__ = "document_team_visibility"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.document_id"), nullable=False
    )
    team_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("teams.team_id"), nullable=False
    )


class DocumentStageReference(Base):
    """
    Optional many-to-many: lets a document also SURFACE in other stages'
    search/dashboard views (e.g. a Test Plan drafted in Development but
    referenced throughout Testing) WITHOUT changing which stage owns it for
    gap-detection/coverage math (that's still Document.stage_id, single-value).
    """
    __tablename__ = "document_stage_references"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.document_id"), nullable=False
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("stages.stage_id"), nullable=False
    )
