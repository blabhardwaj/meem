import unittest
import uuid
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.services.document_persistence import PermissionDeniedError, StageNotFoundError, _check_upload_access


class TestStageGrantUploadAccess(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"Upload Grant Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.routing_team = Team(project_id=self.project.project_id, name="Routing Team")
        self.other_team = Team(project_id=self.project.project_id, name="Other Team")
        self.db.add_all([self.routing_team, self.other_team])
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Upload Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.routing_team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        # The grant holder has ZERO membership on either team — proving the
        # grant path works without any native role at all.
        self.grant_holder = User(email=f"gh-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.grant_holder)
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.stage_id == self.stage.stage_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id == self.grant_holder.user_id).delete()
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def _grant(self, tier):
        return AccessRequest(
            user_id=self.grant_holder.user_id, team_id=self.routing_team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=tier, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )

    def test_no_grant_no_membership_is_denied(self):
        with self.assertRaises(PermissionDeniedError):
            _check_upload_access(
                self.db, user_id=self.grant_holder.user_id, team_id=self.routing_team.team_id,
                project_id=self.project.project_id, stage_id=self.stage.stage_id,
            )

    def test_viewer_tier_grant_cannot_upload(self):
        self.db.add(self._grant(GrantTier.viewer))
        self.db.commit()
        with self.assertRaises(PermissionDeniedError):
            _check_upload_access(
                self.db, user_id=self.grant_holder.user_id, team_id=self.routing_team.team_id,
                project_id=self.project.project_id, stage_id=self.stage.stage_id,
            )

    def test_contributor_tier_grant_can_upload_as_its_own_routing_team(self):
        self.db.add(self._grant(GrantTier.contributor))
        self.db.commit()
        stage = _check_upload_access(
            self.db, user_id=self.grant_holder.user_id, team_id=self.routing_team.team_id,
            project_id=self.project.project_id, stage_id=self.stage.stage_id,
        )
        self.assertEqual(stage.stage_id, self.stage.stage_id)

    def test_contributor_tier_grant_cannot_upload_as_a_different_team(self):
        self.db.add(self._grant(GrantTier.contributor))
        self.db.commit()
        with self.assertRaises(PermissionDeniedError):
            _check_upload_access(
                self.db, user_id=self.grant_holder.user_id, team_id=self.other_team.team_id,
                project_id=self.project.project_id, stage_id=self.stage.stage_id,
            )
