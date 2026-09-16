import unittest
import uuid
from starlette.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.api.dependencies import get_current_user
from app.models.graph import (
    AuditFinding,
    AuditRun,
    Claim,
    Edge,
    Node,
    ProjectMetricSnapshot,
    StageMetricSnapshot,
)
from app.models.project import Project
from app.models.stage import Stage, StageReference, TeamStageAccess
from app.models.team import ProjectAdmin, Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.models.document import Document, DocumentStatus, DocumentVersion
from app.models.workflow import WorkflowState
from app.models.required_document import RequiredDocument
from app.services.auth import resolve_identity
from app.services.graph.audit_engine import execute_project_audit
from app.services.graph.sync import sync_project_graph


class TestGraphSecurity(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()

        # Create 2 distinct tenants for cross-tenant isolation testing
        self.tenant_a = Tenant(name=f"Tenant A {uuid.uuid4().hex[:6]}")
        self.tenant_b = Tenant(name=f"Tenant B {uuid.uuid4().hex[:6]}")
        self.db.add_all([self.tenant_a, self.tenant_b])
        self.db.commit()

        # Users in Tenant A
        self.user_a1 = User(
            tenant_id=self.tenant_a.tenant_id,
            email=f"user_a1_{uuid.uuid4().hex[:6]}@test.com",
            full_name="User A1 (Member)",
            password_hash="mock",
            is_org_admin=False,
        )
        self.user_a_outsider = User(
            tenant_id=self.tenant_a.tenant_id,
            email=f"user_outsider_{uuid.uuid4().hex[:6]}@test.com",
            full_name="User Outsider (No Project Access)",
            password_hash="mock",
            is_org_admin=False,
        )
        # User in Tenant B
        self.user_b = User(
            tenant_id=self.tenant_b.tenant_id,
            email=f"user_b_{uuid.uuid4().hex[:6]}@test.com",
            full_name="User B (Foreign Tenant)",
            password_hash="mock",
            is_org_admin=False,
        )
        self.db.add_all([self.user_a1, self.user_a_outsider, self.user_b])
        self.db.commit()

        # Project in Tenant A
        self.project_a = Project(
            tenant_id=self.tenant_a.tenant_id,
            name="Security Hardened Project",
        )
        self.db.add(self.project_a)
        self.db.commit()

        # Team in Project A
        self.team1 = Team(
            project_id=self.project_a.project_id,
            name="Team Stage 1 Only",
        )
        self.db.add(self.team1)
        self.db.commit()

        # User A1 belongs to Team 1
        self.membership = UserTeamMembership(
            user_id=self.user_a1.user_id,
            team_id=self.team1.team_id,
            project_id=self.project_a.project_id,
            role=TeamRole.contributor,
        )
        self.db.add(self.membership)
        self.db.commit()

        # Stages in Project A
        self.stage1 = Stage(
            project_id=self.project_a.project_id,
            name="Stage 1 Public",
            order_index=1,
            requires_approval=False,
        )
        self.stage2 = Stage(
            project_id=self.project_a.project_id,
            name="Stage 2 Restricted",
            order_index=2,
            requires_approval=False,
        )
        self.db.add_all([self.stage1, self.stage2])
        self.db.commit()

        # Grant Team 1 access ONLY to Stage 1
        self.tsa1 = TeamStageAccess(
            team_id=self.team1.team_id,
            stage_id=self.stage1.stage_id,
        )
        self.db.add(self.tsa1)
        self.db.commit()

        # Add mandatory requirements to both stages
        self.req1 = RequiredDocument(
            stage_id=self.stage1.stage_id,
            name="Stage 1 Spec",
            is_mandatory=True,
        )
        self.req2 = RequiredDocument(
            stage_id=self.stage2.stage_id,
            name="Stage 2 Confidential Plan",
            is_mandatory=True,
        )
        self.db.add_all([self.req1, self.req2])
        self.db.commit()

        sync_project_graph(self.db, self.project_a.project_id)
        # Execute audit so metric snapshots and findings exist
        execute_project_audit(self.db, self.project_a.project_id)

        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.rollback()

        self.db.query(Document).filter(Document.project_id == self.project_a.project_id).update({"current_version_id": None})
        self.db.commit()

        self.db.query(Claim).filter(Claim.project_id == self.project_a.project_id).delete()
        self.db.query(StageMetricSnapshot).filter(StageMetricSnapshot.project_id == self.project_a.project_id).delete()
        self.db.query(ProjectMetricSnapshot).filter(ProjectMetricSnapshot.project_id == self.project_a.project_id).delete()
        self.db.query(AuditFinding).filter(AuditFinding.project_id == self.project_a.project_id).delete()
        self.db.query(AuditRun).filter(AuditRun.project_id == self.project_a.project_id).delete()
        self.db.query(Edge).filter(Edge.project_id == self.project_a.project_a if hasattr(self.project_a, "project_a") else Edge.project_id == self.project_a.project_id).delete()
        self.db.query(Node).filter(Node.project_id == self.project_a.project_id).delete()

        stage_ids = [self.stage1.stage_id, self.stage2.stage_id]
        self.db.query(RequiredDocument).filter(RequiredDocument.stage_id.in_(stage_ids)).delete(synchronize_session=False)
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id.in_(stage_ids)).delete(synchronize_session=False)
        self.db.query(Stage).filter(Stage.stage_id.in_(stage_ids)).delete(synchronize_session=False)

        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project_a.project_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project_a.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project_a.project_id).delete()

        self.db.query(User).filter(User.user_id.in_([self.user_a1.user_id, self.user_a_outsider.user_id, self.user_b.user_id])).delete()
        self.db.query(Tenant).filter(Tenant.tenant_id.in_([self.tenant_a.tenant_id, self.tenant_b.tenant_id])).delete()
        self.db.commit()
        self.db.close()

    def test_cross_tenant_isolation(self):
        """
        User B (Tenant B) attempting to access Project A returns 404 Not Found.
        Does not leak project existence.
        """
        identity_b = resolve_identity(self.db, self.user_b.user_id)
        app.dependency_overrides[get_current_user] = lambda: identity_b

        resp = self.client.get(f"/projects/{self.project_a.project_id}/intelligence/metrics")
        self.assertEqual(resp.status_code, 404)

    def test_outsider_access_denial(self):
        """
        User Outsider (same tenant, but 0 project memberships / not project_admin)
        receives 403 Forbidden.
        """
        identity_outsider = resolve_identity(self.db, self.user_a_outsider.user_id)
        app.dependency_overrides[get_current_user] = lambda: identity_outsider

        resp = self.client.get(f"/projects/{self.project_a.project_id}/intelligence/metrics")
        self.assertEqual(resp.status_code, 403)

    def test_stage_abac_redaction(self):
        """
        User A1 has team_stage_access ONLY for Stage 1.
        Metrics and findings for Stage 2 must be strictly redacted.
        """
        identity_a1 = resolve_identity(self.db, self.user_a1.user_id)
        app.dependency_overrides[get_current_user] = lambda: identity_a1

        resp = self.client.get(f"/projects/{self.project_a.project_id}/intelligence/metrics")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        # Verify Stage 2 is redacted from the stages array
        stage_ids_returned = [s["stage_id"] for s in data["stages"]]
        self.assertIn(str(self.stage1.stage_id), stage_ids_returned)
        self.assertNotIn(str(self.stage2.stage_id), stage_ids_returned)

        # Verify findings for Stage 2 are redacted
        resp_findings = self.client.get(f"/projects/{self.project_a.project_id}/intelligence/findings")
        self.assertEqual(resp_findings.status_code, 200)
        findings_data = resp_findings.json()
        findings_target_stages = [f["target_stage_id"] for f in findings_data["findings"]]
        self.assertNotIn(str(self.stage2.stage_id), findings_target_stages)

    def test_historical_audit_finding_interpretability(self):
        """
        Historical findings must remain fully interpretable even if the referenced
        live entity no longer exists in the database.
        """
        # Read the latest finding for req1
        finding = (
            self.db.query(AuditFinding)
            .filter(
                AuditFinding.project_id == self.project_a.project_id,
                AuditFinding.affected_entity_id == self.req1.requirement_id,
            )
            .first()
        )
        self.assertIsNotNone(finding)
        self.assertIn("stage_name", finding.details)
        self.assertEqual(finding.details["stage_name"], "Stage 1 Public")
        self.assertEqual(finding.details["entity_label"], "Stage 1 Spec")


if __name__ == "__main__":
    unittest.main()
