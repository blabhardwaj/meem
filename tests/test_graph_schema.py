import unittest
import uuid
from sqlalchemy import text
from app.database import SessionLocal, engine
from app.models.graph import (
    Node,
    Edge,
    Claim,
    ExtractionRun,
    AuditRun,
    AuditFinding,
    ProjectMetricSnapshot,
    StageMetricSnapshot,
)
from app.models.project import Project
from app.models.tenant import Tenant
from app.models.stage import Stage


class TestKnowledgeGraphSchema(unittest.TestCase):
    def test_knowledge_tables_exist(self):
        """Verify all 8 tables in the 'knowledge' schema exist in PostgreSQL."""
        expected_tables = {
            "nodes",
            "edges",
            "claims",
            "extraction_runs",
            "audit_runs",
            "audit_findings",
            "project_metric_snapshots",
            "stage_metric_snapshots",
        }
        with engine.connect() as conn:
            result = conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'knowledge';"
                )
            ).fetchall()
            actual_tables = {r[0] for r in result}
            missing = expected_tables - actual_tables
            self.assertFalse(missing, f"Missing tables in knowledge schema: {missing}")

    def test_node_and_edge_crud_and_cascade(self):
        """Verify Node and Edge model mapping, constraints, and cascading cleanup."""
        db = SessionLocal()
        try:
            tenant = db.query(Tenant).first()
            project = db.query(Project).filter(Project.tenant_id == tenant.tenant_id).first()
            self.assertIsNotNone(tenant, "Tenant required for test")
            self.assertIsNotNone(project, "Project required for test")

            test_source_id_1 = uuid.uuid4()
            test_source_id_2 = uuid.uuid4()

            # 1. Create Nodes
            node_a = Node(
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                entity_type="stage",
                source_table="stages",
                source_id=test_source_id_1,
                label="Stage A",
                properties={"order_index": 1},
            )
            node_b = Node(
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                entity_type="stage",
                source_table="stages",
                source_id=test_source_id_2,
                label="Stage B",
                properties={"order_index": 2},
            )
            db.add_all([node_a, node_b])
            db.commit()

            # 2. Create Edge
            edge = Edge(
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                source_node_id=node_a.node_id,
                target_node_id=node_b.node_id,
                edge_type="PRECEDES",
                properties={"lifecycle": True},
                confidence=1.0,
            )
            db.add(edge)
            db.commit()

            saved_edge_id = edge.edge_id
            self.assertIsNotNone(saved_edge_id)

            # 3. Verify Unique constraint on nodes
            duplicate_node = Node(
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                entity_type="stage",
                source_table="stages",
                source_id=test_source_id_1,
                label="Duplicate Stage",
            )
            db.add(duplicate_node)
            with self.assertRaises(Exception):
                db.commit()
            db.rollback()

            # 4. Clean up test nodes (cascading deletes edge)
            db.delete(node_a)
            db.delete(node_b)
            db.commit()

            remaining_edge = db.query(Edge).filter(Edge.edge_id == saved_edge_id).first()
            self.assertIsNone(remaining_edge, "Edge should have been cascade deleted with Node")

        finally:
            db.close()

    def test_audit_run_and_findings_historical_retention(self):
        """Verify AuditRun, AuditFinding, and StageMetricSnapshot historical persistence."""
        db = SessionLocal()
        try:
            tenant = db.query(Tenant).first()
            project = db.query(Project).filter(Project.tenant_id == tenant.tenant_id).first()
            fake_stage_id = uuid.uuid4()

            audit_run = AuditRun(
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                target_stage_id=fake_stage_id,
                lifecycle_snapshot={"stages": [{"name": "A", "order_index": 1}]},
                rules_version="1.0.0",
                status="completed",
                rules_evaluated=10,
                findings_count=1,
                readiness_status="NOT_READY",
                completeness_score=85.0,
                summary={"blockers": 1},
            )
            db.add(audit_run)
            db.commit()

            finding = AuditFinding(
                run_id=audit_run.run_id,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                target_stage_id=fake_stage_id,
                rule_code="R001",
                severity="CRITICAL",
                is_blocker=True,
                title="Missing Mandatory Requirement Evidence",
                description="SRS has no approved satisfying document.",
                affected_entity_type="requirement",
                affected_entity_id=uuid.uuid4(),
                evidence_sources=[],
                details={"stage_name": "Historical Stage", "entity_label": "SRS IEEE 830"},
            )
            db.add(finding)

            stage_snapshot = StageMetricSnapshot(
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                stage_id=fake_stage_id,
                stage_name="Historical Stage",
                audit_run_id=audit_run.run_id,
                completeness_score=85.0,
                readiness_status="NOT_READY",
                mandatory_requirements_total=1,
                mandatory_requirements_satisfied=0,
                mandatory_requirements_missing=1,
                mandatory_requirements_partial=0,
                mandatory_requirements_blocked=0,
                upstream_requirements_applicable=0,
                upstream_requirements_satisfied=0,
                evidence_coverage=0.0,
                document_health=1.0,
                approvals_satisfied=False,
                blockers_count=1,
            )
            db.add(stage_snapshot)
            db.commit()

            # Verify query back with unconstrained references
            retrieved_run = db.query(AuditRun).filter(AuditRun.run_id == audit_run.run_id).first()
            self.assertIsNotNone(retrieved_run)
            self.assertEqual(len(retrieved_run.findings), 1)
            self.assertEqual(retrieved_run.findings[0].rule_code, "R001")
            self.assertTrue(retrieved_run.findings[0].is_blocker)
            self.assertEqual(retrieved_run.findings[0].details["stage_name"], "Historical Stage")

            retrieved_snapshot = db.query(StageMetricSnapshot).filter(
                StageMetricSnapshot.snapshot_id == stage_snapshot.snapshot_id
            ).first()
            self.assertIsNotNone(retrieved_snapshot)
            self.assertEqual(retrieved_snapshot.stage_name, "Historical Stage")
            self.assertEqual(retrieved_snapshot.stage_id, fake_stage_id)

            # Clean up
            db.delete(retrieved_run)
            db.delete(retrieved_snapshot)
            db.commit()

        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
