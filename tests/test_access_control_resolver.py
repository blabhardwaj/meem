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
from app.services.access_control import (
    resolve_effective_access,
    is_grant_only_confidential_access,
    classify_document_visibility,
    classify_documents_visibility,
    DocumentVisibility,
)
from app.services.authorization_context import build_authorization_context


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

    def test_document_scope_grant_does_not_leak_via_shared_team_routing(self):
        """
        Regression: AccessRequest.team_id is mandatory ROUTING metadata (which
        team's team_lead decides the request) — set on EVERY row regardless of
        scope — not "which team this grant's coverage is limited to". The
        candidate-row query for a document target must not treat "team_id is
        one of this document's native teams" as sufficient to pull in a row;
        without an explicit AccessRequestScope.team filter on that OR-clause,
        a document-scope grant for a DIFFERENT document sharing the same
        native team would leak in and be misread as covering this document.
        """
        other_document = Document(
            tenant_id=self.tenant.tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.team_lead.user_id, uploaded_as_team_id=self.team.team_id,
            sensitivity_level=SensitivityLevel.confidential,
            original_filename="other-secret.md", mime_type="text/markdown",
        )
        self.db.add(other_document)
        self.db.commit()
        self.db.add(DocumentTeamVisibility(document_id=other_document.document_id, team_id=self.team.team_id))
        self.db.commit()

        try:
            leak_grant = AccessRequest(
                user_id=self.viewer.user_id, team_id=self.team.team_id,
                scope=AccessRequestScope.document, document_id=other_document.document_id,
                status=AccessRequestStatus.approved,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
            )
            self.db.add(leak_grant)
            self.db.commit()

            result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
            self.assertEqual(result.status, "none")
        finally:
            self.db.query(AccessRequest).filter(AccessRequest.document_id == other_document.document_id).delete()
            self.db.query(DocumentTeamVisibility).filter(DocumentTeamVisibility.document_id == other_document.document_id).delete()
            self.db.query(Document).filter(Document.document_id == other_document.document_id).update({"current_version_id": None})
            self.db.query(Document).filter(Document.document_id == other_document.document_id).delete()
            self.db.commit()

    def test_old_denial_does_not_outrank_a_later_lapsed_grant(self):
        """
        Regression: terminal history must be ranked by (decided_at or
        requested_at) across BOTH denied and expired-approved rows together,
        not "any denial on record always wins". An old denial followed by a
        LATER approval that has since lapsed (no further denial on record)
        must read the same as a lone lapsed grant: "none" — the stale denial
        must not outrank the more recent (but now-expired) grant.
        """
        old_denied = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.denied,
            decided_at=datetime.now(timezone.utc) - timedelta(days=10),
        )
        later_lapsed = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            decided_at=datetime.now(timezone.utc) - timedelta(days=5),
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        self.db.add_all([old_denied, later_lapsed])
        self.db.commit()
        result = resolve_effective_access(self.db, self.viewer.user_id, document_id=self.document.document_id)
        self.assertEqual(result.status, "none")


