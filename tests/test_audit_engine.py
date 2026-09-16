import unittest
import uuid
from app.database import SessionLocal
from app.models.document import Document, DocumentStatus, DocumentVersion
from app.models.graph import (
    AuditFinding,
    AuditRun,
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
from app.services.graph.audit_engine import execute_project_audit
from app.services.graph.relationship_extractor import extract_document_relationships
from app.services.graph.sync import sync_project_graph


class TestAuditEngine(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.tenant = self.db.query(Tenant).first()
        self.assertIsNotNone(self.tenant, "Tenant required")
        self.user = self.db.query(User).filter(User.tenant_id == self.tenant.tenant_id).first()
        self.assertIsNotNone(self.user, "User required")

        self.project = Project(
            tenant_id=self.tenant.tenant_id,
            name=f"Audit Engine Project {uuid.uuid4().hex[:8]}",
        )
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(
            project_id=self.project.project_id,
            name="Core Team",
        )
        self.db.add(self.team)
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        # Break circular references and cascade clean up
        self.db.query(Document).filter(Document.project_id == self.project.project_id).update({"current_version_id": None})
        self.db.commit()

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

    def test_future_stage_non_contamination(self):
        """
        Verify that a downstream requirement in stage C does NOT alter or contaminate
        upstream stage A or B's completeness score or readiness status.
        """
        # Create stages A, B, C
        stage_a = Stage(project_id=self.project.project_id, name="Stage A", order_index=1, requires_approval=False)
        stage_b = Stage(project_id=self.project.project_id, name="Stage B", order_index=2, requires_approval=False)
        stage_c = Stage(project_id=self.project.project_id, name="Stage C", order_index=3, requires_approval=False)
        self.db.add_all([stage_a, stage_b, stage_c])
        self.db.commit()

        # Add satisfied requirement to Stage A
        req_a = RequiredDocument(stage_id=stage_a.stage_id, name="Spec A", is_mandatory=True)
        self.db.add(req_a)
        self.db.commit()

        doc_a = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=stage_a.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="Spec A Document.md",
            mime_type="text/markdown",
        )
        self.db.add(doc_a)
        self.db.commit()

        ver_a = DocumentVersion(
            document_id=doc_a.document_id,
            version_number=1,
            file_data=b"Spec A content",
            file_size_bytes=14,
            uploaded_by=self.user.user_id,
            status=DocumentStatus.indexed,
        )
        self.db.add(ver_a)
        self.db.commit()
        doc_a.current_version_id = ver_a.version_id
        self.db.commit()

        # Sync and extract
        sync_project_graph(self.db, self.project.project_id)
        extract_document_relationships(self.db, doc_a.document_id, ver_a.version_id, "This establishes Spec A.")

        # Baseline audit of Stage B
        audit_b_before = execute_project_audit(self.db, self.project.project_id, target_stage_id=stage_b.stage_id)
        comp_before = audit_b_before.completeness_score
        ready_before = audit_b_before.readiness_status
        findings_count_before = audit_b_before.findings_count

        # Introduce an unfulfilled mandatory requirement in downstream Stage C
        req_c = RequiredDocument(stage_id=stage_c.stage_id, name="Downstream Release Checklist", is_mandatory=True)
        self.db.add(req_c)
        self.db.commit()

        sync_project_graph(self.db, self.project.project_id)

        # Re-audit Stage B
        audit_b_after = execute_project_audit(self.db, self.project.project_id, target_stage_id=stage_b.stage_id)

        # Completeness, readiness, and findings count for Stage B MUST BE IDENTICAL
        self.assertEqual(audit_b_after.completeness_score, comp_before)
        self.assertEqual(audit_b_after.readiness_status, ready_before)
        self.assertEqual(audit_b_after.findings_count, findings_count_before)

        # Now audit Stage C: Stage C SHOULD have the missing requirement finding
        audit_c = execute_project_audit(self.db, self.project.project_id, target_stage_id=stage_c.stage_id)
        r001_findings = [f for f in audit_c.findings if f.rule_code == "R001" and f.affected_entity_id == req_c.requirement_id]
        self.assertEqual(len(r001_findings), 1)
        self.assertEqual(audit_c.readiness_status, "NOT_READY")

    def test_r002_unapproved_document_in_gate_stage(self):
        """
        Verify that in a stage with requires_approval=True, unapproved documents trigger R002 blocker.
        """
        stage_gate = Stage(project_id=self.project.project_id, name="Review Gate", order_index=1, requires_approval=True)
        self.db.add(stage_gate)
        self.db.commit()

        doc = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=stage_gate.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="Draft Design.md",
            mime_type="text/markdown",
        )
        self.db.add(doc)
        self.db.commit()

        # Document workflow state is 'pending_review', not 'approved'
        wf = WorkflowState(document_id=doc.document_id, state="pending_review")
        self.db.add(wf)
        self.db.commit()

        sync_project_graph(self.db, self.project.project_id)
        audit = execute_project_audit(self.db, self.project.project_id, target_stage_id=stage_gate.stage_id)

        r002 = [f for f in audit.findings if f.rule_code == "R002"]
        self.assertEqual(len(r002), 1)
        self.assertTrue(r002[0].is_blocker)
        self.assertEqual(audit.readiness_status, "NOT_READY")
        self.assertEqual(r002[0].details["stage_name"], "Review Gate")


if __name__ == "__main__":
    unittest.main()
