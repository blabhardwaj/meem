import unittest
import uuid
from datetime import datetime, timezone
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
from app.models.stage import Stage, StageReference
from app.models.team import ProjectAdmin, Team, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.models.document import Document, DocumentStatus, DocumentVersion
from app.models.workflow import WorkflowState
from app.models.required_document import RequiredDocument
from app.services.auth import ResolvedIdentity
from app.tools.graph_tools import (
    query_entity_neighborhood,
    query_project_gaps,
    query_project_readiness,
    query_project_timeline,
)
from app.services.graph.sync import sync_project_graph


class TestProjectIntelligenceAPI(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.tenant = self.db.query(Tenant).first()
        self.assertIsNotNone(self.tenant, "Tenant required")
        self.user = self.db.query(User).filter(User.tenant_id == self.tenant.tenant_id).first()
        self.assertIsNotNone(self.user, "User required")

        self.project = Project(
            tenant_id=self.tenant.tenant_id,
            name=f"Intelligence API Project {uuid.uuid4().hex[:8]}",
        )
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(
            project_id=self.project.project_id,
            name="API Engineering Team",
        )
        self.db.add(self.team)
        self.db.commit()

        # Add user as project_admin so they have full access
        self.p_admin = ProjectAdmin(
            user_id=self.user.user_id,
            project_id=self.project.project_id,
        )
        self.db.add(self.p_admin)
        self.db.commit()

        self.stage1 = Stage(
            project_id=self.project.project_id,
            name="Stage 1 Discovery",
            order_index=1,
            requires_approval=False,
        )
        self.stage2 = Stage(
            project_id=self.project.project_id,
            name="Stage 2 Delivery",
            order_index=2,
            requires_approval=False,
        )
        self.db.add_all([self.stage1, self.stage2])
        self.db.commit()

        # Add mandatory requirement to Stage 1
        self.req1 = RequiredDocument(
            stage_id=self.stage1.stage_id,
            name="Product Strategy Doc",
            is_mandatory=True,
        )
        self.db.add(self.req1)
        self.db.commit()

        # Sync graph
        sync_project_graph(self.db, self.project.project_id)

        from app.services.auth import resolve_identity
        self.identity = resolve_identity(self.db, self.user.user_id)
        app.dependency_overrides[get_current_user] = lambda: self.identity
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()
        self.db.rollback()

        self.db.query(Document).filter(Document.project_id == self.project.project_id).update({"current_version_id": None})
        self.db.commit()

        self.db.query(Claim).filter(Claim.project_id == self.project.project_id).delete()
        self.db.query(StageMetricSnapshot).filter(StageMetricSnapshot.project_id == self.project.project_id).delete()
        self.db.query(ProjectMetricSnapshot).filter(ProjectMetricSnapshot.project_id == self.project.project_id).delete()
        self.db.query(AuditFinding).filter(AuditFinding.project_id == self.project.project_id).delete()
        self.db.query(AuditRun).filter(AuditRun.project_id == self.project.project_id).delete()
        self.db.query(Edge).filter(Edge.project_id == self.project.project_id).delete()
        self.db.query(Node).filter(Node.project_id == self.project.project_id).delete()

        doc_ids = [d.document_id for d in self.db.query(Document).filter(Document.project_id == self.project.project_id).all()]
        if doc_ids:
            self.db.query(WorkflowState).filter(WorkflowState.document_id.in_(doc_ids)).delete(synchronize_session=False)
            self.db.query(DocumentVersion).filter(DocumentVersion.document_id.in_(doc_ids)).delete(synchronize_session=False)
            self.db.query(Document).filter(Document.document_id.in_(doc_ids)).delete(synchronize_session=False)

        stage_ids = [s.stage_id for s in self.db.query(Stage).filter(Stage.project_id == self.project.project_id).all()]
        if stage_ids:
            self.db.query(RequiredDocument).filter(RequiredDocument.stage_id.in_(stage_ids)).delete(synchronize_session=False)
            self.db.query(StageReference).filter(StageReference.stage_id.in_(stage_ids)).delete(synchronize_session=False)
            self.db.query(StageReference).filter(StageReference.references_stage_id.in_(stage_ids)).delete(synchronize_session=False)
            self.db.query(Stage).filter(Stage.stage_id.in_(stage_ids)).delete(synchronize_session=False)

        self.db.query(ProjectAdmin).filter(ProjectAdmin.project_id == self.project.project_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_metrics_endpoint(self):
        resp = self.client.get(f"/projects/{self.project.project_id}/intelligence/metrics")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["project_id"], str(self.project.project_id))
        self.assertIsNotNone(data["project_metric"])
        self.assertIn("readiness_status", data["project_metric"])
        self.assertIn("completeness_score", data["project_metric"])
        self.assertGreaterEqual(len(data["stages"]), 2)

    def test_gaps_endpoint(self):
        resp = self.client.get(f"/projects/{self.project.project_id}/intelligence/gaps")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["project_id"], str(self.project.project_id))
        self.assertIn("missing_mandatory_requirements", data)
        self.assertGreaterEqual(len(data["missing_mandatory_requirements"]), 1)
        self.assertEqual(data["readiness_status"], "NOT_READY")

    def test_findings_endpoint(self):
        # Trigger audit first to generate findings
        self.client.post(f"/projects/{self.project.project_id}/intelligence/audit", json={})

        resp = self.client.get(f"/projects/{self.project.project_id}/intelligence/findings?severity=HIGH")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("findings", data)
        self.assertIn("blockers_count", data)

    def test_progress_history_endpoint(self):
        # Trigger audit
        self.client.post(f"/projects/{self.project.project_id}/intelligence/audit", json={})

        resp = self.client.get(f"/projects/{self.project.project_id}/intelligence/progress-history")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["project_id"], str(self.project.project_id))
        self.assertGreaterEqual(len(data["history"]), 1)

    def test_timeline_endpoint(self):
        # Trigger audit
        self.client.post(f"/projects/{self.project.project_id}/intelligence/audit", json={})

        resp = self.client.get(f"/projects/{self.project.project_id}/intelligence/timeline")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["project_id"], str(self.project.project_id))
        self.assertGreaterEqual(len(data["timeline"]), 1)

    def test_audit_post_endpoint(self):
        resp = self.client.post(
            f"/projects/{self.project.project_id}/intelligence/audit",
            json={"target_stage_id": str(self.stage1.stage_id)},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["target_stage_id"], str(self.stage1.stage_id))
        self.assertIn("readiness_status", data)

    def test_graph_agent_tools(self):
        # Test query_project_readiness
        readiness_str = query_project_readiness(str(self.project.project_id))
        self.assertIn("readiness_status", readiness_str)
        self.assertIn("NOT_READY", readiness_str)

        # Test query_project_gaps
        gaps_str = query_project_gaps(str(self.project.project_id))
        self.assertIn("missing_mandatory_requirements", gaps_str)

        # Test query_project_timeline
        timeline_str = query_project_timeline(str(self.project.project_id))
        self.assertIn("timeline", timeline_str)

        # Test query_entity_neighborhood
        stage_node = (
            self.db.query(Node)
            .filter(Node.project_id == self.project.project_id, Node.source_id == self.stage1.stage_id)
            .first()
        )
        self.assertIsNotNone(stage_node)
        neighborhood_str = query_entity_neighborhood(
            project_id=str(self.project.project_id),
            entity_type="stage",
            entity_id=str(self.stage1.stage_id),
        )
        self.assertIn("connections", neighborhood_str)


if __name__ == "__main__":
    unittest.main()
