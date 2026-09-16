"""
Phase 5: document approval workflow endpoints (MERGE_DECISIONS §3/4).

  POST /documents/{document_id}/submit   draft -> pending_review   (contributor+)
  POST /documents/{document_id}/approve  pending_review -> approved (team_lead+)
  POST /documents/{document_id}/reject   pending_review -> rejected (team_lead+, reason required)
  GET  /documents/{document_id}/status   current WorkflowState, or null if the
                                         stage doesn't require approval

All authenticated via get_current_user. The document's own team
(Document.uploaded_as_team_id) and project are what has_permission() checks —
so "team_lead somewhere" is not enough; you must be team_lead on *this*
document's team.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db_with_tenant
from app.models.document import Document
from app.models.workflow import WorkflowState
from app.services.access_control import can_view_document
from app.services.auth import ResolvedIdentity
from app.services.workflow import (
    WorkflowError,
    WorkflowPermissionError,
    WorkflowScanNotPassedError,
    approve_document,
    get_workflow_state,
    reject_document,
    submit_for_review,
)

router = APIRouter(prefix="/documents", tags=["workflow"])


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


class ApproveRequest(BaseModel):
    # org_admin/project_admin only — approve_document() re-checks the role
    # itself, this flag alone grants nothing.
    override: bool = False


class WorkflowStateResponse(BaseModel):
    document_id: str
    state: str
    approved_by: str | None
    approval_timestamp: str | None
    rejection_reason: str | None


def _to_response(state: WorkflowState) -> WorkflowStateResponse:
    return WorkflowStateResponse(
        document_id=str(state.document_id),
        state=state.state.value,
        approved_by=str(state.approved_by) if state.approved_by else None,
        approval_timestamp=(
            state.approval_timestamp.isoformat() if state.approval_timestamp else None
        ),
        rejection_reason=state.rejection_reason,
    )


def _load_document(db: Session, identity: ResolvedIdentity, document_id: uuid.UUID) -> Document:
    doc = db.get(Document, document_id)
    # Cross-tenant documents are reported as not-found, not 403.
    if doc is None or doc.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.post("/{document_id}/submit", response_model=WorkflowStateResponse)
def submit(
    document_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    doc = _load_document(db, identity, document_id)
    role = identity.role_on_team(doc.uploaded_as_team_id, doc.project_id)
    try:
        state = submit_for_review(
            db, document_id, identity.user_id, doc.uploaded_as_team_id, doc.project_id, role
        )
    except WorkflowPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_response(state)


@router.post("/{document_id}/approve", response_model=WorkflowStateResponse)
def approve(
    document_id: uuid.UUID,
    body: ApproveRequest = ApproveRequest(),
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    doc = _load_document(db, identity, document_id)
    role = identity.role_on_team(doc.uploaded_as_team_id, doc.project_id)
    try:
        state = approve_document(
            db, document_id, identity.user_id, doc.uploaded_as_team_id, doc.project_id, role,
            override=body.override,
        )
    except WorkflowPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except WorkflowScanNotPassedError as exc:
        raise HTTPException(
            status_code=422,
            detail={"message": str(exc), "scan": exc.scan_detail},
        ) from exc
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_response(state)


@router.post("/{document_id}/reject", response_model=WorkflowStateResponse)
def reject(
    document_id: uuid.UUID,
    body: RejectRequest,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    doc = _load_document(db, identity, document_id)
    role = identity.role_on_team(doc.uploaded_as_team_id, doc.project_id)
    try:
        state = reject_document(
            db, document_id, identity.user_id, doc.uploaded_as_team_id, doc.project_id,
            role, body.reason,
        )
    except WorkflowPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:  # empty reason
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except WorkflowError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_response(state)


@router.get("/{document_id}/status", response_model=WorkflowStateResponse | None)
def status(
    document_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    doc = _load_document(db, identity, document_id)
    if not can_view_document(db, identity.user_id, doc):
        raise HTTPException(status_code=403, detail="You cannot view this document")
    state = get_workflow_state(db, document_id)
    return _to_response(state) if state is not None else None
