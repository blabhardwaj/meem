"""
SUPERSEDED (Phase 3) — not registered in app.main anymore.

The authenticated JSON upload path now lives in app/routers/documents.py
(POST /documents/upload + GET /documents). This module is kept only for its
multipart/file-bytes helpers (_validate_file, _resolve_uploading_as_team),
which a future real file-upload endpoint can reuse. Its endpoint still uses
the pre-auth `uploaded_by` / `tenant_id` form fields and is intentionally
left unwired.
"""

import os
import uuid

from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select

from app.config import ALLOWED_EXTENSIONS, MAX_UPLOAD_SIZE_BYTES
from app.database import get_db
from app.models import (
    Document,
    DocumentVersion,
    DocumentTeamVisibility,
    UserTeamMembership,
)
from app.models.document import SensitivityLevel, DocumentStatus
from app.schemas.document import UploadResponse

router = APIRouter(prefix="/documents", tags=["documents"])


def _validate_file(filename: str, mime_type: str, size_bytes: int) -> None:
    """Phase 1 §4: reject unsupported formats / oversized files at upload time."""
    ext = os.path.splitext(filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file format '{ext}'. Allowed: {sorted(ALLOWED_EXTENSIONS)}",
        )
    if size_bytes > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"File exceeds the {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)}MB limit. "
                "This is a temporary limit tied to local Postgres-blob storage and will be "
                "raised once the project moves to object storage (MinIO/S3)."
            ),
        )


def _resolve_uploading_as_team(
    db: Session, user_id: uuid.UUID, project_id: uuid.UUID, requested_team_id: uuid.UUID | None
) -> uuid.UUID:
    """
    Phase 1 §1: resolves which team the uploader is "acting as".
    - If the user belongs to exactly one team on this project, use it (no picker needed).
    - If they belong to multiple teams, requested_team_id must be provided explicitly.
    - If they belong to none, this is a permissions error (should not be able to upload).
    """
    memberships = db.execute(
        select(UserTeamMembership).where(
            UserTeamMembership.user_id == user_id,
            UserTeamMembership.project_id == project_id,
        )
    ).scalars().all()

    if not memberships:
        raise HTTPException(
            status_code=403,
            detail="User has no team membership on this project — cannot upload.",
        )

    team_ids = {m.team_id for m in memberships}

    if requested_team_id is not None:
        if requested_team_id not in team_ids:
            raise HTTPException(
                status_code=403,
                detail="User is not a member of the specified 'uploading as' team on this project.",
            )
        return requested_team_id

    if len(team_ids) == 1:
        return next(iter(team_ids))

    raise HTTPException(
        status_code=400,
        detail=(
            "User belongs to multiple teams on this project — "
            "'uploading_as_team_id' must be specified explicitly."
        ),
    )


@router.post("/upload", response_model=UploadResponse)
def upload_document(
    project_id: uuid.UUID = Form(...),
    stage_id: uuid.UUID = Form(...),
    uploaded_by: uuid.UUID = Form(...),  # TODO: replace with auth-derived current user once auth is wired up
    tenant_id: uuid.UUID = Form(...),  # TODO: derive from user's tenant once auth is wired up
    uploading_as_team_id: uuid.UUID | None = Form(None),
    sensitivity_level: SensitivityLevel = Form(SensitivityLevel.internal),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """
    Phase 1 upload flow:
      - project_id, stage_id: mandatory (per §1)
      - uploading_as_team_id: optional if user belongs to only one team on the
        project; required (as a picker) if they belong to multiple (per §1)
      - sensitivity_level: optional, defaults to Internal (per §1)
      - format/size validated per §4
      - always creates a NEW document + version 1 (this endpoint is the
        "normal add document" flow, not "upload new version" — per §2)
      - sets status = pending_review, which the async polling worker (§5)
        will later pick up for the Structure Scanner
    """
    file_bytes = file.file.read()
    size_bytes = len(file_bytes)

    _validate_file(file.filename, file.content_type, size_bytes)

    resolved_team_id = _resolve_uploading_as_team(
        db, uploaded_by, project_id, uploading_as_team_id
    )

    document = Document(
        tenant_id=tenant_id,
        project_id=project_id,
        stage_id=stage_id,
        uploaded_by=uploaded_by,
        uploaded_as_team_id=resolved_team_id,
        sensitivity_level=sensitivity_level,
        original_filename=file.filename,
        mime_type=file.content_type,
    )
    db.add(document)
    db.flush()  # get document.document_id before creating the version

    version = DocumentVersion(
        document_id=document.document_id,
        file_data=file_bytes,
        file_size_bytes=size_bytes,
        uploaded_by=uploaded_by,
        status=DocumentStatus.pending_review,
    )
    db.add(version)
    db.flush()

    document.current_version_id = version.version_id

    # Default visibility: the "uploading as" team (per §1). Editable later by team_lead+/project_admin.
    visibility = DocumentTeamVisibility(document_id=document.document_id, team_id=resolved_team_id)
    db.add(visibility)

    db.commit()
    db.refresh(document)
    db.refresh(version)

    return UploadResponse(
        document_id=document.document_id,
        version_id=version.version_id,
        original_filename=document.original_filename,
        status=version.status,
        sensitivity_level=document.sensitivity_level,
        uploaded_as_team_id=document.uploaded_as_team_id,
    )
