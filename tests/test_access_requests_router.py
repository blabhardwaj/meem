import unittest
import uuid
from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.api.dependencies import get_current_user
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.services.auth import resolve_identity


class TestAccessStatusEndpoint(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            self.assertIsNotNone(self.tenant, "Tenant required")
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"Status Endpoint Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(project_id=self.project.project_id, name="Status Team")
        self.db.add(self.team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Status Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        self.viewer = User(email=f"status-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.viewer)
        self.db.commit()
        self.db.add(UserTeamMembership(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            project_id=self.project.project_id, role=TeamRole.viewer,
        ))
        self.db.commit()

        self.identity = resolve_identity(self.db, self.viewer.user_id)
        app.dependency_overrides[get_current_user] = lambda: self.identity
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.team_id == self.team.team_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id == self.viewer.user_id).delete()
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.team_id == self.team.team_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_active_viewer_tier_grant_reports_granted_not_none(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.viewer, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        self.db.add(grant)
        self.db.commit()
        resp = self.client.get(f"/access-requests/status?stage_id={self.stage.stage_id}")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Before this task's fix, this would report "none" — the ordinary
        # confidential-access resolver correctly doesn't count a viewer-tier
        # grant as "granted" for CONFIDENTIAL purposes, but the UI polling
        # this endpoint (StageAccessLink, LockedDocumentRow) needs to know
        # the grant exists at all, regardless of tier.
        self.assertEqual(body["status"], "granted")
        self.assertTrue(body["via_grant"])

    def test_no_grant_still_reports_none(self):
        resp = self.client.get(f"/access-requests/status?stage_id={self.stage.stage_id}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "none")
