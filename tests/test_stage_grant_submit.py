import unittest
import uuid
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.document import Document, SensitivityLevel
from app.models.notification import Notification
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier, Team, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workflow import WorkflowState, WorkflowStatus
from app.services.workflow import WorkflowPermissionError, submit_for_review


class TestStageGrantSubmit(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"Submit Grant Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.routing_team = Team(project_id=self.project.project_id, name="Routing Team")
        self.db.add(self.routing_team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Submit Stage", order_index=1, requires_approval=True)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.routing_team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        self.grant_holder = User(email=f"sgh-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.grant_holder)
        self.db.commit()

        self.document = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.grant_holder.user_id, uploaded_as_team_id=self.routing_team.team_id,
            sensitivity_level=SensitivityLevel.internal,
            original_filename="submit-me.md", mime_type="text/markdown",
        )
        self.db.add(self.document)
        self.db.commit()
        self.db.add(WorkflowState(document_id=self.document.document_id, state=WorkflowStatus.draft))
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(Notification).filter(Notification.project_id == self.project.project_id).delete()
        self.db.query(AccessRequest).filter(AccessRequest.stage_id == self.stage.stage_id).delete()
        self.db.query(WorkflowState).filter(WorkflowState.document_id == self.document.document_id).delete()
        self.db.query(Document).filter(Document.document_id == self.document.document_id).update({"current_version_id": None})
        self.db.query(Document).filter(Document.document_id == self.document.document_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        # grant_holder is left in place — record_audit() on a successful
        # submit_for_review() call may have created an audit_log row
        # referencing this user, and that table's FK is append-only.
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_no_grant_is_denied(self):
        with self.assertRaises(WorkflowPermissionError):
            submit_for_review(
                self.db, self.document.document_id, self.grant_holder.user_id,
                self.routing_team.team_id, self.project.project_id, "viewer",
            )

    def test_contributor_tier_grant_can_submit(self):
        grant = AccessRequest(
            user_id=self.grant_holder.user_id, team_id=self.routing_team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.contributor, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        state = submit_for_review(
            self.db, self.document.document_id, self.grant_holder.user_id,
            self.routing_team.team_id, self.project.project_id, "viewer",
        )
        self.assertEqual(state.state, WorkflowStatus.pending_review)