class TestIsGrantOnlyConfidentialAccess(TestResolveEffectiveAccess):
    def test_internal_document_is_never_grant_only(self):
        self.document.sensitivity_level = SensitivityLevel.internal
        self.db.commit()
        self.assertFalse(is_grant_only_confidential_access(self.db, self.viewer.user_id, self.document))

    def test_team_lead_is_never_grant_only_even_with_a_grant(self):
        grant = AccessRequest(
            user_id=self.team_lead.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()
        self.assertFalse(is_grant_only_confidential_access(self.db, self.team_lead.user_id, self.document))

    def test_viewer_with_active_grant_is_grant_only(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()
        self.assertTrue(is_grant_only_confidential_access(self.db, self.viewer.user_id, self.document))

    def test_viewer_with_no_grant_is_not_grant_only(self):
        self.assertFalse(is_grant_only_confidential_access(self.db, self.viewer.user_id, self.document))

    def test_classify_document_visibility_still_blocks_ungranted_viewer(self):
        self.assertEqual(
            classify_document_visibility(self.db, self.viewer.user_id, self.document),
            DocumentVisibility.blocked_by_sensitivity,
        )

    def test_classify_document_visibility_allows_granted_viewer(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()
        self.assertEqual(
            classify_document_visibility(self.db, self.viewer.user_id, self.document),
            DocumentVisibility.fully_allowed,
        )


class TestBatchVisibilityGrants(TestResolveEffectiveAccess):
    def test_document_scoped_grant_is_honored_in_batch_path(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.document, document_id=self.document.document_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
        )
        self.db.add(grant)
        self.db.commit()
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        result = classify_documents_visibility(self.db, ctx, [self.document.document_id])
        self.assertEqual(result[self.document.document_id], DocumentVisibility.fully_allowed)

    def test_stage_scoped_grant_is_honored_in_batch_path(self):
        grant = AccessRequest(
            user_id=self.viewer.user_id, team_id=self.team.team_id,
            scope=AccessRequestScope.stage, stage_id=self.stage.stage_id,
            status=AccessRequestStatus.approved,
            expires_at=datetime.now(timezone.utc) + timedelta(days=90),
        )
        self.db.add(grant)
        self.db.commit()
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        result = classify_documents_visibility(self.db, ctx, [self.document.document_id])
        self.assertEqual(result[self.document.document_id], DocumentVisibility.fully_allowed)

    def test_no_grant_is_blocked_in_batch_path(self):
        ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
        result = classify_documents_visibility(self.db, ctx, [self.document.document_id])
        self.assertEqual(result[self.document.document_id], DocumentVisibility.blocked_by_sensitivity)

    def test_document_scoped_grant_on_other_document_does_not_leak_in_batch_path(self):
        """
        Regression, Task-3-bug-class check for the BULK precompute: two
        documents share a native team (self.team). A document-scope grant
        exists for `other_document` only. Resolving `self.document`'s batch
        visibility must NOT come back fully_allowed just because the grant's
        mandatory routing team_id happens to equal a team self.document is
        also visible through. AuthorizationContext.active_confidential_grant_
        team_ids must never be populated from a non-team-scoped grant row
        (that was the pre-existing bug this task's Step 3 query closes: the
        prior team-only query added g.team_id for ANY approved grant
        regardless of scope), and active_confidential_grant_document_ids
        must only ever match the exact document_id it was issued for.
        """
        other_document = Document(
            tenant_id=self.tenant.tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.team_lead.user_id, uploaded_as_team_id=self.team.team_id,
            sensitivity_level=SensitivityLevel.confidential,
            original_filename="other-secret-batch.md", mime_type="text/markdown",
        )
        self.db.add(other_document)
        self.db.commit()
        self.db.add(DocumentTeamVisibility(document_id=other_document.document_id, team_id=self.team.team_id))
        self.db.commit()

        try:
            leak_grant = AccessRequest(
                user_id=self.viewer.user_id, team_id=self.team.team_id,
                scope=AccessRequestScope.document, document_id=other_document.document_id,
                status=AccessRequestStatus.approved,
                expires_at=datetime.now(timezone.utc) + timedelta(hours=72),
            )
            self.db.add(leak_grant)
            self.db.commit()

            ctx = build_authorization_context(self.db, self.viewer.user_id, self.project.project_id)
            # The grant must not have leaked into the team-scope set at all.
            self.assertNotIn(self.team.team_id, ctx.active_confidential_grant_team_ids)
            self.assertIn(other_document.document_id, ctx.active_confidential_grant_document_ids)
            self.assertNotIn(self.document.document_id, ctx.active_confidential_grant_document_ids)

            result = classify_documents_visibility(
                self.db, ctx, [self.document.document_id, other_document.document_id]
            )
            self.assertEqual(result[self.document.document_id], DocumentVisibility.blocked_by_sensitivity)
            self.assertEqual(result[other_document.document_id], DocumentVisibility.fully_allowed)
        finally:
            self.db.query(AccessRequest).filter(AccessRequest.document_id == other_document.document_id).delete()
            self.db.query(DocumentTeamVisibility).filter(DocumentTeamVisibility.document_id == other_document.document_id).delete()
            self.db.query(Document).filter(Document.document_id == other_document.document_id).update({"current_version_id": None})
            self.db.query(Document).filter(Document.document_id == other_document.document_id).delete()
            self.db.commit()


if __name__ == "__main__":
    unittest.main()
