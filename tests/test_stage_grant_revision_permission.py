import unittest
import uuid
from datetime import datetime, timezone

from fastapi import HTTPException

from app.database import SessionLocal
from app.models.document import Document, SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier, Team, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.routers.document_review import _check_revision_permission


class TestStageGrantRevisionPermission(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"Revision Grant Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.routing_team = Team(project_id=self.project.project_id, name="Routing Team")
        self.db.add(self.routing_team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Revision Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.routing_team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        self.grant_holder = User(email=f"rgh-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.grant_holder)
        self.db.commit()

        self.document = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.grant_holder.user_id, uploaded_as_team_id=self.routing_team.team_id,
            sensitivity_level=SensitivityLevel.internal,
            original_filename="revise-me.md", mime_type="text/markdown",
        )
        self.db.add(self.document)
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.stage_id == self.stage.stage_id).delete()
        self.db.query(Document).filter(Document.document_id == self.document.document_id).update({"current_version_id": None})
        self.db.query(Document).filter(Document.document_id == self.document.document_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id == self.grant_holder.user_id).delete()
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_no_grant_is_denied(self):
        with self.assertRaises(HTTPException) as ctx:
            _check_revision_permission(self.db, self.grant_holder.user_id, self.document)
        self.assertEqual(ctx.exception.status_code, 403)

    def test_contributor_tier_grant_via_own_routing_team_is_allowed(self):
        grant = AccessRequest(
            user_id=self.grant_holder.user_id, team_id=self.routing_team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.contributor, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        _check_revision_permission(self.db, self.grant_holder.user_id, self.document)  # does not raise
