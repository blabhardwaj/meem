import unittest
import uuid
from datetime import datetime, timezone

from app.database import SessionLocal
from app.models.document import Document, DocumentTeamVisibility, SensitivityLevel
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import AccessRequest, AccessRequestScope, AccessRequestStatus, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.services.access_requests_service import AccessRequestError, request_confidential_access


class TestScopedAccessRequests(unittest.TestCase):
    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"ScopedReq Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.engineering = Team(project_id=self.project.project_id, name="Engineering")
        self.qa = Team(project_id=self.project.project_id, name="QA")
        self.db.add_all([self.engineering, self.qa])
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Testing", order_index=1)
        self.db.add(self.stage)
        self.db.commit()

        self.contributor = User(email=f"contrib-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add(self.contributor)
        self.db.commit()

        self.document = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.contributor.user_id, uploaded_as_team_id=self.engineering.team_id,
            sensitivity_level=SensitivityLevel.confidential,
            original_filename="doc.md", mime_type="text/markdown",
        )
        self.db.add(self.document)
        self.db.commit()

    def tearDown(self):
        # NOTE: request_confidential_access() writes an audit_log row for the
        # contributor, and audit_log is enforced append-only at the DB level
        # (a trigger rejects UPDATE/DELETE outright — see
        # app/models/audit.py). That means self.contributor can never be
        # hard-deleted once a test has called request_confidential_access()
        # for them, so — matching the existing test_metrics_service.py
        # pattern of never deleting a user that might be audit-referenced —
        # this leaves the throwaway contributor row in place rather than
        # deleting it (deleting it would raise ForeignKeyViolation).
        self.db.rollback()
        self.db.query(AccessRequest).filter(AccessRequest.user_id == self.contributor.user_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(Document).filter(Document.document_id == self.document.document_id).update({"current_version_id": None})
        self.db.query(Document).filter(Document.document_id == self.document.document_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_document_scope_derives_team_id_from_document(self):
        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.commit()
        req = request_confidential_access(
            self.db, user_id=self.contributor.user_id, document_id=self.document.document_id,
            expected_tenant_id=self.tenant.tenant_id,
        )
        self.assertEqual(req.scope, AccessRequestScope.document)
        self.assertEqual(req.document_id, self.document.document_id)
        self.assertEqual(req.team_id, self.engineering.team_id)

    def test_document_scope_requires_membership_on_the_derived_team(self):
        with self.assertRaises(AccessRequestError) as ctx:
            request_confidential_access(
                self.db, user_id=self.contributor.user_id, document_id=self.document.document_id,
                expected_tenant_id=self.tenant.tenant_id,
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_stage_scope_picks_earliest_team_stage_access_among_requesters_teams(self):
        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.qa.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.commit()
        # Engineering gets access first, QA second — requester is only on QA.
        self.db.add(TeamStageAccess(team_id=self.engineering.team_id, stage_id=self.stage.stage_id))
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.qa.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        req = request_confidential_access(
            self.db, user_id=self.contributor.user_id, stage_id=self.stage.stage_id,
            expected_tenant_id=self.tenant.tenant_id,
        )
        self.assertEqual(req.scope, AccessRequestScope.stage)
        self.assertEqual(req.stage_id, self.stage.stage_id)
        self.assertEqual(req.team_id, self.qa.team_id)  # the only team the requester belongs to

    def test_stage_scope_with_no_qualifying_team_membership_is_403(self):
        self.db.add(TeamStageAccess(team_id=self.engineering.team_id, stage_id=self.stage.stage_id))
        self.db.commit()
        with self.assertRaises(AccessRequestError) as ctx:
            request_confidential_access(
                self.db, user_id=self.contributor.user_id, stage_id=self.stage.stage_id,
                expected_tenant_id=self.tenant.tenant_id,
            )
        self.assertEqual(ctx.exception.status_code, 403)

    def test_duplicate_document_request_is_rejected(self):
        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.commit()
        request_confidential_access(
            self.db, user_id=self.contributor.user_id, document_id=self.document.document_id,
            expected_tenant_id=self.tenant.tenant_id,
        )
        with self.assertRaises(AccessRequestError) as ctx:
            request_confidential_access(
                self.db, user_id=self.contributor.user_id, document_id=self.document.document_id,
                expected_tenant_id=self.tenant.tenant_id,
            )
        self.assertEqual(ctx.exception.status_code, 409)

    def test_team_scope_call_shape_unchanged_for_existing_caller(self):
        # This is exactly how app/tools/rag_tools.py's request_confidential_access
        # tool calls this function today — must keep working with zero changes.
        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.commit()
        req = request_confidential_access(
            self.db, user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            expected_tenant_id=self.tenant.tenant_id,
        )
        self.assertEqual(req.scope, AccessRequestScope.team)
        self.assertEqual(req.team_id, self.engineering.team_id)

    def test_stage_scope_active_grant_does_not_block_a_new_request(self):
        from datetime import timedelta
        from app.models.team import AccessRequestStatus, GrantTier

        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.add(TeamStageAccess(team_id=self.engineering.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        # contributor_confidential specifically, not viewer: a viewer-tier
        # grant is already excluded from resolve_effective_access()'s
        # "granted" path by Task 4's tier-gating, so it wouldn't exercise
        # this task's fix on its own — contributor_confidential is the tier
        # that still reads as "granted" and needs the stage-scope dedup
        # bypass added here to allow a fresh request past it.
        existing_grant = AccessRequest(
            user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            tier=GrantTier.contributor_confidential, status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc), expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        )
        self.db.add(existing_grant)
        self.db.commit()

        req = request_confidential_access(
            self.db, user_id=self.contributor.user_id, stage_id=self.stage.stage_id,
            expected_tenant_id=self.tenant.tenant_id,
        )
        self.assertEqual(req.scope, AccessRequestScope.stage)
        self.assertEqual(req.status, AccessRequestStatus.pending)

    def test_stage_scope_pending_request_still_blocks_a_duplicate(self):
        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.engineering.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.add(TeamStageAccess(team_id=self.engineering.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        request_confidential_access(
            self.db, user_id=self.contributor.user_id, stage_id=self.stage.stage_id,
            expected_tenant_id=self.tenant.tenant_id,
        )
        with self.assertRaises(AccessRequestError) as ctx:
            request_confidential_access(
                self.db, user_id=self.contributor.user_id, stage_id=self.stage.stage_id,
                expected_tenant_id=self.tenant.tenant_id,
            )
        self.assertEqual(ctx.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()
