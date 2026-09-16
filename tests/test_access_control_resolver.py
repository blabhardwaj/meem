import unittest
import uuid
from datetime import datetime, timedelta, timezone

from app.database import SessionLocal
from app.models.document import Document, DocumentTeamVisibility, SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.services.access_control import resolve_effective_access


class TestResolveEffectiveAccess(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            self.assertIsNotNone(self.tenant, "Tenant required")
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)
        self.tenant = self.db.get(Tenant, tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"Resolver Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(project_id=self.project.project_id, name="Resolver Team")
        self.db.add(self.team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Resolver Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()

        self.viewer = User(email=f"viewer-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.team_lead = User(email=f"lead-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add_all([self.viewer, self.team_lead])
        self.db.commit()

        self.db.add(UserTeamMembership(user_id=self.viewer.user_id, team_id=self.team.team_id, project_id=self.project.project_id, role=TeamRole.viewer))
        self.db.add(UserTeamMembership(user_id=self.team_lead.user_id, team_id=self.team.team_id, project_id=self.project.project_id, role=TeamRole.team_lead))
        self.db.commit()

        self.document = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.team_lead.user_id, uploaded_as_team_id=self.team.team_id,
            sensitivity_level=SensitivityLevel.confidential,
            original_filename="secret.md", mime_type="text/markdown",
        )
        self.db.add(self.document)
        self.db.commit()
        self.db.add(DocumentTeamVisibility(document_id=self.document.document_id, team_id=self.team.team_id))
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.team_id == self.team.team_id).delete()
        self.db.query(DocumentTeamVisibility).filter(DocumentTeamVisibility.document_id == self.document.document_id).delete()
        self.db.query(Document).filter(Document.document_id == self.document.document_id).update({"current_version_id": None})
        self.db.query(Document).filter(Document.document_id == self.document.document_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id.in_([self.viewer.user_id, self.team_lead.user_id])).delete(synchronize_session=False)
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.team_id == self.team.team_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_native_team_lead_is_granted_never_via_grant(self):
        result = resolve_effective_access(self.db, self.team_lead.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "granted")
        self.assertFalse(result.via_grant)
        self.assertIsNone(result.expires_at)

    def test_viewer_with_no_grant_or_request_is_none(self):
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "none")

    def test_viewer_with_pending_request_is_pending(self):
        req = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.pending,
        )
        self.db.add(req)
        self.db.commit()
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "pending")
        self.assertIsNone(result.expires_at)

    def test_viewer_with_active_document_grant_is_granted_via_grant(self):
        req = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(req)
        self.db.commit()
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "granted")
        self.assertTrue(result.via_grant)
        self.assertIsNotNone(result.expires_at)

    def test_stale_denied_request_does_not_suppress_active_stage_grant(self):
        denied = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.denied, decided_at=datetime.now(timezone.utc),
        )
        stage_grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        self.db.add_all([denied, stage_grant])
        self.db.commit()
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "granted")
        self.assertTrue(result.via_grant)

    def test_document_grant_takes_precedence_over_overlapping_stage_grant(self):
        stage_grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        doc_grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add_all([stage_grant, doc_grant])
        self.db.commit()
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "granted")
        self.assertEqual(result.request_id, doc_grant.request_id)

    def test_expired_grant_falls_through_to_none(self):
        expired = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        self.db.add(expired)
        self.db.commit()
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "none")


if __name__ == "__main__":
    unittest.main()
