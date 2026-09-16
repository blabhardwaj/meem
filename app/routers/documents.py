"""
Phase 3: authenticated document endpoints.

  POST /documents/upload  — create a document as the authenticated user,
                            acting as one of their teams (team_id in body).
                            Enforced by has_permission() inside create_document().
  GET  /documents         — list documents in a project the caller can actually
                            see, via build_access_filter() + can_view_document().
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db_with_tenant
from app.database import get_db

from app.models.document import Document
from app.models.project import Project
from app.models.team import Team
from app.models.workflow import WorkflowState
from app.services.access_control import build_access_filter, can_view_document
from app.services.auth import ResolvedIdentity
from app.services.document_persistence import (
    PermissionDeniedError,
    StageNotFoundError,
    create_document,
)

router = APIRouter(prefix="/documents", tags=["documents"])


# --- models ----------------------------------------------------------------

class DocumentUploadRequest(BaseModel):
    document_type: str = Field(min_length=1, max_length=200)
    stage_id: uuid.UUID
    content: str = Field(min_length=1)
    # Which of the caller's teams they are uploading as — required, since a
    # user can belong to several teams (see erin: Engineering + Design).
    team_id: uuid.UUID
    sensitivity_level: str = "internal"  # "public" | "internal" | "confidential"


class DocumentUploadResponse(BaseModel):
    document_id: str
    version_id: str
    stage_id: str
    stage_name: str
    sensitivity_level: str
    uploaded_as_team_id: str
    # "draft" if the stage requires approval, else null.
    workflow_state: str | None


class DocumentListItem(BaseModel):
    document_id: str
    original_filename: str
    project_id: str
    stage_id: str
    sensitivity_level: str
    uploaded_as_team_id: str
    # Current approval state, or null if the stage doesn't require approval.
    workflow_state: str | None
    uploaded_by: str


# --- endpoints -----------------------------------------------------------

@router.post("/upload", response_model=DocumentUploadResponse, status_code=201)
def upload_document(
    body: DocumentUploadRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):

    team = db.get(Team, body.team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")

    project = db.get(Project, team.project_id)
    if project is None or project.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=403, detail="That team is not in your organization")

    role = identity.role_on_team(body.team_id, project.project_id)

    try:
        created = create_document(
            db,
            user_id=identity.user_id,
            team_id=body.team_id,
            project_id=project.project_id,
            role=role,
            document_type=body.document_type,
            stage_id=body.stage_id,
            content=body.content,
            sensitivity=body.sensitivity_level,
        )
    except PermissionDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except StageNotFoundError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:  # bad sensitivity name, unknown user, etc.
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return DocumentUploadResponse(
        document_id=str(created.document_id),
        version_id=str(created.version_id),
        stage_id=str(created.stage_id),
        stage_name=created.stage_name,
        sensitivity_level=created.sensitivity_level.name,
        uploaded_as_team_id=str(body.team_id),
        workflow_state=created.workflow_state,
    )


@router.get("", response_model=list[DocumentListItem])
def list_documents(
    project_id: uuid.UUID = Query(..., description="Project to list documents for"),
    include_drafts: bool = Query(
        False,
        description=(
            "Only honoured for org_admin/project_admin — a regular contributor "
            "always sees just their own drafts regardless of this flag."
        ),
    ),
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):

    access_filter = build_access_filter(db, identity.user_id, project_id)
    rows = db.execute(
        select(Document).where(access_filter, Document.project_id == project_id)
    ).scalars().all()

    # Row-level refine: build_access_filter is a coarse pre-filter; the final
    # authority (esp. for confidential) is can_view_document().
    visible = [d for d in rows if can_view_document(db, identity.user_id, d)]

    wf_by_doc = {
        w.document_id: w.state.value
        for w in db.execute(
            select(WorkflowState).where(
                WorkflowState.document_id.in_([d.document_id for d in visible])
            )
        ).scalars()
    } if visible else {}

    # Master Plan v2, item 4: an in-progress draft is only the author's business
    # until they submit it. `include_drafts=True` is server-side gated to
    # org_admin/project_admin — a contributor passing it anyway still only sees
    # their own drafts, never anyone else's.
    can_see_all_drafts = identity.is_org_admin or (project_id in identity.project_admin_project_ids)
    if not (include_drafts and can_see_all_drafts):
        visible = [
            d for d in visible
            if wf_by_doc.get(d.document_id) != "draft" or d.uploaded_by == identity.user_id
        ]

    return [
        DocumentListItem(
            document_id=str(d.document_id),
            original_filename=d.original_filename,
            project_id=str(d.project_id),
            stage_id=str(d.stage_id),
            sensitivity_level=d.sensitivity_level.name,
            uploaded_as_team_id=str(d.uploaded_as_team_id),
            workflow_state=wf_by_doc.get(d.document_id),
            uploaded_by=str(d.uploaded_by),
        )
        for d in visible
    ]
