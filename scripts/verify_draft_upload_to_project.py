"""
Verification of 'Upload to Project' for finalized Drafting Agent documents.

Covers all 11 requirements:
  1. A finalized draft can be uploaded directly.
  2. The resulting Document exists in DB.
  3. The resulting DocumentVersion exists in DB with version_number=1.
  4. The correct project/stage/team are associated.
  5. The finalized content is preserved.
  6. The authenticated user must have appropriate upload access (viewer rejected with 403).
  7. An unauthorized user cannot upload another user's finalized draft (403).
  8. A nonexistent draft is rejected (404).
  9. An unfinalized draft cannot be uploaded (404).
  10. No second Structure Scanner invocation occurs during upload-to-project.
  11. No second Injection Scanner invocation occurs during upload-to-project.
"""

import sys
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

from app.main import app
from app.database import SessionLocal
from app.models.user import User
from app.models.project import Project
from app.models.team import Team, UserTeamMembership
from app.models.stage import Stage
from app.models.document import Document, DocumentVersion, DocumentScan
from app.services import draft_workspace
from app.services.auth import create_session_token

client = TestClient(app)
db = SessionLocal()

# Pick a project, team and stage first
project = db.query(Project).first()
assert project, "No project found"
stage = db.query(Stage).filter(Stage.project_id == project.project_id, Stage.deleted_at.is_(None)).first()
assert stage, "No stage found"

# Find users in the project's tenant
alice = db.query(User).filter(User.tenant_id == project.tenant_id, User.email.like("alice%")).first()
bob = db.query(User).filter(User.tenant_id == project.tenant_id, User.email.like("bob%")).first()
charlie = db.query(User).filter(User.tenant_id == project.tenant_id, User.email.like("charlie%")).first()

assert alice and bob and charlie, "Seed users missing in project tenant"

ALICE_AUTH = {"Authorization": f"Bearer {create_session_token(str(alice.user_id))}"}
BOB_AUTH = {"Authorization": f"Bearer {create_session_token(str(bob.user_id))}"}
CHARLIE_AUTH = {"Authorization": f"Bearer {create_session_token(str(charlie.user_id))}"}

# Find a team in this project where bob is a contributor
mem = db.query(UserTeamMembership).filter(
    UserTeamMembership.user_id == bob.user_id,
    UserTeamMembership.role == "contributor",
).first()
team = db.get(Team, mem.team_id)
assert team and team.project_id == project.project_id, "Bob membership mismatch"

PROJECT_ID = str(project.project_id)
TEAM_ID = str(team.team_id)
STAGE_ID = str(stage.stage_id)

FAILURES = []

def check(condition: bool, description: str):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {description}", flush=True)
    if not condition:
        FAILURES.append(description)


MOCK_FINAL_SCAN = {
    "overall_score": 52,
    "criteria": [
        {"name": "completeness", "score": 18, "note": "Covers requirements"},
        {"name": "clarity", "score": 17, "note": "Clear structure"},
        {"name": "feasibility", "score": 17, "note": "Actionable test cases"},
    ],
    "summary": "High quality draft test plan.",
}

