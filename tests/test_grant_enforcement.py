import unittest
import uuid
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.document import Document, DocumentTeamVisibility, DocumentVersion, DocumentStatus, SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.services import draft_workspace
from app.services.document_delete import PermissionDeniedError, delete_version
from app.services.document_finalize import GrantOnlyAccessError, finalize_document_revision
from app.services.workflow import WorkflowPermissionError, submit_for_review


class TestFinalizeGrantEnforcement(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"Enforce Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(project_id=self.project.project_id, name="Enforce Team")
        self.db.add(self.team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Enforce Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()

        self.contributor = User(email=f"c-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.contributor)
        self.db.commit()
        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.team.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.commit()

        version_id = uuid.uuid4()
        self.document = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.contributor.user_id, uploaded_as_team_id=self.team.team_id,
            sensitivity_level=SensitivityLevel.confidential,
            original_filename="doc.md", mime_type="text/markdown",
            current_version_id=None,
        )
        self.db.add(self.document)
        self.db.commit()
        version = DocumentVersion(
            version_id=version_id, document_id=self.document.document_id,
            file_data=b"original", file_size_bytes=8, uploaded_by=self.contributor.user_id,
            status=DocumentStatus.pending_review,
        )
        self.db.add(version)
        self.db.flush()
        self.document.current_version_id = version_id
        self.db.commit()
        self.db.add(DocumentTeamVisibility(document_id=self.document.document_id, team_id=self.team.team_id))
        self.db.commit()

        self.session_id = f"test-session-{uuid.uuid4().hex[:8]}"
        draft_workspace.write_working_draft(self.session_id, "# Revised content\n\nSome text.")

    def tearDown(self):
        draft_workspace.delete_working_draft(self.session_id)
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.team_id == self.team.team_id).delete()
        self.db.query(DocumentTeamVisibility).filter(DocumentTeamVisibility.document_id == self.document.document_id).delete()
        self.db.query(Document).filter(Document.document_id == self.document.document_id).update({"current_version_id": None})
        self.db.query(DocumentVersion).filter(DocumentVersion.document_id == self.document.document_id).delete()
        self.db.query(Document).filter(Document.document_id == self.document.document_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id == self.contributor.user_id).delete()
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_grant_only_contributor_cannot_finalize(self):
        grant = AccessRequest(
            user_id=self.contributor.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()

        with self.assertRaises(GrantOnlyAccessError):
            finalize_document_revision(
                self.db, document_id=self.document.document_id,
                user_id=self.contributor.user_id, session_id=self.session_id,
            )


class TestDeleteVersionGrantEnforcement(TestFinalizeGrantEnforcement):
    def test_grant_only_contributor_cannot_delete_their_own_non_live_version(self):
        # A second, later version the contributor uploaded themselves — the
        # normal can_delete_version() rule (own version, after live) would
        # otherwise allow this.
        second_version_id = uuid.uuid4()
        second_version = DocumentVersion(
            version_id=second_version_id, document_id=self.document.document_id,
            file_data=b"newer draft", file_size_bytes=11, uploaded_by=self.contributor.user_id,
            status=DocumentStatus.pending_review,
        )
        self.db.add(second_version)
        self.db.commit()

        grant = AccessRequest(
            user_id=self.contributor.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()

        with self.assertRaises(PermissionDeniedError):
            delete_version(
                self.db, document_id=self.document.document_id, version_id=second_version_id,
                actor_id=self.contributor.user_id, is_admin=False,
            )


class TestSubmitGrantEnforcement(TestFinalizeGrantEnforcement):
    def tearDown(self):
        # The base tearDown deletes the Document, which would otherwise hit
        # workflow_state's FK constraint given the WorkflowState row this
        # class's test adds — clear it first, then defer to the base cleanup.
        from app.models.workflow import WorkflowState
        self.db.query(WorkflowState).filter(WorkflowState.document_id == self.document.document_id).delete()
        self.db.commit()
        super().tearDown()

    def test_grant_only_contributor_cannot_submit_for_review(self):
        from app.models.workflow import WorkflowState, WorkflowStatus
        self.db.add(WorkflowState(document_id=self.document.document_id, state=WorkflowStatus.draft))
        self.db.commit()

        grant = AccessRequest(
            user_id=self.contributor.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()

        with self.assertRaises(WorkflowPermissionError):
            submit_for_review(
                self.db, self.document.document_id, self.contributor.user_id,
                self.team.team_id, self.project.project_id, "contributor",
            )


if __name__ == "__main__":
    unittest.main()
