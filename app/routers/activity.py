"""
Project Activity endpoints (finalized design — see app/services/activity.py).

  GET /activity/projects              project cards the caller can open, each
                                      with the teams they may select
  GET /activity?project_id&team_id    flat, sensitivity-filtered activity feed
                                      for one team

Visible to org_admin, project_admin, team_lead and contributor. A viewer-only
user gets an empty /activity/projects list and a 403 from /activity.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.dependencies import get_current_user, get_db_with_tenant
from app.services.activity import (
    ActivityAccessError,
    ActivityNotFound,
    list_activity_projects,
    list_team_activity,
)
from app.services.auth import ResolvedIdentity

router = APIRouter(prefix="/activity", tags=["activity"])


@router.get("/projects")
def activity_projects(
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    return list_activity_projects(db, identity)


@router.get("")
def activity_feed(
    project_id: uuid.UUID = Query(...),
    team_id: uuid.UUID = Query(...),
    limit: int = Query(200, ge=1, le=500),
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    try:
        return list_team_activity(db, identity, project_id, team_id, limit)
    except ActivityNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ActivityAccessError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