def main():
    print(f"\n--- Testing Upload to Project for Finalized Drafts ---", flush=True)
    print(f"Project: {project.name} ({PROJECT_ID})", flush=True)
    print(f"Team: {team.name} ({TEAM_ID})", flush=True)
    print(f"Stage: {stage.name} ({STAGE_ID})", flush=True)

    # -----------------------------------------------------------------------
    # Step A: Create and finalize a draft for Bob
    # -----------------------------------------------------------------------
    bob_session = f"test-upload-bob-{uuid.uuid4().hex[:8]}"
    draft_content = (
        "# User Authentication Test Plan\n\n"
        "## Overview\n"
        "Verification plan for authentication flows including login, lockout, and password reset.\n\n"
        "## Test Cases\n"
        "1. Happy path login with valid credentials\n"
        "2. Invalid password rejected with 401\n"
        "3. Account lockout after 5 failed attempts\n"
    )
    draft_workspace.write_working_draft(bob_session, draft_content)
    assert draft_workspace.has_working_draft(bob_session)

    # Finalize draft as Bob (with fast mock for setup)
    with patch("app.services.draft_workspace.score_document", return_value=MOCK_FINAL_SCAN):
        finalize_res = draft_workspace.finalize(bob_session, user_id=bob.user_id)
    bob_draft_id = finalize_res["draft_id"]
    check(bool(bob_draft_id), "Draft finalized and assigned draft_id")
    check(not draft_workspace.has_working_draft(bob_session), "Working draft deleted after finalization")

    # Verify finalized artifact metadata
    meta = draft_workspace.get_finalized_draft(bob_draft_id, user_id=bob.user_id)
    check(meta["user_id"] == str(bob.user_id), "Finalized draft metadata recorded Bob's user_id")
    check(meta["filename"].endswith(".md"), "Finalized draft has markdown filename")

    # -----------------------------------------------------------------------
    # Requirement 7: Unauthorized user cannot upload another user's draft
    # Alice (different user) attempts to upload Bob's draft_id
    # -----------------------------------------------------------------------
    cross_res = client.post(
        "/agents/draft/upload-to-project",
        headers=ALICE_AUTH,
        json={
            "draft_id": bob_draft_id,
            "project_id": PROJECT_ID,
            "stage_id": STAGE_ID,
            "team_id": TEAM_ID,
            "sensitivity_level": "internal",
        },
    )
    check(cross_res.status_code == 403, f"Cross-user draft upload rejected with 403 (got {cross_res.status_code})")

    # -----------------------------------------------------------------------
    # Requirement 8: Nonexistent draft is rejected (404)
    # -----------------------------------------------------------------------
    fake_res = client.post(
        "/agents/draft/upload-to-project",
        headers=BOB_AUTH,
        json={
            "draft_id": str(uuid.uuid4()),
            "project_id": PROJECT_ID,
            "stage_id": STAGE_ID,
            "team_id": TEAM_ID,
            "sensitivity_level": "internal",
        },
    )
    check(fake_res.status_code == 404, f"Nonexistent draft_id rejected with 404 (got {fake_res.status_code})")

    # -----------------------------------------------------------------------
    # Requirement 9: Unfinalized draft cannot be uploaded
    # An active WIP session cannot be passed as a finalized draft_id
    # -----------------------------------------------------------------------
    unfin_session = f"test-unfin-{uuid.uuid4().hex[:8]}"
    draft_workspace.write_working_draft(unfin_session, "In-progress draft content")
    unfin_res = client.post(
        "/agents/draft/upload-to-project",
        headers=BOB_AUTH,
        json={
            "draft_id": unfin_session,
            "project_id": PROJECT_ID,
            "stage_id": STAGE_ID,
            "team_id": TEAM_ID,
            "sensitivity_level": "internal",
        },
    )
    check(unfin_res.status_code == 404, f"Unfinalized WIP session rejected with 404 (got {unfin_res.status_code})")
    draft_workspace.delete_working_draft(unfin_session)

    # -----------------------------------------------------------------------
    # Requirement 6: Authenticated user must have upload access
    # Charlie (viewer on Engineering, or no upload rights) rejected with 403
    # First create a finalized draft for Charlie
    # -----------------------------------------------------------------------
    charlie_session = f"test-charlie-{uuid.uuid4().hex[:8]}"
    draft_workspace.write_working_draft(charlie_session, "# Charlie Notes\n\nSome notes.")
    with patch("app.services.draft_workspace.score_document", return_value=MOCK_FINAL_SCAN):
        charlie_final = draft_workspace.finalize(charlie_session, user_id=charlie.user_id)
    charlie_draft_id = charlie_final["draft_id"]

    charlie_upload = client.post(
        "/agents/draft/upload-to-project",
        headers=CHARLIE_AUTH,
        json={
            "draft_id": charlie_draft_id,
            "project_id": PROJECT_ID,
            "stage_id": STAGE_ID,
            "team_id": TEAM_ID,
            "sensitivity_level": "internal",
        },
    )
    check(charlie_upload.status_code == 403, f"Viewer (no upload permission) rejected with 403 (got {charlie_upload.status_code})")

    # -----------------------------------------------------------------------
    # Requirements 1, 2, 3, 4, 5, 10, 11:
    # Bob uploads his finalized draft directly into project
    # Assert ZERO scanner invocations occur during this call!
    # -----------------------------------------------------------------------
    with patch("app.services.scan_score.score_document") as mock_score, \
         patch("app.services.injection_scan.scan_for_injection") as mock_injection, \
         patch("app.services.scan_reformer.reform_document") as mock_reform, \
         patch("app.services.document_finalize.run_full_scan") as mock_full_scan:

        up_res = client.post(
            "/agents/draft/upload-to-project",
            headers=BOB_AUTH,
            json={
                "draft_id": bob_draft_id,
                "project_id": PROJECT_ID,
                "stage_id": STAGE_ID,
                "team_id": TEAM_ID,
                "sensitivity_level": "internal",
            },
        )

        check(up_res.status_code == 201, f"Req 1: Upload succeeded with 201 (got {up_res.status_code})")
        check(mock_score.call_count == 0, f"Req 10: Zero score_document calls (calls: {mock_score.call_count})")
        check(mock_injection.call_count == 0, f"Req 11: Zero scan_for_injection calls (calls: {mock_injection.call_count})")
        check(mock_reform.call_count == 0, "Zero reform_document calls")
        check(mock_full_scan.call_count == 0, "Zero run_full_scan calls")

    up_data = up_res.json()
    doc_id = up_data["document_id"]
    ver_id = up_data["version_id"]

    # Requirement 2 & 4: Resulting Document exists with correct project/stage/team
    db.expire_all()
    created_doc = db.get(Document, uuid.UUID(doc_id))
    check(created_doc is not None, "Req 2: Resulting Document exists in DB")
    check(str(created_doc.project_id) == PROJECT_ID, "Req 4: Document associated with correct project_id")
    check(str(created_doc.stage_id) == STAGE_ID, "Req 4: Document associated with correct stage_id")
    check(str(created_doc.uploaded_as_team_id) == TEAM_ID, "Req 4: Document associated with correct team_id")
    check(str(created_doc.uploaded_by) == str(bob.user_id), "Document uploaded_by matches Bob")

    # Requirement 3: Resulting DocumentVersion exists
    created_ver = db.get(DocumentVersion, uuid.UUID(ver_id))
    check(created_ver is not None, "Req 3: Resulting DocumentVersion exists in DB")
    check(created_ver.version_number == 1, "Req 3: Version number is 1")
    check(created_ver.status.value == "pending_review", "DocumentVersion starts at pending_review")

    # Requirement 5: Finalized content is preserved byte-for-byte
    expected_bytes = meta["content_bytes"]
    check(created_ver.file_data == expected_bytes, "Req 5: Finalized content preserved byte-for-byte in DocumentVersion.file_data")

    # DocumentScan row reuse check
    doc_scan = db.query(DocumentScan).filter(DocumentScan.version_id == created_ver.version_id).first()
    if meta.get("score") is not None:
        check(doc_scan is not None and doc_scan.overall_score == meta["score"], f"Existing scan score ({meta['score']}) safely associated with DocumentScan")
    else:
        check(doc_scan is None, "No scan row fabricated when draft was unscored")

    # Clean up test document
    from app.models.document import DocumentTeamVisibility
    from app.models.workflow import WorkflowState
    created_doc.current_version_id = None
    db.commit()
    db.query(DocumentScan).filter(DocumentScan.version_id == created_ver.version_id).delete()
    db.query(WorkflowState).filter(WorkflowState.document_id == created_doc.document_id).delete()
    db.query(DocumentTeamVisibility).filter(DocumentTeamVisibility.document_id == created_doc.document_id).delete()
    db.commit()
    db.delete(created_ver)
    db.commit()
    db.delete(created_doc)
    db.commit()
    print("Cleaned up test document from database.", flush=True)

    print("\n" + "=" * 50, flush=True)
    if not FAILURES:
        print(f"ALL 11 VERIFICATION TESTS PASSED SUCCESSFULLY!", flush=True)
        print("=" * 50, flush=True)
        sys.exit(0)
    else:
        print(f"FAILED TESTS ({len(FAILURES)}):", flush=True)
        for f in FAILURES:
            print(f"  - {f}", flush=True)
        print("=" * 50, flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
