import unittest
import uuid
from datetime import datetime, timezone
from starlette.testclient import TestClient

from sqlalchemy import text
from app.database import SessionLocal
from app.main import app
from app.api.dependencies import get_current_user
from app.models.advisory_dismissal import AdvisoryDismissal
from app.models.audit import AuditLog
from app.models.graph import AuditFinding, AuditRun
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import ProjectAdmin, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.models.document import Document, DocumentVersion, SensitivityLevel
from app.services.auth import resolve_identity
from app.services.graph.advisory_dismissal_service import (
    compute_finding_fingerprint,
    dismiss_advisory_finding,
    restore_advisory_finding,
)


class TestAdvisoryDismissal(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.tenant = self.db.query(Tenant).first()
        self.assertIsNotNone(self.tenant, "Tenant required")
        self.db.info["tenant_id"] = self.tenant.tenant_id
        self.db.execute(
            text("SET LOCAL app.current_tenant_id = :tid"),
            {"tid": str(self.tenant.tenant_id)},
        )

        # Create test users
        self.admin_user = self.db.query(User).filter(User.tenant_id == self.tenant.tenant_id).first()
        self.assertIsNotNone(self.admin_user, "Admin user required")

        self.lead_user = User(
            tenant_id=self.tenant.tenant_id,
            email=f"lead_{uuid.uuid4().hex[:6]}@example.com",
            full_name="Test Team Lead",
            password_hash="hash",
        )
        self.contrib_user = User(
            tenant_id=self.tenant.tenant_id,
            email=f"contrib_{uuid.uuid4().hex[:6]}@example.com",
            full_name="Test Contributor",
            password_hash="hash",
        )
        self.db.add_all([self.lead_user, self.contrib_user])
        self.db.commit()

        # Create project and stage
        self.project = Project(
            tenant_id=self.tenant.tenant_id,
            name=f"Advisory Dismissal Project {uuid.uuid4().hex[:6]}",
        )
        self.db.add(self.project)
        self.db.commit()

        self.stage = Stage(
            project_id=self.project.project_id,
            name="Architecture & Design",
            order_index=1,
            requires_approval=False,
        )
        self.db.add(self.stage)
        self.db.commit()

        # Teams and roles:
        # Team lead on project team
        self.team = Team(
            project_id=self.project.project_id,
            name="Platform Architecture Team",
        )
        self.db.add(self.team)
        self.db.commit()

        self.m_lead = UserTeamMembership(
            user_id=self.lead_user.user_id,
            team_id=self.team.team_id,
            project_id=self.project.project_id,
            role=TeamRole.team_lead,
        )
        self.m_contrib = UserTeamMembership(
            user_id=self.contrib_user.user_id,
            team_id=self.team.team_id,
            project_id=self.project.project_id,
            role=TeamRole.contributor,
        )
        self.p_admin = ProjectAdmin(
            user_id=self.admin_user.user_id,
            project_id=self.project.project_id,
        )
        # Grant team access to stage for ABAC clearance
        self.t_access = TeamStageAccess(
            team_id=self.team.team_id,
            stage_id=self.stage.stage_id,
        )
        self.db.add_all([self.m_lead, self.m_contrib, self.p_admin, self.t_access])
        self.db.commit()

        # Documents
        self.doc1 = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=self.stage.stage_id,
            uploaded_by=self.admin_user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="system_architecture.md",
            mime_type="text/markdown",
            sensitivity_level=SensitivityLevel.internal,
        )
        self.doc2 = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=self.stage.stage_id,
            uploaded_by=self.admin_user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="competitor_analysis.md",
            mime_type="text/markdown",
            sensitivity_level=SensitivityLevel.internal,
        )
        self.db.add_all([self.doc1, self.doc2])
        self.db.commit()

        # Audit Run
        self.audit_run = AuditRun(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            lifecycle_snapshot={},
            rules_version="1.0",
            status="COMPLETED",
            started_at=datetime.now(timezone.utc),
            readiness_status="BLOCKED",
            completeness_score=65.0,
            triggered_by=self.admin_user.user_id,
        )
        self.db.add(self.audit_run)
        self.db.commit()

        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.rollback()
        self.db.info["tenant_id"] = self.tenant.tenant_id
        self.db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(self.tenant.tenant_id)})

        # Cleanup in strict dependency order.
        # NOTE: audit_log is append-only by DB trigger, and users referenced by audit_log
        # are left intact following the test suite pattern (e.g. test_access_requests_scoped.py).
        self.db.query(AdvisoryDismissal).filter(AdvisoryDismissal.project_id == self.project.project_id).delete()
        self.db.query(AuditFinding).filter(AuditFinding.project_id == self.project.project_id).delete()
        self.db.query(AuditRun).filter(AuditRun.project_id == self.project.project_id).delete()
        self.db.query(Document).filter(Document.project_id == self.project.project_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(ProjectAdmin).filter(ProjectAdmin.project_id == self.project.project_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Stage).filter(Stage.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_fingerprint_determinism_and_insensitivity_to_volatile_data(self):
        """
        Fingerprints MUST be strictly deterministic based ONLY on immutable identity attributes,
        completely insensitive to volatile run attributes like scores, timestamps, and LLM text.
        """
        doc_id = self.doc1.document_id
        ref_doc_id = self.doc2.document_id

        # Run 1 details with score 0.95 and timestamp A
        details_run1 = {
            "issue_type": "duplicate",
            "score": 0.95,
            "similarity_score": 0.9523,
            "generated_at": "2026-09-20T01:00:00Z",
            "explanation": "Section 3 repeats competitor data.",
            "related_context": "Existing document content (3. Differentiation)",
        }
        ev_sources_run1 = [
            {"document_id": str(doc_id), "role": "affected"},
            {"document_id": str(ref_doc_id), "role": "reference"},
        ]

        fp1 = compute_finding_fingerprint("R009", doc_id, details_run1, ev_sources_run1)

        # Run 2 details with score 0.78, different timestamp, and reworded explanation
        details_run2 = {
            "issue_type": "duplicate",
            "score": 0.78,
            "similarity_score": 0.7812,
            "generated_at": "2026-09-21T05:30:00Z",
            "explanation": "Competitor points repeated.",
            "related_context": "Existing document content (3. Differentiation)",
        }
        ev_sources_run2 = [
            {"document_id": str(doc_id), "role": "affected"},
            {"document_id": str(ref_doc_id), "role": "reference"},
        ]

        fp2 = compute_finding_fingerprint("R009", doc_id, details_run2, ev_sources_run2)

        # Invariant: Must match exactly!
        self.assertEqual(fp1, fp2, "Fingerprint must be insensitive to scores, timestamps, and explanations")

        # Identity change: changing issue_type to 'unmet_requirement' or a different conflicting doc MUST change fingerprint
        details_diff = dict(details_run1, issue_type="contradiction")
        fp_diff = compute_finding_fingerprint("R009", doc_id, details_diff, ev_sources_run1)
        self.assertNotEqual(fp1, fp_diff, "Different issue_type must produce distinct fingerprint")

    def test_blocker_protection_invariant(self):
        """
        Attempting to dismiss any finding where is_blocker=True MUST be rejected with HTTP 400.
        Gating blockers are protected and can never be dismissed.
        """
        # 1. Blocker finding
        blocker = AuditFinding(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            run_id=self.audit_run.run_id,
            rule_code="R001",
            severity="HIGH",
            is_blocker=True,
            title="Missing Mandatory Requirements",
            description="Stage requirement missing",
            affected_entity_type="document",
            affected_entity_id=self.doc1.document_id,
            target_stage_id=self.stage.stage_id,
        )
        self.db.add(blocker)
        self.db.commit()

        # Act as team lead
        identity = resolve_identity(self.db, self.lead_user.user_id)
        app.dependency_overrides[get_current_user] = lambda: identity

        res = self.client.post(
            f"/projects/{self.project.project_id}/intelligence/findings/{blocker.finding_id}/dismiss",
            json={"reason": "Attempting to ignore a gating blocker"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Gating blockers cannot be dismissed", res.json()["detail"])

        # 2. Another blocker (e.g. R007 or R009 contradiction where is_blocker=True)
        blocker_r009 = AuditFinding(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            run_id=self.audit_run.run_id,
            rule_code="R009",
            severity="HIGH",
            is_blocker=True,
            title="Material Contradiction in Claims",
            description="Contradicts SLA guarantee",
            affected_entity_type="document",
            affected_entity_id=self.doc1.document_id,
            details={"issue_type": "contradiction"},
        )
        self.db.add(blocker_r009)
        self.db.commit()

        res2 = self.client.post(
            f"/projects/{self.project.project_id}/intelligence/findings/{blocker_r009.finding_id}/dismiss",
            json={"reason": "False positive contradiction"},
        )
        self.assertEqual(res2.status_code, 400)
        self.assertIn("Gating blockers cannot be dismissed", res2.json()["detail"])

    def test_advisory_eligibility_and_durable_persistence(self):
        """
        Attempting to dismiss any finding where is_blocker=False succeeds.
        The dismissal is durable across re-audits via (project_id, finding_fingerprint),
        while finding_id is provenance only. Readiness score remains unmutated.
        """
        advisory_finding = AuditFinding(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            run_id=self.audit_run.run_id,
            rule_code="R009",
            severity="LOW",
            is_blocker=False,
            title="Possible duplicate section",
            description="Repeats competitive landscape table",
            affected_entity_type="document",
            affected_entity_id=self.doc1.document_id,
            details={
                "issue_type": "duplicate",
                "related_context": "Existing document content (3. Differentiation)",
            },
            evidence_sources=[
                {"document_id": str(self.doc1.document_id), "role": "affected"},
                {"document_id": str(self.doc2.document_id), "role": "reference"},
            ],
        )
        self.db.add(advisory_finding)
        self.db.commit()

        identity = resolve_identity(self.db, self.lead_user.user_id)
        app.dependency_overrides[get_current_user] = lambda: identity

        # Substantive reason validation (min length 5)
        res_short = self.client.post(
            f"/projects/{self.project.project_id}/intelligence/findings/{advisory_finding.finding_id}/dismiss",
            json={"reason": "no"},
        )
        self.assertEqual(res_short.status_code, 422)  # Pydantic min_length validation

        # Valid dismissal
        reason = "Reviewed by team lead: intentional repetition for executive summary."
        res = self.client.post(
            f"/projects/{self.project.project_id}/intelligence/findings/{advisory_finding.finding_id}/dismiss",
            json={"reason": reason},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["is_active"])
        self.assertEqual(data["reason"], reason)
        self.assertEqual(data["initial_finding_id"], str(advisory_finding.finding_id))

        # Check DB AdvisoryDismissal table directly
        dismissal = self.db.query(AdvisoryDismissal).filter(
            AdvisoryDismissal.project_id == self.project.project_id,
            AdvisoryDismissal.is_active.is_(True),
        ).first()
        self.assertIsNotNone(dismissal)
        self.assertEqual(dismissal.initial_finding_id, advisory_finding.finding_id)

        # Audit log append-only verification
        log = self.db.query(AuditLog).filter(
            AuditLog.action == "DISMISS_ADVISORY_FINDING",
            AuditLog.user_id == self.lead_user.user_id,
        ).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.details["reason"], reason)

        # PROVENANCE & PERSISTENCE CHECK:
        # Simulate Run 2 creating a completely NEW finding row with a different finding_id
        run_2 = AuditRun(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            lifecycle_snapshot={},
            rules_version="1.0",
            status="COMPLETED",
            started_at=datetime.now(timezone.utc),
            readiness_status="BLOCKED",
            completeness_score=65.0,
            triggered_by=self.admin_user.user_id,
        )
        self.db.add(run_2)
        self.db.commit()

        new_finding_row = AuditFinding(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            run_id=run_2.run_id,  # fresh run
            rule_code="R009",
            severity="LOW",
            is_blocker=False,
            title="Possible duplicate section",
            description="Repeats competitive landscape table",
            affected_entity_type="document",
            affected_entity_id=self.doc1.document_id,
            details={
                "issue_type": "duplicate",
                "score": 0.81,  # volatile score changed
                "related_context": "Existing document content (3. Differentiation)",
            },
            evidence_sources=[
                {"document_id": str(self.doc1.document_id), "role": "affected"},
                {"document_id": str(self.doc2.document_id), "role": "reference"},
            ],
        )
        self.db.add(new_finding_row)
        self.db.commit()

        # The new finding has a different UUID
        self.assertNotEqual(new_finding_row.finding_id, advisory_finding.finding_id)

        # Query findings via API: new finding MUST be identified as is_dismissed=True
        get_res = self.client.get(f"/projects/{self.project.project_id}/intelligence/findings")
        self.assertEqual(get_res.status_code, 200)
        items = get_res.json()["findings"]
        target_dto = next((f for f in items if f["finding_id"] == str(new_finding_row.finding_id)), None)
        self.assertIsNotNone(target_dto)
        self.assertTrue(target_dto["is_dismissed"], "Durable dismissal must carry over to Run 2 finding via fingerprint")
        self.assertEqual(target_dto["dismissal"]["reason"], reason)

        # Status filter test
        active_res = self.client.get(f"/projects/{self.project.project_id}/intelligence/findings?status=active")
        active_ids = [f["finding_id"] for f in active_res.json()["findings"]]
        self.assertNotIn(str(new_finding_row.finding_id), active_ids)

        dismissed_res = self.client.get(f"/projects/{self.project.project_id}/intelligence/findings?status=dismissed")
        dismissed_ids = [f["finding_id"] for f in dismissed_res.json()["findings"]]
        self.assertIn(str(new_finding_row.finding_id), dismissed_ids)

    def test_restore_advisory_finding(self):
        """
        Restoring a dismissed advisory sets is_active=False and records RESTORE_ADVISORY_FINDING audit log.
        """
        advisory_finding = AuditFinding(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            run_id=self.audit_run.run_id,
            rule_code="R004",
            severity="LOW",
            is_blocker=False,
            title="Stale Document Reference",
            description="Refers to v1 of doc",
            affected_entity_type="document",
            affected_entity_id=self.doc1.document_id,
            details={"target_document_id": str(self.doc2.document_id)},
        )
        self.db.add(advisory_finding)
        self.db.commit()

        identity = resolve_identity(self.db, self.lead_user.user_id)
        app.dependency_overrides[get_current_user] = lambda: identity

        # Dismiss
        dismiss_res = self.client.post(
            f"/projects/{self.project.project_id}/intelligence/findings/{advisory_finding.finding_id}/dismiss",
            json={"reason": "Reference acceptable until next release"},
        )
        self.assertEqual(dismiss_res.status_code, 200)

        # Restore
        restore_res = self.client.post(
            f"/projects/{self.project.project_id}/intelligence/findings/{advisory_finding.finding_id}/restore",
        )
        self.assertEqual(restore_res.status_code, 200)
        self.assertFalse(restore_res.json()["is_active"])

        # Audit log verification
        restore_log = self.db.query(AuditLog).filter(
            AuditLog.action == "RESTORE_ADVISORY_FINDING",
            AuditLog.user_id == self.lead_user.user_id,
        ).first()
        self.assertIsNotNone(restore_log)

    def test_role_authorization_contributor_forbidden(self):
        """
        Contributors and viewers cannot dismiss or restore advisory findings (HTTP 403).
        """
        advisory_finding = AuditFinding(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            run_id=self.audit_run.run_id,
            rule_code="R006",
            severity="LOW",
            is_blocker=False,
            title="Unassigned requirement",
            description="Requirement not assigned to document",
            affected_entity_type="requirement",
            affected_entity_id=uuid.uuid4(),
        )
        self.db.add(advisory_finding)
        self.db.commit()

        # Act as contributor (not team lead, not project admin)
        contrib_identity = resolve_identity(self.db, self.contrib_user.user_id)
        app.dependency_overrides[get_current_user] = lambda: contrib_identity

        res = self.client.post(
            f"/projects/{self.project.project_id}/intelligence/findings/{advisory_finding.finding_id}/dismiss",
            json={"reason": "Trying to dismiss as contributor"},
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("Only team leads, project administrators, and organization administrators", res.json()["detail"])

    def test_dismissal_safety_invariant_blocker_never_overridden(self):
        """
        Dismissal Safety Invariant:
        An AdvisoryDismissal may only affect a finding while the current AuditFinding
        has is_blocker == False. If a later audit classifies the same fingerprint as
        a gate blocker (is_blocker == True), the historical dismissal MUST BE IGNORED
        for that run and the blocker must remain fully active and gating.
        """
        # Run 1: Advisory finding with is_blocker = False
        run_1_finding = AuditFinding(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            run_id=self.audit_run.run_id,
            rule_code="R009",
            severity="LOW",
            is_blocker=False,
            title="Coherence observation",
            description="Repeated competitor notes",
            affected_entity_type="document",
            affected_entity_id=self.doc1.document_id,
            details={"issue_type": "duplicate", "section": "3. Competitors"},
            evidence_sources=[
                {"document_id": str(self.doc1.document_id), "role": "affected"},
                {"document_id": str(self.doc2.document_id), "role": "reference"},
            ],
        )
        self.db.add(run_1_finding)
        self.db.commit()

        # Act as team lead and dismiss the advisory
        identity = resolve_identity(self.db, self.lead_user.user_id)
        app.dependency_overrides[get_current_user] = lambda: identity

        dismiss_res = self.client.post(
            f"/projects/{self.project.project_id}/intelligence/findings/{run_1_finding.finding_id}/dismiss",
            json={"reason": "Acknowledged repetition during draft stage."},
        )
        self.assertEqual(dismiss_res.status_code, 200)

        # Run 2: Rule logic or lifecycle policy changes, and the exact same finding
        # is now classified as a GATING BLOCKER (is_blocker = True)
        run_2 = AuditRun(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            lifecycle_snapshot={},
            rules_version="2.0",
            status="COMPLETED",
            started_at=datetime.now(timezone.utc),
            readiness_status="BLOCKED",
            completeness_score=50.0,
            triggered_by=self.admin_user.user_id,
        )
        self.db.add(run_2)
        self.db.commit()

        run_2_blocker = AuditFinding(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            run_id=run_2.run_id,
            rule_code="R009",
            severity="HIGH",
            is_blocker=True,  # NOW ELEVATED TO GATING BLOCKER
            title="Coherence observation",
            description="Repeated competitor notes",
            affected_entity_type="document",
            affected_entity_id=self.doc1.document_id,
            details={"issue_type": "duplicate", "section": "3. Competitors"},
            evidence_sources=[
                {"document_id": str(self.doc1.document_id), "role": "affected"},
                {"document_id": str(self.doc2.document_id), "role": "reference"},
            ],
        )
        self.db.add(run_2_blocker)
        self.db.commit()

        # Invariant check: Fingerprints must match
        fp1 = compute_finding_fingerprint(
            run_1_finding.rule_code,
            run_1_finding.affected_entity_id,
            run_1_finding.details,
            run_1_finding.evidence_sources,
        )
        fp2 = compute_finding_fingerprint(
            run_2_blocker.rule_code,
            run_2_blocker.affected_entity_id,
            run_2_blocker.details,
            run_2_blocker.evidence_sources,
        )
        self.assertEqual(fp1, fp2, "Fingerprints must be identical across runs")

        # Hydrate via GET /findings: The blocker MUST NOT be considered dismissed!
        res = self.client.get(f"/projects/{self.project.project_id}/intelligence/findings")
        self.assertEqual(res.status_code, 200)
        items = res.json()["findings"]
        target_dto = next((f for f in items if f["finding_id"] == str(run_2_blocker.finding_id)), None)
        self.assertIsNotNone(target_dto)

        # CRITICAL SAFETY INVARIANT:
        self.assertTrue(target_dto["is_blocker"])
        self.assertFalse(target_dto["is_dismissed"], "Blocker must NEVER inherit historical advisory dismissal!")
        self.assertIsNone(target_dto["dismissal"], "Dismissal metadata must be None for active blockers")

        # Status filter checks:
        # status=active must include the blocker
        active_res = self.client.get(f"/projects/{self.project.project_id}/intelligence/findings?status=active")
        active_ids = [f["finding_id"] for f in active_res.json()["findings"]]
        self.assertIn(str(run_2_blocker.finding_id), active_ids)

        # status=dismissed must NOT include the blocker
        dismissed_res = self.client.get(f"/projects/{self.project.project_id}/intelligence/findings?status=dismissed")
        dismissed_ids = [f["finding_id"] for f in dismissed_res.json()["findings"]]
        self.assertNotIn(str(run_2_blocker.finding_id), dismissed_ids)


if __name__ == "__main__":
    unittest.main()
