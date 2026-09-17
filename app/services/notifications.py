"""
Notifications — real per-recipient event rows, replacing the permanent
`notificationsApi` stub.

Scope (user-selected, not guessed): document approved/rejected (-> the
uploader), a document entering pending_review (-> reviewers who can act on
it), a confidential-access request (-> team leads who can decide it) and its
decision (-> the requester), a role assignment/change (-> the affected
user), and a document becoming newly approved+indexed (-> everyone with
view access to it, EXCEPT the approver and the uploader, who already know).

Every create_* function here stages row(s) on the CALLER'S session without
committing — same pattern as app.services.audit.record_audit — so a
notification lands in the same transaction as the action that caused it (an
approval that rolls back creates no notification either). Callers commit.

Fan-out (notify_document_viewers) reuses access_control's own
DocumentTeamVisibility + can_view_document — there is exactly one
"who can see this document" implementation in the codebase; this does not
duplicate that logic, it calls it.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Document, DocumentTeamVisibility
from app.models.notification import Notification, NotificationType, NotificationAudience
from app.models.team import UserTeamMembership, TeamRole
from app.models.user import User
from app.services.access_control import _is_org_admin, can_view_document


def _create(
    db: Session,
    *,
    recipient_user_id: uuid.UUID,
    notification_type: NotificationType,
    title: str,
    body: str | None = None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    audience: NotificationAudience | None = None,
) -> None:
    db.add(Notification(
        recipient_user_id=recipient_user_id,
        notification_type=notification_type,
        title=title,
        body=body,
        resource_type=resource_type,
        resource_id=resource_id,
        project_id=project_id,
        audience=audience,
    ))


def notify_document_decided(
    db: Session, *, document: Document, approved: bool, reason: str | None = None,
) -> None:
    """Document approved/rejected -> the uploader. Skipped if the uploader
    was also the decider (approving/rejecting your own upload — possible
    for a team_lead+ uploader — tells you nothing you don't already know)."""
    _create(
        db,
        recipient_user_id=document.uploaded_by,
        notification_type=(
            NotificationType.document_approved if approved else NotificationType.document_rejected
        ),
        title=f"\"{document.original_filename}\" was {'approved' if approved else 'rejected'}",
        body=reason if not approved else None,
        resource_type="document",
        resource_id=document.document_id,
        project_id=document.project_id,
    )


def notify_pending_review(
    db: Session, *, document: Document, team_id: uuid.UUID,
) -> None:
    """Document entered pending_review -> every reviewer (team_lead on this
    team, or project_admin/org_admin) who can act on it, excluding whoever
    submitted it (the actor)."""
    lead_user_ids = set(db.execute(
        select(UserTeamMembership.user_id).where(
            UserTeamMembership.team_id == team_id,
            UserTeamMembership.role == TeamRole.team_lead,
        )
    ).scalars())

    from app.models.team import ProjectAdmin
    admin_user_ids = set(db.execute(
        select(ProjectAdmin.user_id).where(ProjectAdmin.project_id == document.project_id)
    ).scalars())

    org_admin_user_ids = set(db.execute(
        select(User.user_id).where(User.tenant_id == document.tenant_id, User.is_org_admin.is_(True))
    ).scalars())

    for recipient_id in (lead_user_ids | admin_user_ids | org_admin_user_ids):
        _create(
            db,
            recipient_user_id=recipient_id,
            notification_type=NotificationType.document_pending_review,
            title=f"\"{document.original_filename}\" is waiting for your review",
            resource_type="document",
            resource_id=document.document_id,
            project_id=document.project_id,
        )


def notify_access_request_created(
    db: Session, *, team_id: uuid.UUID, team_name: str, project_id: uuid.UUID,
    requester_id: uuid.UUID, request_id: uuid.UUID,
) -> None:
    """Confidential access requested -> every team_lead on that team."""
    lead_user_ids = set(db.execute(
        select(UserTeamMembership.user_id).where(
            UserTeamMembership.team_id == team_id,
            UserTeamMembership.role == TeamRole.team_lead,
        )
    ).scalars())
    requester = db.get(User, requester_id)
    requester_label = requester.full_name or requester.email if requester else "Someone"
    for recipient_id in lead_user_ids:
        _create(
            db,
            recipient_user_id=recipient_id,
            notification_type=NotificationType.access_request_created,
            title=f"{requester_label} requested confidential access on {team_name}",
            resource_type="access_request",
            resource_id=request_id,
            project_id=project_id,
            audience=NotificationAudience.approver,
        )


def notify_access_request_decided(
    db: Session, *, requester_id: uuid.UUID, team_name: str, project_id: uuid.UUID,
    request_id: uuid.UUID, approved: bool,
) -> None:
    """Confidential-access decision -> the requester."""
    _create(
        db,
        recipient_user_id=requester_id,
        notification_type=(
            NotificationType.access_request_approved if approved
            else NotificationType.access_request_denied
        ),
        title=f"Your confidential access request on {team_name} was {'approved' if approved else 'denied'}",
        resource_type="access_request",
        resource_id=request_id,
        project_id=project_id,
        audience=NotificationAudience.requester,
    )


def notify_role_changed(
    db: Session, *, affected_user_id: uuid.UUID, team_name: str, role: str, project_id: uuid.UUID,
) -> None:
    """Role assigned/changed -> the affected user."""
    _create(
        db,
        recipient_user_id=affected_user_id,
        notification_type=NotificationType.role_changed,
        title=f"Your role on {team_name} is now {role.replace('_', ' ')}",
        resource_type="team",
        project_id=project_id,
    )


def notify_document_viewers(
    db: Session, *, document: Document, exclude_user_ids: set[uuid.UUID],
) -> None:
    """
    A document just became approved AND indexed -> everyone who can
    currently VIEW it (same ABAC check GET /documents uses), except
    `exclude_user_ids` (the approver and the uploader — they already know).

    Candidates are every user on a team the document is visible to
    (DocumentTeamVisibility), narrowed by the real per-user can_view_document
    sensitivity check — reused, not reimplemented. org_admin/project_admin
    also see every document regardless of team visibility, so they're
    included as candidates too.
    """
    visible_team_ids = set(db.execute(
        select(DocumentTeamVisibility.team_id).where(
            DocumentTeamVisibility.document_id == document.document_id
        )
    ).scalars())

    candidate_ids: set[uuid.UUID] = set()
    if visible_team_ids:
        candidate_ids.update(db.execute(
            select(UserTeamMembership.user_id).where(
                UserTeamMembership.team_id.in_(visible_team_ids)
            )
        ).scalars())

    from app.models.team import ProjectAdmin
    candidate_ids.update(db.execute(
        select(ProjectAdmin.user_id).where(ProjectAdmin.project_id == document.project_id)
    ).scalars())
    candidate_ids.update(db.execute(
        select(User.user_id).where(User.tenant_id == document.tenant_id, User.is_org_admin.is_(True))
    ).scalars())

    candidate_ids -= exclude_user_ids
    if not candidate_ids:
        return

    for recipient_id in candidate_ids:
        if not can_view_document(db, recipient_id, document):
            continue
        _create(
            db,
            recipient_user_id=recipient_id,
            notification_type=NotificationType.document_available,
            title=f"\"{document.original_filename}\" is now approved and available",
            resource_type="document",
            resource_id=document.document_id,
            project_id=document.project_id,
        )


def list_for_user(db: Session, user_id: uuid.UUID, limit: int = 50) -> list[dict]:
    """Newest-first notifications for `user_id` (RLS already scopes this to
    their own tenant; the recipient filter here is the actual per-user
    narrowing)."""
    rows = db.execute(
        select(Notification)
        .where(Notification.recipient_user_id == user_id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return [_serialize(n) for n in rows]


def _serialize(n: Notification) -> dict:
    return {
        "notification_id": str(n.notification_id),
        "notification_type": n.notification_type.value,
        "title": n.title,
        "body": n.body,
        "resource_type": n.resource_type,
        "resource_id": str(n.resource_id) if n.resource_id else None,
        "project_id": str(n.project_id) if n.project_id else None,
        "read": n.read_at is not None,
        "created_at": n.created_at.isoformat(),
        "audience": n.audience.value if n.audience else None,
    }


class NotificationNotFound(Exception):
    """The notification doesn't exist, or doesn't belong to this user -> 404."""


def mark_read(db: Session, user_id: uuid.UUID, notification_id: uuid.UUID) -> None:
    from datetime import datetime, timezone
    n = db.get(Notification, notification_id)
    if n is None or n.recipient_user_id != user_id:
        raise NotificationNotFound("Notification not found.")
    if n.read_at is None:
        n.read_at = datetime.now(timezone.utc)
        db.commit()


def mark_all_read(db: Session, user_id: uuid.UUID) -> int:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    result = db.execute(
        select(Notification).where(
            Notification.recipient_user_id == user_id,
            Notification.read_at.is_(None),
        )
    ).scalars().all()
    for n in result:
        n.read_at = now
    db.commit()
    return len(result)
