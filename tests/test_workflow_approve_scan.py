import unittest
import uuid

from app.database import SessionLocal
from app.models.document import Document, DocumentScan, DocumentStatus, DocumentVersion, SensitivityLevel
from app.models.notification import Notification
from app.models.project import Project
from app.models.stage import Stage, TeamStageAccess
from app.models.team import Team, TeamRole, UserTeamMembership
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workflow import WorkflowState, WorkflowStatus
from app.services.workflow import WorkflowScanNotPassedError, approve_document


class TestApproveDocumentScanGate(unittest.TestCase):
    """
    Covers a real bug: a document uploaded by a contributor without
    auto-approve rights, then moved to pending_review via submit_for_review()
    (which never runs the Scanner-driven finalize step), keeps
    DocumentVersion.status == pending_review even when its persisted
    DocumentScan genuinely passed. approve_document() used to trust
    version.status == indexed directly, so a team_lead's ordinary Approve
    was blocked on a scan that actually passed, and there was no way
    around it (override is org_admin/project_admin only). Fixed by having
    _check_scan_passed() recompute pass/fail from the persisted scan and
    promote version.status when it genuinely passes.
    """

    def setUp(self):
        with SessionLocal() as lookup_db:
            self.tenant = lookup_db.query(Tenant).first()
            tenant_id = self.tenant.tenant_id

        self.db = SessionLocal()
        self.db.info["tenant_id"] = str(tenant_id)

        self.project = Project(tenant_id=tenant_id, name=f"Approve Scan Test {uuid.uuid4().hex[:8]}")
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(project_id=self.project.project_id, name="Approve Scan Team")
        self.db.add(self.team)
        self.db.commit()

        self.stage = Stage(project_id=self.project.project_id, name="Approve Scan Stage", order_index=1, requires_approval=True)
        self.db.add(self.stage)
        self.db.commit()
        self.db.add(TeamStageAccess(team_id=self.team.team_id, stage_id=self.stage.stage_id))
        self.db.commit()

        self.lead = User(email=f"asl-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.contributor = User(email=f"asc-{uuid.uuid4().hex[:8]}@test.com", tenant_id=tenant_id, password_hash="x")
        self.db.add_all([self.lead, self.contributor])
        self.db.commit()
        self.db.add(UserTeamMembership(
            user_id=self.lead.user_id, team_id=self.team.team_id,
            project_id=self.project.project_id, role=TeamRole.team_lead,
        ))
        self.db.add(UserTeamMembership(
            user_id=self.contributor.user_id, team_id=self.team.team_id,
            project_id=self.project.project_id, role=TeamRole.contributor,
        ))
        self.db.commit()

        self.document = Document(
            tenant_id=tenant_id, project_id=self.project.project_id, stage_id=self.stage.stage_id,
            uploaded_by=self.contributor.user_id, uploaded_as_team_id=self.team.team_id,
            sensitivity_level=SensitivityLevel.internal,
            original_filename="approve-scan.md", mime_type="text/markdown",
        )
        self.db.add(self.document)
        self.db.commit()

    def _add_version(self, *, status, overall_score, criteria, injection_flagged=False):
        version = DocumentVersion(
            document_id=self.document.document_id,
            file_data=b"content", file_size_bytes=7,
            uploaded_by=self.contributor.user_id,
            status=status,
        )
        self.db.add(version)
        self.db.flush()
        self.document.current_version_id = version.version_id
        self.db.add(DocumentScan(
            version_id=version.version_id,
            overall_score=overall_score,
            criteria=criteria,
            injection_flagged=injection_flagged,
        ))
        self.db.add(WorkflowState(document_id=self.document.document_id, state=WorkflowStatus.pending_review))
        self.db.commit()
        return version

    def tearDown(self):
        self.db.rollback()
        self.db.query(Notification).filter(Notification.project_id == self.project.project_id).delete()
        self.db.query(WorkflowState).filter(WorkflowState.document_id == self.document.document_id).delete()
        self.db.query(DocumentScan).filter(DocumentScan.version_id.in_(
            self.db.query(DocumentVersion.version_id).filter(DocumentVersion.document_id == self.document.document_id)
        )).delete(synchronize_session=False)
        self.db.query(Document).filter(Document.document_id == self.document.document_id).update({"current_version_id": None})
        self.db.query(DocumentVersion).filter(DocumentVersion.document_id == self.document.document_id).delete()
        self.db.query(Document).filter(Document.document_id == self.document.document_id).delete()
        self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id == self.stage.stage_id).delete()
        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id == self.project.project_id).delete()
        self.db.query(Stage).filter(Stage.stage_id == self.stage.stage_id).delete()
        self.db.query(Team).filter(Team.project_id == self.project.project_id).delete()
        self.db.query(Project).filter(Project.project_id == self.project.project_id).delete()
        self.db.commit()
        self.db.close()

    def test_team_lead_can_approve_a_genuinely_passing_scan_stuck_at_pending_review(self):
        self._add_version(
            status=DocumentStatus.pending_review,
            overall_score=60,
            criteria=[
                {"name": "structural_clarity", "score": 20, "note": ""},
                {"name": "completeness", "score": 20, "note": ""},
                {"name": "labeling_accuracy", "score": 20, "note": ""},
            ],
        )
        state = approve_document(
            self.db, self.document.document_id, self.lead.user_id,
            self.team.team_id, self.project.project_id, "team_lead",
        )
        self.assertEqual(state.state, WorkflowStatus.approved)
        version = self.db.get(DocumentVersion, self.document.current_version_id)
        self.assertEqual(version.status, DocumentStatus.indexed)

    def test_team_lead_cannot_approve_a_genuinely_failing_scan(self):
        self._add_version(
            status=DocumentStatus.pending_review,
            overall_score=20,
            criteria=[
                {"name": "structural_clarity", "score": 2, "note": ""},
                {"name": "completeness", "score": 8, "note": ""},
                {"name": "labeling_accuracy", "score": 10, "note": ""},
            ],
        )
        with self.assertRaises(WorkflowScanNotPassedError):
            approve_document(
                self.db, self.document.document_id, self.lead.user_id,
                self.team.team_id, self.project.project_id, "team_lead",
            )

    def test_team_lead_cannot_approve_an_injection_flagged_version_even_with_a_high_score(self):
        self._add_version(
            status=DocumentStatus.needs_attention,
            overall_score=60,
            criteria=[
                {"name": "structural_clarity", "score": 20, "note": ""},
                {"name": "completeness", "score": 20, "note": ""},
                {"name": "labeling_accuracy", "score": 20, "note": ""},
            ],
            injection_flagged=True,
        )
        with self.assertRaises(WorkflowScanNotPassedError):
            approve_document(
                self.db, self.document.document_id, self.lead.user_id,
                self.team.team_id, self.project.project_id, "team_lead",
            )


if __name__ == "__main__":
    unittest.main()
