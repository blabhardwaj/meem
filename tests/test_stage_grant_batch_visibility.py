import unittest
import uuid
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.document import Document, DocumentTeamVisibility, SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, GrantTier, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.services.access_control import classify_documents_visibility, DocumentVisibility
from app.services.authorization_context import build_authorization_context


class TestStageGrantBatchVisibility(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"BatchVis Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.own_team = Team(project_id=self.project.project_id, name="Own Team")
        self.other_team = Team(project_id=self.project.project_id, name="Other Team")
        self.db.add_all([self.own_team, self.other_team])
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Batch Stage", order_index=1)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.own_team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        self.viewer = User(email=f"bv-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.other_uploader = User(email=f"bo-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add_all([self.viewer, self.other_uploader])
        self.db.commit()
        self.db.add(UserTeamMembership(
            user_id=self.viewer.user_id, team_id=self.own_team.team_id,
            project_id=self.project.project_id, role=TeamRole.viewer,
        ))
        self.db.commit()

        self.public_doc = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.other_uploader.user_id, uploaded_as_team_id=self.other_team.team_id,
            sensitivity_level=SensitivityLevel.public,
            original_filename="batch-public.md", mime_type="text/markdown",
        )
        self.confidential_doc = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.other_uploader.user_id, uploaded_as_team_id=self.other_team.team_id,
            sensitivity_level=SensitivityLevel.confidential,
            original_filename="batch-confidential.md", mime_type="text/markdown",
        )
        self.db.add_all([self.public_doc, self.confidential_doc])
        self.db.commit()
        self.db.add(DocumentTeamVisibility(document_id=self.public_doc.document_id, team_id=self.other_team.team_id))
        self.db.add(DocumentTeamVisibility(document_id=self.confidential_doc.document_id, team_id=self.other_team.team_id))
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.stage_id == self.stage.stage_id).delete()
        for doc in (self.public_doc, self.confidential_doc):
            self.db.query(DocumentTeamVisibility).filter(DocumentTeamVisibility.document_id == doc.document_id).delete()
            self.db.query(Document).filter(Document.document_id == doc.document_id).update({"current_version_id": None})
            self.db.query(Document).filter(Document.document_id == doc.document_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(User).filter(User.user_id.in_([self.viewer.user_id, self.other_uploader.user_id])).delete(synchronize_session=False)
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_authorization_context_carries_stage_grant_tier(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.own_team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.contributor, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        self.assertEqual(ctx.stage_grant_tiers.get(self.stage.stage_id), GrantTier.contributor)
        self.assertIn(self.stage.stage_id, ctx.accessible_stage_ids)

    def test_batch_visibility_without_grant_excludes_other_teams_doc(self):
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        result = classify_documents_visibility(self.db, ctx, [self.public_doc.document_id])
        self.assertEqual(result[self.public_doc.document_id], DocumentVisibility.not_visible)

    def test_batch_visibility_viewer_tier_unlocks_public_not_confidential(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.own_team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.viewer, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        result = classify_documents_visibility(self.db, ctx, [self.public_doc.document_id, self.confidential_doc.document_id])
        self.assertEqual(result[self.public_doc.document_id], DocumentVisibility.fully_allowed)
        self.assertEqual(result[self.confidential_doc.document_id], DocumentVisibility.blocked_by_sensitivity)

    def test_batch_visibility_contributor_confidential_tier_unlocks_both(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.own_team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.contributor_confidential, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=None,
        )
        self.db.add(grant)
        self.db.commit()
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        result = classify_documents_visibility(self.db, ctx, [self.public_doc.document_id, self.confidential_doc.document_id])
        self.assertEqual(result[self.public_doc.document_id], DocumentVisibility.fully_allowed)
        self.assertEqual(result[self.confidential_doc.document_id], DocumentVisibility.fully_allowed)
