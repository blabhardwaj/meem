import unittest
import uuid
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.project import Project
from app.models.stage import Stage
from app.models.team import (
    AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier,
    Team, TeamRole, UserTeamMembership,
)
from app.models.tenant import Tenant
from app.models.user import User
from app.services.access_control import get_active_stage_grants_for_user, resolve_stage_grant


class TestStageGrantResolver(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"StageGrant Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(project_id=self.project.project_id, name="Grant Team")
        self.db.add(self.team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Grant Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()

        self.viewer = User(email=f"v-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.viewer)
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.team_id == self.team.team_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id == self.viewer.user_id).delete()
        self.db.query(Stage).filter(Stage.project_id == self.project.project_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def _grant(self, *, tier, status=AccessRequestStatus.approved, decided_at=None, revoked_at=None, expires_at=None, request_id=None):
        return AccessRequest(
            request_id=request_id or uuid.uuid4(),
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=tier, status=status, decided_at=decided_at, revoked_at=revoked_at,
            expires_at=expires_at,
        )

    def test_no_grant_returns_none(self):
        self.assertEqual(get_active_stage_grants_for_user(self.db, self.viewer.user_id), {})
        self.assertIsNone(resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id))

    def test_approved_unexpired_grant_is_returned(self):
        now = datetime.now(timezone.utc)
        self.db.add(self._grant(tier=GrantTier.viewer, decided_at=now, expires_at=now + timedelta(days=7)))
        self.db.commit()
        grant = resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id)
        self.assertIsNotNone(grant)
        self.assertEqual(grant.tier, GrantTier.viewer)
        self.assertEqual(grant.team_id, self.team.team_id)

    def test_approved_expired_grant_is_not_returned(self):
        now = datetime.now(timezone.utc)
        self.db.add(self._grant(tier=GrantTier.viewer, decided_at=now - timedelta(days=10), expires_at=now - timedelta(days=1)))
        self.db.commit()
        self.assertIsNone(resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id))

    def test_latest_decided_at_wins_not_latest_requested_at(self):
        now = datetime.now(timezone.utc)
        older_decision_newer_request = self._grant(
            tier=GrantTier.viewer, decided_at=now - timedelta(days=5), expires_at=now + timedelta(days=90),
        )
        newer_decision_older_request = self._grant(
            tier=GrantTier.contributor, decided_at=now - timedelta(days=1), expires_at=now + timedelta(days=90),
        )
        self.db.add(older_decision_newer_request)
        self.db.commit()
        self.db.add(newer_decision_older_request)
        self.db.commit()
        grant = resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id)
        self.assertEqual(grant.tier, GrantTier.contributor)

    def test_revoke_does_not_reactivate_an_older_approved_grant(self):
        now = datetime.now(timezone.utc)
        january_viewer = self._grant(tier=GrantTier.viewer, decided_at=now - timedelta(days=60), expires_at=None)
        february_contributor = self._grant(
            tier=GrantTier.contributor, status=AccessRequestStatus.revoked,
            decided_at=now - timedelta(days=30), revoked_at=now - timedelta(days=1), expires_at=None,
        )
        self.db.add_all([january_viewer, february_contributor])
        self.db.commit()
        self.assertIsNone(resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id))

    def test_tie_breaker_uses_request_id_when_timestamps_are_identical(self):
        same_ts = datetime.now(timezone.utc)
        lower_id = uuid.UUID(int=1)
        higher_id = uuid.UUID(int=2)
        self.db.add(self._grant(tier=GrantTier.viewer, decided_at=same_ts, expires_at=same_ts + timedelta(days=90), request_id=lower_id))
        self.db.add(self._grant(tier=GrantTier.contributor, decided_at=same_ts, expires_at=same_ts + timedelta(days=90), request_id=higher_id))
        self.db.commit()
        grant = resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id)
        self.assertEqual(grant.request_id, higher_id)
        self.assertEqual(grant.tier, GrantTier.contributor)

    def test_grant_on_soft_deleted_stage_is_not_returned(self):
        now = datetime.now(timezone.utc)
        self.db.add(self._grant(tier=GrantTier.viewer, decided_at=now, expires_at=None))
        self.db.commit()
        self.stage.deleted_at = now
        self.db.commit()
        self.assertIsNone(resolve_stage_grant(self.db, self.viewer.user_id, self.stage.stage_id))

    def test_stage_with_no_native_team_access_becomes_visible_via_grant(self):
        from app.services.access_control import get_accessible_stages_for_user

        outsider = User(email=f"o-{uuid.uuid4().hex[:8]}@test.com", tenant_id=self.tenant.tenant_id, password_hash="x")
        self.db.add(outsider)
        self.db.commit()
        other_team = Team(project_id=self.project.project_id, name="Outsider's Team")
        self.db.add(other_team)
        self.db.commit()
        self.db.add(UserTeamMembership(
            user_id=outsider.user_id, team_id=other_team.team_id,
            project_id=self.project.project_id, role=TeamRole.viewer,
        ))
        self.db.commit()
        # outsider's own team has NO TeamStageAccess to self.stage at all.
        self.assertEqual(get_accessible_stages_for_user(self.db, outsider.user_id, self.project.project_id), [])

        grant = AccessRequest(
            user_id=outsider.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.viewer, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        try:
            self.assertEqual(
                get_accessible_stages_for_user(self.db, outsider.user_id, self.project.project_id),
                [self.stage.stage_id],
            )
        finally:
            self.db.query(AccessRequest).filter(AccessRequest.request_id == grant.request_id).delete()
            self.db.query(UserTeamMembership).filter(UserTeamMembership.user_id == outsider.user_id).delete()
            self.db.query(Team).filter(Team.team_id == other_team.team_id).delete()
            self.db.query(User).filter(User.user_id == outsider.user_id).delete()
            self.db.commit()

    def test_grant_for_a_stage_in_a_different_project_is_not_leaked_in(self):
        from app.services.access_control import get_accessible_stages_for_user

        other_project = Project(tenant_id=self.tenant.tenant_id, name=f"Other Project {uuid.uuid4().hex[:8]}")
        self.db.add(other_project)
        self.db.commit()
        other_stage = Stage(project_id=other_project.project_id, name="Other Stage", order_index=1)
        self.db.add(other_stage)
        self.db.commit()

        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=other_stage.stage_id,
            tier=GrantTier.viewer, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        try:
            self.assertEqual(
                get_accessible_stages_for_user(self.db, self.viewer.user_id, self.project.project_id),
                [],
            )
        finally:
            self.db.query(AccessRequest).filter(AccessRequest.request_id == grant.request_id).delete()
            self.db.query(Stage).filter(Stage.stage_id == other_stage.stage_id).delete()
            self.db.query(Project).filter(Project.project_id == other_project.project_id).delete()
            self.db.commit()
