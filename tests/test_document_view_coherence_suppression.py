import unittest
import uuid

from app.database import SessionLocal
from app.models.document import Document, DocumentStatus, DocumentVersion
from app.models.graph import AuditFinding, AuditRun, DocumentCoherenceCheck, Edge, Node, ProjectMetricSnapshot, StageMetricSnapshot
from app.models.project import Project
from app.models.required_document import RequiredDocument
from app.models.requirement_satisfaction import RequirementSatisfaction
from app.models.stage import Stage, StageReference
from app.models.team import Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workflow import WorkflowState
from app.services.document_persistence import get_document_view_data


class TestDocumentViewCoherenceSuppression(unittest.TestCase):
    """
    Regression test for a second surface of the same bug fixed in
    evaluate_r009_document_coherence: a document's own review view
    (GET .../documents/{id}) reads DocumentCoherenceCheck.issues directly,
    completely independent of the audit sweep's evidence-aware suppression.
    Without a matching fix here, a document could keep showing "doesn't meet
    the requirement" in its own view forever after a different, better
    document had already superseded it as that requirement's evidence in
    the audit findings list -- the two views would visibly disagree about
    whether the same stale fact is still a live concern.
    """

    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            self.assertIsNotNone(self.tenant, "Tenant required")
            tenant_id = self.tenant.tenant_id
            user = lookup_db.query(User).filter(User.tenant_id == tenant_id).first()
            self.assertIsNotNone(user, "User required")
            user_id = user.user_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)
        self.tenant = self.db.get(Tenant, tenant_id)
        self.user = self.db.get(User, user_id)

        self.project = Project(
            tenant_id=self.tenant.tenant_id,
            name=f"Doc View Coherence Test {uuid.uuid4().hex[:8]}",
        )
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(project_id=self.project.project_id, name="Alpha Product")
        self.db.add(self.team)
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
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

        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def _make_document(self, stage_id, filename):
        doc = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename=filename,
            mime_type="text/markdown",
        )
        self.db.add(doc)
        self.db.commit()
        version = DocumentVersion(
            document_id=doc.document_id, uploaded_by=self.user.user_id,
            file_data=b"content", file_size_bytes=7, status=DocumentStatus.indexed, version_number=1,
        )
        self.db.add(version)
        self.db.commit()
        doc.current_version_id = version.version_id
        self.db.commit()
        return doc, version

    def test_suppressed_issue_not_shown_in_document_view(self):
        stage = Stage(project_id=self.project.project_id, name="Product Definition", order_index=1, requires_approval=False)
        self.db.add(stage)
        self.db.commit()

        requirement = RequiredDocument(
            stage_id=stage.stage_id, name="Product concept",
            description="High-level credit-line concept and differentiation from competitors.",
            is_mandatory=True,
        )
        self.db.add(requirement)
        self.db.commit()

        old_doc, old_version = self._make_document(stage.stage_id, "02_product_business_case.md")
        new_doc, new_version = self._make_document(stage.stage_id, "03_product_concept_and_differentiation.md")

        # A different document (new_doc) is the requirement's CURRENT
        # evidence -- this is the persisted snapshot get_document_view_data
        # reads, same table the audit engine writes every run.
        fake_run = AuditRun(
            tenant_id=self.tenant.tenant_id, project_id=self.project.project_id,
            lifecycle_snapshot={}, rules_version="test", status="completed", rules_evaluated=0, readiness_status="READY", findings_count=0, completeness_score=100.0,
        )
        self.db.add(fake_run)
        self.db.commit()
        self.db.add(RequirementSatisfaction(
            requirement_id=requirement.requirement_id, document_id=new_doc.document_id,
            matched_via="graph_edge", audit_run_id=fake_run.run_id,
        ))
        self.db.commit()

        # OLD document's cache still has the historical unmet_requirement
        # issue -- this must now be suppressed in ITS OWN view because it's
        # no longer the requirement's current evidence.
        old_check = DocumentCoherenceCheck(
            tenant_id=self.tenant.tenant_id, project_id=self.project.project_id,
            document_id=old_doc.document_id, version_id=old_version.version_id,
            content_hash="old-hash", checker_version="1.0.0", status="completed",
            issues=[{
                "type": "unmet_requirement",
                "description": "Describes the credit line but does not provide differentiation from competitors.",
                "confidence": 0.9,
                "related_context": "Requirement: Product concept",
            }],
        )
        self.db.add(old_check)
        self.db.commit()

        old_view = get_document_view_data(self.db, old_doc.document_id)
        self.assertEqual(
            old_view["coherence_issues"], [],
            "Superseded document's own view must not show the stale unmet_requirement issue",
        )

    def test_current_evidence_document_still_shows_its_own_issue(self):
        stage = Stage(project_id=self.project.project_id, name="Product Definition", order_index=1, requires_approval=False)
        self.db.add(stage)
        self.db.commit()

        requirement = RequiredDocument(
            stage_id=stage.stage_id, name="Product concept",
            description="High-level credit-line concept and differentiation from competitors.",
            is_mandatory=True,
        )
        self.db.add(requirement)
        self.db.commit()

        doc, version = self._make_document(stage.stage_id, "03_product_concept_and_differentiation.md")

        fake_run = AuditRun(
            tenant_id=self.tenant.tenant_id, project_id=self.project.project_id,
            lifecycle_snapshot={}, rules_version="test", status="completed", rules_evaluated=0, readiness_status="READY", findings_count=0, completeness_score=100.0,
        )
        self.db.add(fake_run)
        self.db.commit()
        self.db.add(RequirementSatisfaction(
            requirement_id=requirement.requirement_id, document_id=doc.document_id,
            matched_via="graph_edge", audit_run_id=fake_run.run_id,
        ))
        self.db.commit()

        check = DocumentCoherenceCheck(
            tenant_id=self.tenant.tenant_id, project_id=self.project.project_id,
            document_id=doc.document_id, version_id=version.version_id,
            content_hash="only-hash", checker_version="1.0.0", status="completed",
            issues=[{
                "type": "unmet_requirement",
                "description": "Still missing a real differentiation section.",
                "confidence": 0.9,
                "related_context": "Requirement: Product concept",
            }],
        )
        self.db.add(check)
        self.db.commit()

        view = get_document_view_data(self.db, doc.document_id)
        self.assertEqual(len(view["coherence_issues"]), 1, "A real issue on the currently-selected document must still show")


if __name__ == "__main__":
    unittest.main()
