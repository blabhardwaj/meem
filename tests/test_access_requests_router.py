import unittest
import uuid
from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.api.dependencies import get_current_user
from app.models.notification import Notification
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantDuration, GrantTier, Team, TeamRole, UserTeamMembership
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


class TestApproveAndRevokeStageGrant(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"Approve Revoke Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(project_id=self.project.project_id, name="Approve Team")
        self.db.add(self.team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Approve Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        self.team_lead = User(email=f"lead-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.requester = User(email=f"req-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add_all([self.team_lead, self.requester])
        self.db.commit()
        self.db.add(UserTeamMembership(
            user_id=self.team_lead.user_id, team_id=self.team.team_id,
            project_id=self.project.project_id, role=TeamRole.team_lead,
        ))
        self.db.add(UserTeamMembership(
            user_id=self.requester.user_id, team_id=self.team.team_id,
            project_id=self.project.project_id, role=TeamRole.viewer,
        ))
        self.db.commit()

        self.request = AccessRequest(
            user_id=self.requester.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            status=AccessRequestStatus.pending,
        )
        self.db.add(self.request)
        self.db.commit()

        self.lead_identity = resolve_identity(self.db, self.team_lead.user_id)
        app.dependency_overrides[get_current_user] = lambda: self.lead_identity
        self.client = TestClient(app)

    def tearDown(self):
        # team_lead/requester are left in place — approve/revoke each call
        # record_audit(), and audit_log's FK is enforced append-only, so
        # hard-deleting either user here would raise ForeignKeyViolation.
        app.dependency_overrides.clear()
        self.db.rollback()
        self.db.query(Notification).filter(Notification.project_id == self.project.project_id).delete()
        self.db.query(AccessRequest).filter(AccessRequest.team_id == self.team.team_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.team_id == self.team.team_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_approving_stage_request_without_tier_and_duration_is_422(self):
        resp = self.client.post(f"/access-requests/{self.request.request_id}/approve", json={})
        self.assertEqual(resp.status_code, 422)

    def test_approving_with_tier_and_duration_sets_expires_at_and_serializes_both(self):
        resp = self.client.post(
            f"/access-requests/{self.request.request_id}/approve",
            json={"tier": "contributor", "duration": "week_1"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["tier"], "contributor")
        self.assertEqual(body["requester_role"], "viewer")
        self.assertEqual(body["status"], "approved")
        expires_at = datetime.fromisoformat(body["expires_at"])
        expected = datetime.now(timezone.utc) + timedelta(days=7)
        self.assertLess(abs((expires_at - expected).total_seconds()), 30)

    def test_revoke_transitions_approved_to_revoked(self):
        self.client.post(
            f"/access-requests/{self.request.request_id}/approve",
            json={"tier": "contributor", "duration": "unlimited"},
        )
        resp = self.client.post(f"/access-requests/{self.request.request_id}/revoke")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "revoked")

        from app.services.access_control import resolve_stage_grant
        self.assertIsNone(resolve_stage_grant(self.db, self.requester.user_id, self.stage.stage_id))

    def test_revoke_on_a_pending_request_is_409(self):
        resp = self.client.post(f"/access-requests/{self.request.request_id}/revoke")
        self.assertEqual(resp.status_code, 409)
