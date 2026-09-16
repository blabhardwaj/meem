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
from app.models.team import Team, UserTeamMembership, TeamRole
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workflow import WorkflowState, WorkflowStatus
from app.services.graph.audit_engine import RULE_REGISTRY, execute_project_audit
from app.services.graph.relationship_extractor import extract_document_relationships
from app.services.graph.sync import _upsert_edge, _upsert_node, sync_project_graph


class TestAuditRulesExpanded(unittest.TestCase):
    def setUp(self):
        # after_begin (app/database.py) only re-applies db.info["tenant_id"]
        # when a NEW transaction begins — setting it after this session's
        # first query has no effect on the transaction that query already
        # opened. tenants carries no RLS policy (root of the hierarchy,
        # nothing to scope it by), so look the tenant up on its own
        # short-lived session first, close it (ending that transaction), then
        # open self.db with tenant_id already known so its very first
        # transaction picks it up.
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
            name=f"Expanded Rules Test {uuid.uuid4().hex[:8]}",
        )
        self.db.add(self.project)
        self.db.commit()

        self.team = Team(
            project_id=self.project.project_id,
            name="Alpha Engineering",
        )
        self.db.add(self.team)
        self.db.commit()

    def tearDown(self):
        self.db.rollback()
        # Disconnect foreign key references
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

    def test_r004_stale_document_references(self):
        """
        R004: Stale Document Reference
        1. reference to current version -> no R004
        2. reference to old version while newer finalized version exists -> R004
        3. old version with no newer finalized version -> no R004
        4. version relationship remains deterministic across repeated audits

        NOTE (Master Plan v2, item 13): this test manually calls _upsert_edge
        with properties={"referenced_version_number": 1} — R004's logic
        requires that exact edge property, but neither real extraction path
        (relationship_extractor.py's regex pass, nor llm_extraction.py's LLM
        pass) ever writes it; every real REFERENCES/DEPENDS_ON edge only
        names a target document, never a pinned version. So this test proves
        the rule's LOGIC is correct given that input, not that the rule ever
        fires against real application data — see evaluate_r004_stale_
        document_references's docstring for the full verification. Kept
        (not deleted, unlike the old R006 test) because R004 stays
        registered as a placeholder the extractor could genuinely feed later.
        """
        stage = Stage(project_id=self.project.project_id, name="Design Stage", order_index=1, requires_approval=False)
        self.db.add(stage)
        self.db.commit()

        # Target document with v1 and v2
        target_doc = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=stage.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="ArchitectureSpec.md",
            mime_type="text/markdown",
        )
        self.db.add(target_doc)
        self.db.commit()

        target_v1 = DocumentVersion(
            document_id=target_doc.document_id,
            version_number=1,
            file_data=b"Arch Spec v1",
            file_size_bytes=12,
            uploaded_by=self.user.user_id,
            status=DocumentStatus.indexed,
        )
        self.db.add(target_v1)
        self.db.commit()
        target_doc.current_version_id = target_v1.version_id
        self.db.commit()

        # Consumer document
        consumer_doc = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=stage.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="ImplementationPlan.md",
            mime_type="text/markdown",
        )
        self.db.add(consumer_doc)
        self.db.commit()

        consumer_v1 = DocumentVersion(
            document_id=consumer_doc.document_id,
            version_number=1,
            file_data=b"Implementation Plan",
            file_size_bytes=19,
            uploaded_by=self.user.user_id,
            status=DocumentStatus.indexed,
        )
        self.db.add(consumer_v1)
        self.db.commit()
        consumer_doc.current_version_id = consumer_v1.version_id
        self.db.commit()

        sync_project_graph(self.db, self.project.project_id)

        # Connect consumer -> REFERENCES target v1
        c_node = self.db.query(Node).filter(Node.project_id == self.project.project_id, Node.source_id == consumer_doc.document_id).first()
        t_node = self.db.query(Node).filter(Node.project_id == self.project.project_id, Node.source_id == target_doc.document_id).first()

        edge = _upsert_edge(
            db=self.db,
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            source_node_id=c_node.node_id,
            target_node_id=t_node.node_id,
            edge_type="REFERENCES",
            properties={"referenced_version_number": 1},
        )
        self.db.commit()

        # 1. Target has only v1 (indexed) -> reference is current -> NO R004
        audit1 = execute_project_audit(self.db, self.project.project_id)
        r004_f1 = [f for f in audit1.findings if f.rule_code == "R004"]
        self.assertEqual(len(r004_f1), 0)

        # 2. Add target v2 with status=pending_review (NOT finalized) -> NO R004
        target_v2_draft = DocumentVersion(
            document_id=target_doc.document_id,
            version_number=2,
            file_data=b"Arch Spec v2 draft",
            file_size_bytes=18,
            uploaded_by=self.user.user_id,
            status=DocumentStatus.pending_review,
        )
        self.db.add(target_v2_draft)
        self.db.commit()

        audit2 = execute_project_audit(self.db, self.project.project_id)
        r004_f2 = [f for f in audit2.findings if f.rule_code == "R004"]
        self.assertEqual(len(r004_f2), 0)

        # 3. Finalize target v2 (status=indexed) -> newer finalized version exists -> R004 FIRES!
        target_v2_draft.status = DocumentStatus.indexed
        target_doc.current_version_id = target_v2_draft.version_id
        self.db.commit()

        audit3 = execute_project_audit(self.db, self.project.project_id)
        r004_f3 = [f for f in audit3.findings if f.rule_code == "R004"]
        self.assertEqual(len(r004_f3), 1)

        finding = r004_f3[0]
        self.assertEqual(finding.details["referencing_document"], "ImplementationPlan.md")
        self.assertEqual(finding.details["referenced_document"], "ArchitectureSpec.md")
        self.assertEqual(finding.details["referenced_version"], 1)
        self.assertEqual(finding.details["newer_available_version"], 2)
        self.assertIn("Design Stage", finding.details["stage_name"])
        self.assertIn("v2", finding.description)
        self.assertFalse(finding.is_blocker)
        # Master Plan v2, item 13: downgraded MEDIUM -> LOW (policy call —
        # staleness is informational, not actionable, even when real).
        self.assertEqual(finding.severity, "LOW")

        # 4. Deterministic across repeated audits
        audit4 = execute_project_audit(self.db, self.project.project_id)
        r004_f4 = [f for f in audit4.findings if f.rule_code == "R004"]
        self.assertEqual(len(r004_f4), 1)
        self.assertEqual(r004_f4[0].details["referenced_version"], 1)
        self.assertEqual(r004_f4[0].details["newer_available_version"], 2)

    # test_r006_true_orphan_entity removed — Master Plan v2, item 13. R006
    # was deleted from the rule engine after verifying it could never fire
    # against any state the real application can produce: Document.project_id
    # / stage_id / uploaded_as_team_id are all non-nullable FK columns set
    # together, once, at creation from the same validated team/stage context
    # (app/services/document_persistence.py::_check_upload_access rejects a
    # cross-project stage before any row is written). This test's own
    # "missing owner"/"invalid stage" cases only ever existed by constructing
    # Document rows directly via the ORM with a foreign-project team/stage —
    # a state no upload endpoint, or any other real code path, can create.
    # Confirms the rule was defensible only against a synthetic test fixture,
    # never against real usage.

    def test_r010_pending_workflow_blocker(self):
        """
        R010: Pending Workflow Blocker
        1. pending document in approval-required stage -> R010 blocker
        2. approved document -> no R010
        3. pending document in non-gate stage -> no R010
        4. rejected/draft state follows actual workflow semantics (fires R002, not R010)
        5. R010 is included in audit engine's final findings and blocker calculation
        """
        gate_stage = Stage(project_id=self.project.project_id, name="Security Gate", order_index=1, requires_approval=True)
        open_stage = Stage(project_id=self.project.project_id, name="Ideation", order_index=2, requires_approval=False)
        self.db.add_all([gate_stage, open_stage])
        self.db.commit()

        # Case 1: Pending document in approval-required stage -> R010 BLOCKER
        pending_doc = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=gate_stage.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="SecurityAudit.pdf",
            mime_type="application/pdf",
        )
        self.db.add(pending_doc)
        self.db.commit()

        wf_pending = WorkflowState(document_id=pending_doc.document_id, state=WorkflowStatus.pending_review)
        self.db.add(wf_pending)
        self.db.commit()

        sync_project_graph(self.db, self.project.project_id)
        audit1 = execute_project_audit(self.db, self.project.project_id, target_stage_id=gate_stage.stage_id)

        r010_findings = [f for f in audit1.findings if f.rule_code == "R010" and f.affected_entity_id == pending_doc.document_id]
        self.assertEqual(len(r010_findings), 1)
        self.assertTrue(r010_findings[0].is_blocker)
        self.assertEqual(r010_findings[0].severity, "HIGH")
        self.assertEqual(r010_findings[0].details["workflow_state"], "pending_review")
        self.assertTrue(r010_findings[0].details["blocks_stage_exit"])
        self.assertEqual(audit1.readiness_status, "NOT_READY")

        # Master Plan v2, item 13: R002 previously checked `state != "approved"`,
        # which also matched pending_review — producing a duplicate R002
        # finding for the exact same document/fact R010 above already
        # covers. Confirms the fix: R002 must NOT also fire here.
        r002_on_pending = [f for f in audit1.findings if f.rule_code == "R002" and f.affected_entity_id == pending_doc.document_id]
        self.assertEqual(len(r002_on_pending), 0)

        # Case 2: Approved document in gate stage -> NO R010
        wf_pending.state = WorkflowStatus.approved
        self.db.commit()

        audit2 = execute_project_audit(self.db, self.project.project_id, target_stage_id=gate_stage.stage_id)
        r010_approved = [f for f in audit2.findings if f.rule_code == "R010"]
        self.assertEqual(len(r010_approved), 0)

        # Case 3: Pending document in non-gate stage (requires_approval=False) -> NO R010
        open_doc = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=open_stage.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="Ideas.md",
            mime_type="text/markdown",
        )
        self.db.add(open_doc)
        self.db.commit()
        wf_open = WorkflowState(document_id=open_doc.document_id, state=WorkflowStatus.pending_review)
        self.db.add(wf_open)
        self.db.commit()

        audit3 = execute_project_audit(self.db, self.project.project_id, target_stage_id=open_stage.stage_id)
        r010_open = [f for f in audit3.findings if f.rule_code == "R010" and f.affected_entity_id == open_doc.document_id]
        self.assertEqual(len(r010_open), 0)

        # Case 4: Rejected state in gate stage -> triggers R002 unapproved, NOT R010 pending review
        wf_pending.state = WorkflowStatus.rejected
        self.db.commit()

        audit4 = execute_project_audit(self.db, self.project.project_id, target_stage_id=gate_stage.stage_id)
        r010_rejected = [f for f in audit4.findings if f.rule_code == "R010" and f.affected_entity_id == pending_doc.document_id]
        r002_rejected = [f for f in audit4.findings if f.rule_code == "R002" and f.affected_entity_id == pending_doc.document_id]
        self.assertEqual(len(r010_rejected), 0)
        self.assertEqual(len(r002_rejected), 1)

    def test_r001_cross_stage_applies_to(self):
        """
        R001: APPLIES_TO semantics
        1. requirement originates in current stage -> evaluated
        2. requirement originates upstream and APPLIES_TO current stage -> evaluated
        3. requirement originates upstream but does not apply to current stage -> not evaluated
        4. downstream requirement does not contaminate an upstream audit
        5. multiple APPLIES_TO targets work correctly
        6. evidence matching still works correctly
        """
        discovery = Stage(project_id=self.project.project_id, name="Discovery", order_index=1, requires_approval=False)
        engineering = Stage(project_id=self.project.project_id, name="Engineering", order_index=2, requires_approval=False)
        qa = Stage(project_id=self.project.project_id, name="QA", order_index=3, requires_approval=False)
        release = Stage(project_id=self.project.project_id, name="Release", order_index=4, requires_approval=False)
        self.db.add_all([discovery, engineering, qa, release])
        self.db.commit()

        # Req 1: Originates in Discovery, APPLIES_TO Engineering and QA
        req_arch = RequiredDocument(stage_id=discovery.stage_id, name="Architecture Review", is_mandatory=True)
        # Req 2: Originates in Discovery, only applies to Discovery (default)
        req_disc_only = RequiredDocument(stage_id=discovery.stage_id, name="Market Analysis", is_mandatory=True)
        # Req 3: Originates downstream in Release, APPLIES_TO Discovery (invalid downstream contamination)
        req_downstream = RequiredDocument(stage_id=release.stage_id, name="Post Mortem", is_mandatory=True)
        self.db.add_all([req_arch, req_disc_only, req_downstream])
        self.db.commit()

        sync_project_graph(self.db, self.project.project_id)

        # Explicitly wire APPLIES_TO from req_arch to engineering and qa
        req_arch_node = self.db.query(Node).filter(Node.project_id == self.project.project_id, Node.source_id == req_arch.requirement_id).first()
        eng_node = self.db.query(Node).filter(Node.project_id == self.project.project_id, Node.source_id == engineering.stage_id).first()
        qa_node = self.db.query(Node).filter(Node.project_id == self.project.project_id, Node.source_id == qa.stage_id).first()
        disc_node = self.db.query(Node).filter(Node.project_id == self.project.project_id, Node.source_id == discovery.stage_id).first()
        down_node = self.db.query(Node).filter(Node.project_id == self.project.project_id, Node.source_id == req_downstream.requirement_id).first()

        # Add cross-stage APPLIES_TO edges
        _upsert_edge(
            db=self.db,
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            source_node_id=req_arch_node.node_id,
            target_node_id=eng_node.node_id,
            edge_type="APPLIES_TO",
        )
        _upsert_edge(
            db=self.db,
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            source_node_id=req_arch_node.node_id,
            target_node_id=qa_node.node_id,
            edge_type="APPLIES_TO",
        )
        # Add downstream requirement pointing upstream (must not contaminate Discovery audit)
        _upsert_edge(
            db=self.db,
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            source_node_id=down_node.node_id,
            target_node_id=disc_node.node_id,
            edge_type="APPLIES_TO",
        )
        self.db.commit()

        # Audit Engineering stage (upstream is Discovery, current is Engineering)
        audit_eng = execute_project_audit(self.db, self.project.project_id, target_stage_id=engineering.stage_id)

        # 1. req_arch applies to Engineering -> MUST be evaluated under Engineering
        arch_eng_findings = [
            f for f in audit_eng.findings
            if f.rule_code == "R001" and f.affected_entity_id == req_arch.requirement_id and f.target_stage_id == engineering.stage_id
        ]
        self.assertEqual(len(arch_eng_findings), 1)

        # 2. req_disc_only does NOT apply to Engineering -> must NOT have a finding targeting Engineering
        disc_eng_findings = [
            f for f in audit_eng.findings
            if f.rule_code == "R001" and f.affected_entity_id == req_disc_only.requirement_id and f.target_stage_id == engineering.stage_id
        ]
        self.assertEqual(len(disc_eng_findings), 0)

        # 3. Downstream req_downstream must NOT contaminate Discovery or Engineering audit
        down_findings = [
            f for f in audit_eng.findings
            if f.rule_code == "R001" and f.affected_entity_id == req_downstream.requirement_id
        ]
        self.assertEqual(len(down_findings), 0)

        # 4. Multiple APPLIES_TO targets: Audit QA stage -> req_arch ALSO evaluated for QA
        audit_qa = execute_project_audit(self.db, self.project.project_id, target_stage_id=qa.stage_id)
        arch_qa_findings = [
            f for f in audit_qa.findings
            if f.rule_code == "R001" and f.affected_entity_id == req_arch.requirement_id and f.target_stage_id == qa.stage_id
        ]
        self.assertEqual(len(arch_qa_findings), 1)

        # 5. Evidence matching resolves cross-stage requirement
        evidence_doc = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=engineering.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="ArchEvidence.md",
            mime_type="text/markdown",
        )
        self.db.add(evidence_doc)
        self.db.commit()

        ev_node = _upsert_node(
            db=self.db,
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            entity_type="document",
            source_table="documents",
            source_id=evidence_doc.document_id,
            label="ArchEvidence.md",
        )
        _upsert_edge(
            db=self.db,
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            source_node_id=ev_node.node_id,
            target_node_id=req_arch_node.node_id,
            edge_type="EVIDENCES",
        )
        self.db.commit()

        audit_eng_resolved = execute_project_audit(self.db, self.project.project_id, target_stage_id=engineering.stage_id)
        arch_resolved = [
            f for f in audit_eng_resolved.findings
            if f.rule_code == "R001" and f.affected_entity_id == req_arch.requirement_id and f.target_stage_id == engineering.stage_id
        ]
        self.assertEqual(len(arch_resolved), 0)

    def test_all_rules_wired_into_engine(self):
        """
        Verify every deterministic audit rule is registered and executed.
        R006 (orphan entity) deliberately excluded — removed in Master Plan
        v2, item 13 as structurally unreachable (see the deleted
        test_r006_true_orphan_entity's replacement comment above). R011
        (document coherence, item 10) included.
        """
        stage = Stage(project_id=self.project.project_id, name="Initial Stage", order_index=1, requires_approval=False)
        self.db.add(stage)
        self.db.commit()

        # Check RULE_REGISTRY completeness
        rule_codes = [code for code, fn in RULE_REGISTRY]
        expected_rules = ["R001", "R002", "R003", "R004", "R005", "R007", "R008", "R009", "R010", "R011"]
        self.assertEqual(rule_codes, expected_rules)
        self.assertEqual(len(RULE_REGISTRY), 10)

        # Run audit and verify rules_evaluated == 10
        audit = execute_project_audit(self.db, self.project.project_id)
        self.assertEqual(audit.rules_evaluated, 10)


if __name__ == "__main__":
    unittest.main()
