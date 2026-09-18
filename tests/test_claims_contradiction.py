import unittest
import uuid
from app.database import SessionLocal
from app.models.document import Document, DocumentStatus, DocumentVersion
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
from app.models.required_document import RequiredDocument
from app.models.stage import Stage, StageReference
from app.models.team import Team
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workflow import WorkflowState
from app.services.graph.claims_analyzer import (
    detect_project_contradictions,
    extract_claims_from_text,
    persist_claims,
)
from app.services.graph.audit_engine import execute_project_audit
from app.services.graph.sync import sync_project_graph


class TestClaimsContradiction(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.tenant = self.db.query(Tenant).first()
        self.assertIsNotNone(self.tenant, "Tenant required")
        self.user = self.db.query(User).filter(User.tenant_id == self.tenant.tenant_id).first()
        self.assertIsNotNone(self.user, "User required")

        self.project = Project(
            tenant_id=self.tenant.tenant_id,
            name=f"Claims Test Project {uuid.uuid4().hex[:8]}",
        )
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(
            project_id=self.project.project_id,
            name="Alpha Team",
        )
        self.db.add(self.team)
        self.db.commit()

        self.stage_a = Stage(
            project_id=self.project.project_id,
            name="Stage 1 Architecture",
            order_index=1,
            requires_approval=False,
        )
        self.stage_b = Stage(
            project_id=self.project.project_id,
            name="Stage 2 Delivery",
            order_index=2,
            requires_approval=False,
        )
        self.db.add_all([self.stage_a, self.stage_b])
        self.db.commit()

    def tearDown(self):
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

        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_claims_extraction_and_contradiction_detection(self):
        """
        Verify that:
        1. Explicit claims are extracted from document versions and persisted in knowledge.claims.
        2. Contradictory claims between documents create CONFLICTS_WITH edges.
        3. R007 audit findings are emitted with blocker=True.
        """
        # Create Doc 1 in Stage A with Launch date 2026-10-01 and DB = PostgreSQL
        doc1 = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=self.stage_a.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="Architecture_Spec.md",
            mime_type="text/markdown",
        )
        self.db.add(doc1)
        self.db.commit()

        v1 = DocumentVersion(
            document_id=doc1.document_id,
            version_number=1,
            file_data=b"spec 1",
            file_size_bytes=6,
            uploaded_by=self.user.user_id,
            status=DocumentStatus.indexed,
        )
        self.db.add(v1)
        self.db.commit()
        doc1.current_version_id = v1.version_id
        self.db.commit()

        text1 = (
            "System Architecture Overview.\n"
            "The launch date is 2026-10-01.\n"
            "Our primary database is PostgreSQL.\n"
            "Encryption at rest is mandatory.\n"
        )
        claims1_data = extract_claims_from_text(
            text=text1,
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            document_id=doc1.document_id,
            version_id=v1.version_id,
        )
        self.assertGreaterEqual(len(claims1_data), 3)
        persisted1 = persist_claims(
            self.db,
            self.tenant.tenant_id,
            self.project.project_id,
            doc1.document_id,
            v1.version_id,
            claims1_data,
        )
        self.assertEqual(len(persisted1), len(claims1_data))

        # Create Doc 2 in Stage B with conflicting Launch date 2026-12-15
        doc2 = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=self.stage_b.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="Delivery_Plan.md",
            mime_type="text/markdown",
        )
        self.db.add(doc2)
        self.db.commit()

        v2 = DocumentVersion(
            document_id=doc2.document_id,
            version_number=1,
            file_data=b"plan 2",
            file_size_bytes=6,
            uploaded_by=self.user.user_id,
            status=DocumentStatus.indexed,
        )
        self.db.add(v2)
        self.db.commit()
        doc2.current_version_id = v2.version_id
        self.db.commit()

        text2 = (
            "Project Delivery Plan.\n"
            "The launch date is 2026-12-15.\n"
            "Encryption at rest is optional.\n"
        )
        claims2_data = extract_claims_from_text(
            text=text2,
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            document_id=doc2.document_id,
            version_id=v2.version_id,
        )
        persist_claims(
            self.db,
            self.tenant.tenant_id,
            self.project.project_id,
            doc2.document_id,
            v2.version_id,
            claims2_data,
        )

        # Sync graph nodes so documents have corresponding knowledge nodes
        sync_res = sync_project_graph(self.db, self.project.project_id)
        self.assertGreater(sync_res.nodes_synced, 0)

        # Run contradiction detection
        findings = detect_project_contradictions(
            self.db, self.tenant.tenant_id, self.project.project_id
        )

        # We expect at least two contradictions: launch date (differing values) and encryption (differing polarities)
        r007_findings = [f for f in findings if f.rule_code == "R007"]
        self.assertGreaterEqual(len(r007_findings), 2)
        self.assertTrue(all(f.is_blocker for f in r007_findings))
        self.assertTrue(all(f.severity == "HIGH" for f in r007_findings))

        # Check that CONFLICTS_WITH edge was written
        conflicts_edges = (
            self.db.query(Edge)
            .filter(Edge.project_id == self.project.project_id, Edge.edge_type == "CONFLICTS_WITH")
            .all()
        )
        self.assertGreaterEqual(len(conflicts_edges), 1)

        # Now run full project audit and verify R007 findings are included in the audit run
        audit_run = execute_project_audit(self.db, self.project.project_id)
        self.assertEqual(audit_run.readiness_status, "NOT_READY")
        self.assertIn("R007", [f.rule_code for f in audit_run.findings])

    def test_generic_performance_sla_claims_and_contradictions(self):
        """
        Verify generic performance/SLA claims extraction and cross-document contradiction
        using arbitrary values (e.g. 450 ms vs 520 ms) independent of any specific seed.
        """
        # Document Alpha: SLA specification with target response time = 450 ms
        doc_alpha = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=self.stage_a.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="Service_SLA_Spec.md",
            mime_type="text/markdown",
        )
        self.db.add(doc_alpha)
        self.db.commit()

        v_alpha = DocumentVersion(
            document_id=doc_alpha.document_id,
            version_number=1,
            file_data=b"sla alpha",
            file_size_bytes=9,
            uploaded_by=self.user.user_id,
            status=DocumentStatus.indexed,
        )
        self.db.add(v_alpha)
        self.db.commit()
        doc_alpha.current_version_id = v_alpha.version_id
        self.db.commit()

        text_alpha = (
            "Service SLA Specifications.\n"
            "Requirement SLA-PERF-01: API P95 latency target must not exceed 450 ms under sustained load.\n"
        )
        claims_alpha = extract_claims_from_text(
            text=text_alpha,
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            document_id=doc_alpha.document_id,
            version_id=v_alpha.version_id,
        )
        self.assertEqual(len(claims_alpha), 1)
        self.assertEqual(claims_alpha[0]["subject"], "p95 latency")
        self.assertEqual(claims_alpha[0]["object"], "450 ms")
        self.assertEqual(claims_alpha[0]["source_locator"].get("requirement_context"), "SLA-PERF-01")

        persist_claims(
            self.db,
            self.tenant.tenant_id,
            self.project.project_id,
            doc_alpha.document_id,
            v_alpha.version_id,
            claims_alpha,
        )

        # Document Beta: Performance benchmark results with measured P95 latency = 520 ms
        doc_beta = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=self.stage_b.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="Benchmark_Results.md",
            mime_type="text/markdown",
        )
        self.db.add(doc_beta)
        self.db.commit()

        v_beta = DocumentVersion(
            document_id=doc_beta.document_id,
            version_number=1,
            file_data=b"benchmark beta",
            file_size_bytes=14,
            uploaded_by=self.user.user_id,
            status=DocumentStatus.indexed,
        )
        self.db.add(v_beta)
        self.db.commit()
        doc_beta.current_version_id = v_beta.version_id
        self.db.commit()

        text_beta = (
            "Load Test Execution Results.\n"
            "Observed metric: Measured P95 Latency under peak load is 520 ms.\n"
        )
        claims_beta = extract_claims_from_text(
            text=text_beta,
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            document_id=doc_beta.document_id,
            version_id=v_beta.version_id,
        )
        self.assertEqual(len(claims_beta), 1)
        self.assertEqual(claims_beta[0]["subject"], "p95 latency")
        self.assertEqual(claims_beta[0]["object"], "520 ms")

        persist_claims(
            self.db,
            self.tenant.tenant_id,
            self.project.project_id,
            doc_beta.document_id,
            v_beta.version_id,
            claims_beta,
        )

        # Sync graph nodes
        sync_project_graph(self.db, self.project.project_id)

        # Detect contradictions between Alpha and Beta
        contradictions = detect_project_contradictions(
            self.db, self.tenant.tenant_id, self.project.project_id
        )
        r007_perf = [f for f in contradictions if f.rule_code == "R007" and "p95 latency" in f.description]
        self.assertEqual(len(r007_perf), 1)
        self.assertTrue(r007_perf[0].is_blocker)
        self.assertIn("450 ms", r007_perf[0].description)
        self.assertIn("520 ms", r007_perf[0].description)

        # Verify CONFLICTS_WITH edge exists in graph
        edge = (
            self.db.query(Edge)
            .filter(Edge.project_id == self.project.project_id, Edge.edge_type == "CONFLICTS_WITH")
            .first()
        )
        self.assertIsNotNone(edge)
        self.assertEqual(edge.properties.get("subject"), "p95 latency")
        self.assertEqual(edge.properties.get("value1"), "450 ms")
        self.assertEqual(edge.properties.get("value2"), "520 ms")


if __name__ == "__main__":
    unittest.main()
