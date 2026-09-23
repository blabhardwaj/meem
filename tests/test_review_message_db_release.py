"""
Change 1 (PRODUCTION_READINESS_PLAN.md): the review-chat endpoint must not
hold a live DB transaction/connection open across its Groq/agno AI call.

Deliberately does NOT touch a real database — Supabase's pooler is already
at its session-mode connection cap in this environment, and hammering it
with test connections would recreate exactly the problem this change fixes.
Instead this verifies the actual thing that matters: `review_message` calls
`db.commit()` before invoking `run_review_turn` (which is where the AI call
lives), using a plain Mock in place of the DB session and patching
`run_review_turn` to record whether the commit had already happened by the
time it was called.
"""
import uuid
from unittest.mock import MagicMock, patch

from fastapi import BackgroundTasks

from app.routers.document_review import ReviewMessageRequest, review_message
from app.services.auth import ResolvedIdentity


def _fake_identity(tenant_id, user_id):
    return ResolvedIdentity(
        user_id=user_id,
        email="reviewer@example.com",
        tenant_id=tenant_id,
        is_org_admin=True,  # sidesteps _check_revision_permission's own DB reads; it's patched out anyway
        team_memberships=[],
        project_admin_project_ids=[],
    )


def test_review_message_commits_before_ai_call():
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

    call_order = []
    db.commit.side_effect = lambda: call_order.append("commit")

    def fake_run_review_turn(*args, **kwargs):
        call_order.append("ai_call")
        return {
            "reply": "ok", "drafted": False, "finalized": False,
            "version_id": None, "version_number": None, "status": None,
            "scan": None, "scan_error": None, "reformed_content": None,
            "injection_flagged": None, "injection_findings": None,
            "should_index": None, "failed_criteria": [],
        }

    body = ReviewMessageRequest(document_id=document_id, session_id="s1", message="hello")
    identity = _fake_identity(tenant_id, user_id)

    with patch("app.routers.document_review._check_revision_permission"), \
         patch("app.routers.document_review.run_review_turn", side_effect=fake_run_review_turn):
        review_message(body, BackgroundTasks(), identity=identity, db=db)

    assert call_order == ["commit", "ai_call"], (
        f"expected db.commit() before the AI call, got order: {call_order}"
    )


def test_review_message_does_not_commit_on_permission_denial():
    # A denied permission check must not commit — nothing to release yet,
    # and the request is about to 403 out before run_review_turn anyway.
    from fastapi import HTTPException

    tenant_id = uuid.uuid4()
    user_id = uuid.uuid4()
    document_id = uuid.uuid4()

    fake_document = MagicMock()
    fake_document.tenant_id = tenant_id

    db = MagicMock()
    db.get.return_value = fake_document

    body = ReviewMessageRequest(document_id=document_id, session_id="s1", message="hello")
    identity = _fake_identity(tenant_id, user_id)

    with patch(
        "app.routers.document_review._check_revision_permission",
        side_effect=HTTPException(status_code=403, detail="nope"),
    ):
        try:
            review_message(body, BackgroundTasks(), identity=identity, db=db)
            assert False, "expected HTTPException"
        except HTTPException:
            pass

    db.commit.assert_not_called()
