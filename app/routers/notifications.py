"""
Notifications — replaces the permanent frontend stub.

  GET  /notifications              newest-first, for the caller only
  POST /notifications/{id}/read    mark one read (idempotent)
  POST /notifications/read-all     mark every unread one read
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException

from app.api.dependencies import get_current_user, get_db_with_tenant
from app.services.auth import ResolvedIdentity
from app.services.notifications import (
    NotificationNotFound,
    list_for_user,
    mark_all_read,
    mark_read,
)
from sqlalchemy.orm import Session

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("")
def get_notifications(
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    notifications = list_for_user(db, identity.user_id)
    return {
        "notifications": notifications,
        "unread_count": sum(1 for n in notifications if not n["read"]),
    }


@router.post("/{notification_id}/read")
def read_notification(
    notification_id: uuid.UUID,
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    try:
        mark_read(db, identity.user_id, notification_id)
    except NotificationNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "read"}


@router.post("/read-all")
def read_all_notifications(
    identity: ResolvedIdentity = Depends(get_current_user),
    db: Session = Depends(get_db_with_tenant),
):
    count = mark_all_read(db, identity.user_id)
    return {"status": "read", "count": count}
