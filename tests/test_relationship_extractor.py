import unittest
import uuid
from app.database import SessionLocal
from app.models.document import Document, DocumentVersion
from app.models.graph import Edge, ExtractionRun, Node
from app.models.project import Project
from app.models.required_document import RequiredDocument
from app.models.stage import Stage
from app.models.team import Team
from app.models.tenant import Tenant
from app.services.graph.relationship_extractor import (
    compute_content_hash,
    extract_document_relationships,
)
from app.services.graph.sync import sync_project_graph


class TestRelationshipExtractor(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        self.tenant = self.db.query(Tenant).first()
        self.assertIsNotNone(self.tenant, "Tenant required")

        self.project = Project(
            tenant_id=self.tenant.tenant_id,
            name=f"Extractor Test Project {uuid.uuid4().hex[:8]}",
        )
        self.db.add(self.project)
        self.db.commit()

        self.stage = Stage(
            project_id=self.project.project_id,
            name="Architecture & Design",
            order_index=1,
        )
        self.db.add(self.stage)
        self.db.commit()

        self.req = RequiredDocument(
            stage_id=self.stage.stage_id,
            name="System Architecture Document",
            is_mandatory=True,
        )
        self.db.add(self.req)
        self.db.commit()

        # Create a test team and get test user
        self.team = Team(
            project_id=self.project.project_id,
            name="Architecture Team",
        )
        self.db.add(self.team)
        self.db.commit()

        from app.models.user import User
        self.user = self.db.query(User).filter(User.tenant_id == self.tenant.tenant_id).first()
        self.assertIsNotNone(self.user, "User required")

        # Create referenced target document
        self.target_doc = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=self.stage.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="Database Schema Specification.md",
            mime_type="text/markdown",
        )
        self.db.add(self.target_doc)
        self.db.commit()

        # Create source document
        self.source_doc = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=self.stage.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="System Architecture Document.md",
            mime_type="text/markdown",
        )
        self.db.add(self.source_doc)
        self.db.commit()

        from app.models.document import DocumentStatus
        self.version = DocumentVersion(
            document_id=self.source_doc.document_id,
            version_number=1,
            file_data=b"# Mock content",
            file_size_bytes=14,
            uploaded_by=self.user.user_id,
            status=DocumentStatus.indexed,
        )
        self.db.add(self.version)
        self.db.commit()

        self.source_doc.current_version_id = self.version.version_id
        self.db.commit()

        # Sync base relational entities into graph
        sync_project_graph(self.db, self.project.project_id)

    def tearDown(self):
        # Set current_version_id to None first to break circular FK
        self.db.query(Document).filter(Document.project_id == self.project.project_id).update({"current_version_id": None})
        self.db.commit()

        self.db.query(ExtractionRun).filter(ExtractionRun.project_id == self.project.project_id).delete()
        self.db.query(Edge).filter(Edge.project_id == self.project.project_id).delete()
        self.db.query(Node).filter(Node.project_id == self.project.project_id).delete()
        
        self.db.query(DocumentVersion).filter(DocumentVersion.document_id == self.source_doc.document_id).delete()
        self.db.query(Document).filter(Document.project_id == self.project.project_id).delete()
        self.db.query(RequiredDocument).filter(RequiredDocument.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Stage).filter(Stage.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_extract_references_and_evidence(self):
        content = (
            "# System Architecture\n\n"
            "This document establishes the architecture overview. "
            "For storage details, see Database Schema Specification."
        )

        res = extract_document_relationships(
            db=self.db,
            document_id=self.source_doc.document_id,
            version_id=self.version.version_id,
            content=content,
        )
        if res.errors:
            print("Extraction errors:", res.errors)
        self.assertEqual(res.errors, [])
        self.assertFalse(res.is_cached)
        self.assertGreaterEqual(res.edges_created, 2)

        # Verify REFERENCES edge
        source_node = self.db.query(Node).filter(Node.source_id == self.source_doc.document_id).one()
        target_node = self.db.query(Node).filter(Node.source_id == self.target_doc.document_id).one()
        req_node = self.db.query(Node).filter(Node.source_id == self.req.requirement_id).one()

        ref_edge = (
            self.db.query(Edge)
            .filter(
                Edge.source_node_id == source_node.node_id,
                Edge.target_node_id == target_node.node_id,
                Edge.edge_type == "REFERENCES",
            )
            .first()
        )
        self.assertIsNotNone(ref_edge)
        self.assertIn("content_hash", ref_edge.provenance)

        # Verify ESTABLISHES edge to requirement
        est_edge = (
            self.db.query(Edge)
            .filter(
                Edge.source_node_id == source_node.node_id,
                Edge.target_node_id == req_node.node_id,
            )
            .first()
        )
        self.assertIsNotNone(est_edge)
        self.assertEqual(est_edge.edge_type, "ESTABLISHES")

    def test_extraction_idempotency(self):
        """
        Verify that re-running extraction with identical content returns cached result
        and does not duplicate edges or extraction runs.
        """
        content = "Mentions Database Schema Specification."
        res1 = extract_document_relationships(
            db=self.db,
            document_id=self.source_doc.document_id,
            version_id=self.version.version_id,
            content=content,
        )
        self.assertFalse(res1.is_cached)

        res2 = extract_document_relationships(
            db=self.db,
            document_id=self.source_doc.document_id,
            version_id=self.version.version_id,
            content=content,
        )
        self.assertTrue(res2.is_cached)
        self.assertEqual(res1.run_id, res2.run_id)

        # Verify only 1 ExtractionRun exists
        runs = (
            self.db.query(ExtractionRun)
            .filter(
                ExtractionRun.document_id == self.source_doc.document_id,
                ExtractionRun.version_id == self.version.version_id,
            )
            .all()
        )
        self.assertEqual(len(runs), 1)


if __name__ == "__main__":
    unittest.main()
