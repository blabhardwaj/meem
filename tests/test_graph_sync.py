import unittest
import uuid
from sqlalchemy import text
from app.database import SessionLocal
from app.models.graph import Edge, Node
from app.models.project import Project
from app.models.stage import Stage, StageReference
from app.models.team import Team
from app.models.document import Document
from app.models.required_document import RequiredDocument
from app.models.tenant import Tenant
from app.services.graph.sync import sync_project_graph


class TestGraphSync(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.tenant = self.db.query(Tenant).first()
        self.assertIsNotNone(self.tenant, "Tenant required")

        # Create isolated test project
        self.project = Project(
            tenant_id=self.tenant.tenant_id,
            name=f"Dynamic Test Project {uuid.uuid4().hex[:8]}",
        )
        self.db.add(self.project)
        self.db.commit()

    def tearDown(self):
        # Clean up child entities in proper order
        self.db.query(Edge).filter(Edge.project_id == self.project.project_id).delete()
        self.db.query(Node).filter(Node.project_id == self.project.project_id).delete()
        
        # Clean up relational stage_references and stages
        stage_ids = [s.stage_id for s in self.db.query(Stage).filter(Stage.project_id == self.project.project_id).all()]
        if stage_ids:
            self.db.query(StageReference).filter(StageReference.stage_id.in_(stage_ids)).delete(synchronize_session=False)
            self.db.query(StageReference).filter(StageReference.references_stage_id.in_(stage_ids)).delete(synchronize_session=False)
            self.db.query(Stage).filter(Stage.stage_id.in_(stage_ids)).delete(synchronize_session=False)

        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_dynamic_stages_and_precedes_immediate_adjacency(self):
        """
        Verify that stages with arbitrary names are synced and PRECEDES
        edges represent immediate adjacency only (A->B and B->C, NOT A->C).
        """
        # Create arbitrary named stages
        stage_alpha = Stage(
            project_id=self.project.project_id,
            name="Alpha Inception",
            order_index=1,
        )
        stage_beta = Stage(
            project_id=self.project.project_id,
            name="Beta Construction",
            order_index=2,
        )
        stage_gamma = Stage(
            project_id=self.project.project_id,
            name="Gamma Verification",
            order_index=3,
        )
        self.db.add_all([stage_alpha, stage_beta, stage_gamma])
        self.db.commit()

        # Run Sync
        result = sync_project_graph(self.db, self.project.project_id)
        self.assertEqual(len(result.errors), 0)

        # Query PRECEDES edges
        precedes_edges = (
            self.db.query(Edge)
            .filter(Edge.project_id == self.project.project_id, Edge.edge_type == "PRECEDES")
            .all()
        )

        # There must be exactly 2 immediate edges: Alpha->Beta and Beta->Gamma
        self.assertEqual(len(precedes_edges), 2)

        node_alpha = self.db.query(Node).filter(Node.source_id == stage_alpha.stage_id).one()
        node_beta = self.db.query(Node).filter(Node.source_id == stage_beta.stage_id).one()
        node_gamma = self.db.query(Node).filter(Node.source_id == stage_gamma.stage_id).one()

        pairs = {(e.source_node_id, e.target_node_id) for e in precedes_edges}
        self.assertIn((node_alpha.node_id, node_beta.node_id), pairs)
        self.assertIn((node_beta.node_id, node_gamma.node_id), pairs)
        self.assertNotIn((node_alpha.node_id, node_gamma.node_id), pairs)

    def test_stage_reordering_updates_precedes_idempotently(self):
        """
        Verify that reordering stages (A->B->C reordered to A->C->B)
        idempotently updates the active PRECEDES edges.
        """
        stage_a = Stage(project_id=self.project.project_id, name="Stage A", order_index=1)
        stage_b = Stage(project_id=self.project.project_id, name="Stage B", order_index=2)
        stage_c = Stage(project_id=self.project.project_id, name="Stage C", order_index=3)
        self.db.add_all([stage_a, stage_b, stage_c])
        self.db.commit()

        # Initial sync: A -> B -> C
        sync_project_graph(self.db, self.project.project_id)

        node_a = self.db.query(Node).filter(Node.source_id == stage_a.stage_id).one()
        node_b = self.db.query(Node).filter(Node.source_id == stage_b.stage_id).one()
        node_c = self.db.query(Node).filter(Node.source_id == stage_c.stage_id).one()

        # Reorder to A -> C -> B
        stage_c.order_index = 2
        stage_b.order_index = 3
        self.db.commit()

        # Re-sync
        sync_project_graph(self.db, self.project.project_id)

        precedes_edges = (
            self.db.query(Edge)
            .filter(Edge.project_id == self.project.project_id, Edge.edge_type == "PRECEDES")
            .all()
        )
        self.assertEqual(len(precedes_edges), 2)
        pairs = {(e.source_node_id, e.target_node_id) for e in precedes_edges}
        self.assertIn((node_a.node_id, node_c.node_id), pairs)
        self.assertIn((node_c.node_id, node_b.node_id), pairs)
        self.assertNotIn((node_a.node_id, node_b.node_id), pairs)

    def test_allowed_reference_direction(self):
        """
        Verify ALLOWED_REFERENCE direction matches stage_references(stage_id, references_stage_id):
        (Stage: stage_id) -[ALLOWED_REFERENCE]-> (Stage: references_stage_id)
        """
        stage_req = Stage(project_id=self.project.project_id, name="Requirements", order_index=1)
        stage_dev = Stage(project_id=self.project.project_id, name="Development", order_index=2)
        self.db.add_all([stage_req, stage_dev])
        self.db.commit()

        # Development references Requirements
        sref = StageReference(
            stage_id=stage_dev.stage_id,
            references_stage_id=stage_req.stage_id,
        )
        self.db.add(sref)
        self.db.commit()

        sync_project_graph(self.db, self.project.project_id)

        node_dev = self.db.query(Node).filter(Node.source_id == stage_dev.stage_id).one()
        node_req = self.db.query(Node).filter(Node.source_id == stage_req.stage_id).one()

        ref_edge = (
            self.db.query(Edge)
            .filter(
                Edge.project_id == self.project.project_id,
                Edge.edge_type == "ALLOWED_REFERENCE",
                Edge.source_node_id == node_dev.node_id,
                Edge.target_node_id == node_req.node_id,
            )
            .first()
        )
        self.assertIsNotNone(ref_edge)

    def test_soft_deleted_stage_excluded_from_active_topology(self):
        """
        Verify that a soft-deleted stage (deleted_at IS NOT NULL) is omitted from active PRECEDES chain.
        """
        from datetime import datetime, timezone

        stage_1 = Stage(project_id=self.project.project_id, name="Stage 1", order_index=1)
        stage_2 = Stage(
            project_id=self.project.project_id,
            name="Stage 2 Archived",
            order_index=2,
            deleted_at=datetime.now(timezone.utc),
        )
        stage_3 = Stage(project_id=self.project.project_id, name="Stage 3", order_index=3)
        self.db.add_all([stage_1, stage_2, stage_3])
        self.db.commit()

        sync_project_graph(self.db, self.project.project_id)

        node_1 = self.db.query(Node).filter(Node.source_id == stage_1.stage_id).one()
        node_3 = self.db.query(Node).filter(Node.source_id == stage_3.stage_id).one()

        precedes_edges = (
            self.db.query(Edge)
            .filter(Edge.project_id == self.project.project_id, Edge.edge_type == "PRECEDES")
            .all()
        )
        # Only Stage 1 -> Stage 3 should be linked in active PRECEDES chain
        self.assertEqual(len(precedes_edges), 1)
        self.assertEqual(precedes_edges[0].source_node_id, node_1.node_id)
        self.assertEqual(precedes_edges[0].target_node_id, node_3.node_id)


if __name__ == "__main__":
    unittest.main()
