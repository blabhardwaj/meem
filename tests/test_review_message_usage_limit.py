"""
Change 4 (PRODUCTION_READINESS_PLAN.md): a tenant that has exhausted its AI
usage limit gets a clean 429 from review_message, not a 502 swallowed by the
generic Groq/rate-limit exception handler. Mock-based, no live DB — mirrors
tests/test_review_message_db_release.py's approach.
"""
import uuid
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks, HTTPException

from app.routers.document_review import ReviewMessageRequest, review_message
from app.services.ai_usage import AIUsageLimitExceededError
from app.services.auth import ResolvedIdentity


def test_review_message_returns_429_when_usage_exhausted():
    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    document_id = uuid.uuid4()

    fake_document = MagicMock()
    fake_document.tenant_id = tenant_id
    fake_document.document_id = document_id
    fake_document.uploaded_as_team_id = uuid.uuid4()
    fake_document.project_id = uuid.uuid4()

    db = MagicMock()
    db.get.return_value = fake_document

    body = ReviewMessageRequest(document_id=document_id, session_id="s1", message="hello")
    identity = ResolvedIdentity(
        user_id=user_id, email="reviewer@example.com", tenant_id=tenant_id,
        is_org_admin=True, team_memberships=[], project_admin_project_ids=[],
    )

    with patch("app.routers.document_review._check_revision_permission"), \
         patch(
             "app.routers.document_review.run_review_turn",
             side_effect=AIUsageLimitExceededError(tenant_id),
         ):
        try:
            review_message(body, BackgroundTasks(), identity=identity, db=db)
            assert False, "expected HTTPException"
        except HTTPException as exc:
            assert exc.status_code == 429
