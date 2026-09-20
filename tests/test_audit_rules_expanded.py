import unittest
import uuid
from app.database import SessionLocal
from app.models.document import Document, DocumentStatus, DocumentVersion
from app.models.graph import (
    AuditFinding,
    AuditRun,
    DocumentCoherenceCheck,
    Edge,
    Node,
    ProjectMetricSnapshot,
    StageMetricSnapshot,
)
from app.models.project import Project
from app.models.required_document import RequiredDocument
from app.models.requirement_satisfaction import RequirementSatisfaction
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

    def test_r008_pending_workflow_blocker(self):
        """
        R008: Pending Workflow Blocker
        1. pending document in approval-required stage -> R008 blocker
        2. approved document -> no R008
        3. pending document in non-gate stage -> no R008
        4. rejected/draft state follows actual workflow semantics (fires R002, not R008)
        5. R008 is included in audit engine's final findings and blocker calculation
        """
        gate_stage = Stage(project_id=self.project.project_id, name="Security Gate", order_index=1, requires_approval=True)
        open_stage = Stage(project_id=self.project.project_id, name="Ideation", order_index=2, requires_approval=False)
        self.db.add_all([gate_stage, open_stage])
        self.db.commit()

        # Case 1: Pending document in approval-required stage -> R008 BLOCKER
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

        r008_findings = [f for f in audit1.findings if f.rule_code == "R008" and f.affected_entity_id == pending_doc.document_id]
        self.assertEqual(len(r008_findings), 1)
        self.assertTrue(r008_findings[0].is_blocker)
        self.assertEqual(r008_findings[0].severity, "HIGH")
        self.assertEqual(r008_findings[0].details["workflow_state"], "pending_review")
        self.assertTrue(r008_findings[0].details["blocks_stage_exit"])
        self.assertEqual(audit1.readiness_status, "NOT_READY")

        # Master Plan v2, item 13: R002 previously checked `state != "approved"`,
        # which also matched pending_review — producing a duplicate R002
        # finding for the exact same document/fact R008 above already
        # covers. Confirms the fix: R002 must NOT also fire here.
        r002_on_pending = [f for f in audit1.findings if f.rule_code == "R002" and f.affected_entity_id == pending_doc.document_id]
        self.assertEqual(len(r002_on_pending), 0)

        # Case 2: Approved document in gate stage -> NO R008
        wf_pending.state = WorkflowStatus.approved
        self.db.commit()

        audit2 = execute_project_audit(self.db, self.project.project_id, target_stage_id=gate_stage.stage_id)
        r008_approved = [f for f in audit2.findings if f.rule_code == "R008"]
        self.assertEqual(len(r008_approved), 0)

        # Case 3: Pending document in non-gate stage (requires_approval=False) -> NO R008
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
        r008_open = [f for f in audit3.findings if f.rule_code == "R008" and f.affected_entity_id == open_doc.document_id]
        self.assertEqual(len(r008_open), 0)

        # Case 4: Rejected state in gate stage -> triggers R002 unapproved, NOT R008 pending review
        wf_pending.state = WorkflowStatus.rejected
        self.db.commit()

        audit4 = execute_project_audit(self.db, self.project.project_id, target_stage_id=gate_stage.stage_id)
        r008_rejected = [f for f in audit4.findings if f.rule_code == "R008" and f.affected_entity_id == pending_doc.document_id]
        r002_rejected = [f for f in audit4.findings if f.rule_code == "R002" and f.affected_entity_id == pending_doc.document_id]
        self.assertEqual(len(r008_rejected), 0)
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

    def test_r009_suppressed_when_superseded_by_better_evidence(self):
        """
        Regression test for the "R009 looks broken" bug: an old, generic
        document (e.g. a Business Case with an incidental "Product Concept"
        section) gets an EVIDENCES edge to a requirement via LLM inference,
        and a cached R009 unmet_requirement finding says its content doesn't
        actually satisfy that requirement. A NEW, purpose-built document
        (e.g. "03_product_concept_and_differentiation.md") is then uploaded
        with a deterministic (regex, title-matched) EVIDENCES edge to the
        SAME requirement, at the SAME confidence. Two things must now be true:

        1. _resolve_requirement_evidence must prefer the deterministic/
           title-matched edge over the LLM-inferred one, regardless of which
           was created first (the priority-hierarchy fix, not a recency
           tiebreak) -- so RequirementSatisfaction/R001 point at the NEW doc.
        2. R009's cached unmet_requirement finding on the OLD document must
           be suppressed (no longer surfaced as a live, blocking finding),
           because the old document is no longer the requirement's current
           evidence -- while a genuine coherence issue cached against the
           document that IS the current evidence must still fire.
        """
        stage = Stage(project_id=self.project.project_id, name="Product Definition", order_index=1, requires_approval=False)
        self.db.add(stage)
        self.db.commit()

        requirement = RequiredDocument(
            stage_id=stage.stage_id,
            name="Product concept",
            description="High-level credit-line concept and differentiation from competitors.",
            is_mandatory=True,
        )
        self.db.add(requirement)
        self.db.commit()

        # Old, generic document: gets its EVIDENCES edge via LLM inference
        # (properties.source == "llm"), created FIRST.
        old_doc = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=stage.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="02_product_business_case.md",
            mime_type="text/markdown",
        )
        self.db.add(old_doc)
        self.db.commit()
        old_version = DocumentVersion(
            document_id=old_doc.document_id, uploaded_by=self.user.user_id,
            file_data=b"...", file_size_bytes=3, status=DocumentStatus.indexed, version_number=1,
        )
        self.db.add(old_version)
        self.db.commit()
        old_doc.current_version_id = old_version.version_id
        self.db.commit()

        # New, purpose-built document: gets its EVIDENCES edge via the
        # deterministic regex/title-match rule (properties.source ==
        # "regex"), created SECOND -- i.e. the pre-fix tiebreak (created_at
        # ASC) would have picked the OLD document here, which is exactly
        # the bug being regression-tested.
        new_doc = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=stage.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="03_product_concept_and_differentiation.md",
            mime_type="text/markdown",
        )
        self.db.add(new_doc)
        self.db.commit()
        new_version = DocumentVersion(
            document_id=new_doc.document_id, uploaded_by=self.user.user_id,
            file_data=b"...", file_size_bytes=3, status=DocumentStatus.indexed, version_number=1,
        )
        self.db.add(new_version)
        self.db.commit()
        new_doc.current_version_id = new_version.version_id
        self.db.commit()

        sync_project_graph(self.db, self.project.project_id)

        req_node = self.db.query(Node).filter(
            Node.project_id == self.project.project_id,
            Node.source_table == "required_documents",
            Node.source_id == requirement.requirement_id,
        ).first()
        old_doc_node = self.db.query(Node).filter(
            Node.project_id == self.project.project_id,
            Node.source_table == "documents",
            Node.source_id == old_doc.document_id,
        ).first()
        new_doc_node = self.db.query(Node).filter(
            Node.project_id == self.project.project_id,
            Node.source_table == "documents",
            Node.source_id == new_doc.document_id,
        ).first()

        _upsert_edge(
            db=self.db, tenant_id=self.tenant.tenant_id, project_id=self.project.project_id,
            source_node_id=old_doc_node.node_id, target_node_id=req_node.node_id,
            edge_type="EVIDENCES", properties={"source": "llm", "reason": "mentions product concept"},
            confidence=0.95,
        )
        self.db.commit()
        _upsert_edge(
            db=self.db, tenant_id=self.tenant.tenant_id, project_id=self.project.project_id,
            source_node_id=new_doc_node.node_id, target_node_id=req_node.node_id,
            edge_type="EVIDENCES",
            properties={"source": "regex", "rule": "title_or_content_evidence", "matched_requirement": "Product concept"},
            confidence=0.95,
        )
        self.db.commit()

        # Cached coherence checks: OLD doc has an unmet_requirement issue
        # (its content doesn't actually deliver differentiation); NEW doc
        # has no issues (it does).
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
        new_check = DocumentCoherenceCheck(
            tenant_id=self.tenant.tenant_id, project_id=self.project.project_id,
            document_id=new_doc.document_id, version_id=new_version.version_id,
            content_hash="new-hash", checker_version="1.0.0", status="completed",
            issues=[],
        )
        self.db.add_all([old_check, new_check])
        self.db.commit()

        audit = execute_project_audit(self.db, self.project.project_id, target_stage_id=stage.stage_id)

        # 1. Evidence resolution must prefer the deterministic/title-matched
        # edge (new_doc) over the LLM-inferred one (old_doc), despite old_doc's
        # edge being created first.
        satisfaction = (
            self.db.query(RequirementSatisfaction)
            .filter(RequirementSatisfaction.requirement_id == requirement.requirement_id)
            .first()
        )
        self.assertIsNotNone(satisfaction, "Requirement should have resolved evidence")
        self.assertEqual(satisfaction.document_id, new_doc.document_id)

        # 2a. The OLD document's cached unmet_requirement finding must be
        # suppressed -- it is no longer the requirement's current evidence.
        old_r009 = [
            f for f in audit.findings
            if f.rule_code == "R009" and f.affected_entity_id == old_doc.document_id
        ]
        self.assertEqual(len(old_r009), 0, "Superseded document's stale unmet_requirement finding must be suppressed")

        # 2b. R001 must NOT report this requirement missing -- it has
        # (better) evidence now.
        r001_missing = [
            f for f in audit.findings
            if f.rule_code == "R001" and f.affected_entity_id == requirement.requirement_id
        ]
        self.assertEqual(len(r001_missing), 0)

    def test_r009_still_fires_when_selected_document_has_issue(self):
        """
        Converse of the suppression test above: if the document CURRENTLY
        selected as a requirement's evidence itself has a cached
        unmet_requirement coherence issue, R009 must still fire. Suppression
        only applies to a SUPERSEDED document's stale complaint -- it must
        never silently hide a real, current problem just because some other
        document happens to exist in the stage.
        """
        stage = Stage(project_id=self.project.project_id, name="Product Definition", order_index=1, requires_approval=False)
        self.db.add(stage)
        self.db.commit()

        requirement = RequiredDocument(
            stage_id=stage.stage_id,
            name="Product concept",
            description="High-level credit-line concept and differentiation from competitors.",
            is_mandatory=True,
        )
        self.db.add(requirement)
        self.db.commit()

        doc = Document(
            tenant_id=self.tenant.tenant_id,
            project_id=self.project.project_id,
            stage_id=stage.stage_id,
            uploaded_by=self.user.user_id,
            uploaded_as_team_id=self.team.team_id,
            original_filename="03_product_concept_and_differentiation.md",
            mime_type="text/markdown",
        )
        self.db.add(doc)
        self.db.commit()
        version = DocumentVersion(
            document_id=doc.document_id, uploaded_by=self.user.user_id,
            file_data=b"...", file_size_bytes=3, status=DocumentStatus.indexed, version_number=1,
        )
        self.db.add(version)
        self.db.commit()
        doc.current_version_id = version.version_id
        self.db.commit()

        sync_project_graph(self.db, self.project.project_id)

        req_node = self.db.query(Node).filter(
            Node.project_id == self.project.project_id,
            Node.source_table == "required_documents",
            Node.source_id == requirement.requirement_id,
        ).first()
        doc_node = self.db.query(Node).filter(
            Node.project_id == self.project.project_id,
            Node.source_table == "documents",
            Node.source_id == doc.document_id,
        ).first()

        _upsert_edge(
            db=self.db, tenant_id=self.tenant.tenant_id, project_id=self.project.project_id,
            source_node_id=doc_node.node_id, target_node_id=req_node.node_id,
            edge_type="EVIDENCES",
            properties={"source": "regex", "rule": "title_or_content_evidence", "matched_requirement": "Product concept"},
            confidence=0.95,
        )
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

        audit = execute_project_audit(self.db, self.project.project_id, target_stage_id=stage.stage_id)

        r009_findings = [
            f for f in audit.findings
            if f.rule_code == "R009" and f.affected_entity_id == doc.document_id
        ]
        self.assertEqual(len(r009_findings), 1, "A real issue on the currently-selected document must still fire")
        self.assertTrue(r009_findings[0].is_blocker)

    def test_all_rules_wired_into_engine(self):
        """
        Verify every deterministic audit rule is registered and executed.
        The original R006 (orphan entity) was removed in Master Plan v2,
        item 13 as structurally unreachable (see the deleted
        test_r006_true_orphan_entity's replacement comment above). A second
        rule (Cross-Stage Reference Violation) later took the R006 code and
        was itself retired for flagging legitimate cross-stage document
        mentions — R007-R011 were then renumbered down to R006-R010, so
        R006-R010 below are the same rules the old R007-R011 codes used to
        name, not new checks.
        """
        stage = Stage(project_id=self.project.project_id, name="Initial Stage", order_index=1, requires_approval=False)
        self.db.add(stage)
        self.db.commit()

        # Check RULE_REGISTRY completeness
        rule_codes = [code for code, fn in RULE_REGISTRY]
        expected_rules = ["R001", "R002", "R003", "R004", "R005", "R006", "R007", "R008", "R009", "R010"]
        self.assertEqual(rule_codes, expected_rules)
        self.assertEqual(len(RULE_REGISTRY), 10)

        # Run audit and verify rules_evaluated == 10
        audit = execute_project_audit(self.db, self.project.project_id)
        self.assertEqual(audit.rules_evaluated, 10)


if __name__ == "__main__":
    unittest.main()
