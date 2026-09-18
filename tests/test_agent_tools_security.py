import json
import unittest
import uuid
from sqlalchemy import text
from app.database import SessionLocal
from app.models.document import Document, DocumentStatus, DocumentVersion
from app.models.graph import AuditFinding, AuditRun, Edge, Node
from app.models.project import Project
from app.models.required_document import RequiredDocument
from app.models.stage import Stage, TeamStageAccess
from app.models.team import Team, ProjectAdmin, UserTeamMembership, TeamRole
from app.models.tenant import Tenant
from app.models.user import User
from app.models.workflow import WorkflowState, WorkflowStatus
from app.services.query_context import set_query_context, reset_query_context
from app.services.graph.sync import sync_project_graph
from app.tools.graph_tools import (
    query_entity_neighborhood,
    query_project_gaps,
    query_project_readiness,
    query_project_timeline,
)


class TestAgentToolsSecurity(unittest.TestCase):
    def setUp(self):
        self.db = SessionLocal()
        # Tenant A
        self.tenant_a = self.db.query(Tenant).first()
        self.assertIsNotNone(self.tenant_a)
        self.db.info["tenant_id"] = str(self.tenant_a.tenant_id)
        self.db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(self.tenant_a.tenant_id)})

        # User A1 (Member of Team 1 in Project A)
        self.user_a1 = User(tenant_id=self.tenant_a.tenant_id, email=f"a1_{uuid.uuid4().hex[:6]}@example.com")
        # User Outsider (In Tenant A, but not in Project A)
        self.user_outsider = User(tenant_id=self.tenant_a.tenant_id, email=f"outsider_{uuid.uuid4().hex[:6]}@example.com")
        self.db.add_all([self.user_a1, self.user_outsider])
        self.db.commit()

        # Project A in Tenant A
        self.project_a = Project(tenant_id=self.tenant_a.tenant_id, name="Security Project A")
        # Project B in Tenant A
        self.project_b = Project(tenant_id=self.tenant_a.tenant_id, name="Security Project B")
        self.db.add_all([self.project_a, self.project_b])
        self.db.commit()
        self.project_a_id = self.project_a.project_id
        self.project_b_id = self.project_b.project_id

        # Teams in Project A
        self.team_1 = Team(project_id=self.project_a.project_id, name="Team Stage 1 Only")
        self.team_2 = Team(project_id=self.project_a.project_id, name="Team Stage 2 Only")
        self.db.add_all([self.team_1, self.team_2])
        self.db.commit()

        # User A1 belongs to Team 1
        self.m1 = UserTeamMembership(
            user_id=self.user_a1.user_id,
            team_id=self.team_1.team_id,
            project_id=self.project_a.project_id,
            role=TeamRole.contributor,
        )
        self.db.add(self.m1)
        self.db.commit()

        # Stages in Project A
        self.stage_1 = Stage(project_id=self.project_a.project_id, name="Open Stage 1", order_index=1, requires_approval=False)
        self.stage_2 = Stage(project_id=self.project_a.project_id, name="Confidential Stage 2", order_index=2, requires_approval=True)
        self.db.add_all([self.stage_1, self.stage_2])
        self.db.commit()

        # TeamStageAccess: Team 1 has access to Stage 1; Team 2 has access to Stage 2
        self.tsa1 = TeamStageAccess(team_id=self.team_1.team_id, stage_id=self.stage_1.stage_id)
        self.tsa2 = TeamStageAccess(team_id=self.team_2.team_id, stage_id=self.stage_2.stage_id)
        self.db.add_all([self.tsa1, self.tsa2])
        self.db.commit()

        # Documents: doc 1 in stage 1, doc 2 in stage 2 (unapproved / pending in stage 2)
        self.doc_1 = Document(
            tenant_id=self.tenant_a.tenant_id,
            project_id=self.project_a.project_id,
            stage_id=self.stage_1.stage_id,
            uploaded_by=self.user_a1.user_id,
            uploaded_as_team_id=self.team_1.team_id,
            original_filename="PublicStage1Doc.md",
            mime_type="text/markdown",
        )
        self.doc_2 = Document(
            tenant_id=self.tenant_a.tenant_id,
            project_id=self.project_a.project_id,
            stage_id=self.stage_2.stage_id,
            uploaded_by=self.user_a1.user_id,
            uploaded_as_team_id=self.team_2.team_id,
            original_filename="ConfidentialStage2Doc.md",
            mime_type="text/markdown",
        )
        self.db.add_all([self.doc_1, self.doc_2])
        self.db.commit()

        # Document 2 is unapproved in gate stage 2 -> creates R008 (pending workflow) blocker in stage 2
        wf2 = WorkflowState(document_id=self.doc_2.document_id, state=WorkflowStatus.pending_review)
        self.db.add(wf2)
        self.db.commit()

        sync_project_graph(self.db, self.project_a.project_id)

    def tearDown(self):
        self.db.rollback()
        self.db.info["tenant_id"] = str(self.tenant_a.tenant_id)
        self.db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(self.tenant_a.tenant_id)})
        for pid in [self.project_a_id, self.project_b_id]:
            self.db.query(AuditFinding).filter(AuditFinding.project_id == pid).delete()
            self.db.query(AuditRun).filter(AuditRun.project_id == pid).delete()
            self.db.query(Edge).filter(Edge.project_id == pid).delete()
            self.db.query(Node).filter(Node.project_id == pid).delete()

        doc_ids = [d.document_id for d in self.db.query(Document).filter(Document.project_id.in_([self.project_a_id, self.project_b_id])).all()]
        if doc_ids:
            self.db.query(WorkflowState).filter(WorkflowState.document_id.in_(doc_ids)).delete(synchronize_session=False)
            self.db.query(DocumentVersion).filter(DocumentVersion.document_id.in_(doc_ids)).delete(synchronize_session=False)
            self.db.query(Document).filter(Document.document_id.in_(doc_ids)).delete(synchronize_session=False)

        stage_ids = [s.stage_id for s in self.db.query(Stage).filter(Stage.project_id.in_([self.project_a_id, self.project_b_id])).all()]
        if stage_ids:
            self.db.query(TeamStageAccess).filter(TeamStageAccess.stage_id.in_(stage_ids)).delete(synchronize_session=False)
            self.db.query(Stage).filter(Stage.stage_id.in_(stage_ids)).delete(synchronize_session=False)

        self.db.query(UserTeamMembership).filter(UserTeamMembership.project_id.in_([self.project_a_id, self.project_b_id])).delete()
        self.db.query(Team).filter(Team.project_id.in_([self.project_a_id, self.project_b_id])).delete()
        self.db.query(Project).filter(Project.project_id.in_([self.project_a_id, self.project_b_id])).delete()
        self.db.query(User).filter(User.user_id.in_([self.user_a1.user_id, self.user_outsider.user_id])).delete()
        self.db.commit()
        self.db.close()

    def test_agent_tool_project_access_and_denial(self):
        """
        User A -> agent tool -> Project intelligence -> only User A's permitted data
        User A -> attempt to query Project B -> access denied
        Outsider -> attempt to query Project A -> access denied
        """
        # 1. User A1 queries permitted Project A via QueryContext
        token = set_query_context(user_id=self.user_a1.user_id, project_id=self.project_a.project_id)
        try:
            readiness_str = query_project_readiness(str(self.project_a.project_id))
            res = json.loads(readiness_str)
            self.assertNotIn("error", res)
            self.assertIn("readiness_status", res)
        finally:
            reset_query_context(token)

        # 2. User A1 attempts to query Project B (where User A1 has no access) -> ACCESS DENIED
        token = set_query_context(user_id=self.user_a1.user_id, project_id=self.project_b.project_id)
        try:
            readiness_b_str = query_project_readiness(str(self.project_b.project_id))
            res_b = json.loads(readiness_b_str)
            self.assertIn("error", res_b)
            self.assertIn("Access denied", res_b["error"])
        finally:
            reset_query_context(token)

        # 3. Outsider attempts to query Project A -> ACCESS DENIED
        token = set_query_context(user_id=self.user_outsider.user_id, project_id=self.project_a.project_id)
        try:
            outsider_res_str = query_project_readiness(str(self.project_a.project_id))
            res_out = json.loads(outsider_res_str)
            self.assertIn("error", res_out)
            self.assertIn("Access denied", res_out["error"])
        finally:
            reset_query_context(token)

    def test_agent_tool_stage_abac_and_redaction(self):
        """
        User A1 has access to Stage 1, but lacks access to Stage 2.
        - Direct query for Stage 2 -> Access denied
        - Direct query for Stage 1 -> Permitted
        - Gaps and Readiness for Project A do not leak Stage 2 blockers or descriptions
        - Neighborhood query on Document 2 (in Stage 2) -> Access denied
        - Neighborhood query on Stage 1 does not expose Stage 2 edges
        """
        token = set_query_context(user_id=self.user_a1.user_id, project_id=self.project_a.project_id)
        try:
            # 1. Query readiness targeting inaccessible Stage 2 -> ACCESS DENIED
            s2_readiness = json.loads(query_project_readiness(str(self.project_a.project_id), stage_id=str(self.stage_2.stage_id)))
            self.assertIn("error", s2_readiness)
            self.assertIn("Stage is not accessible", s2_readiness["error"])

            # 2. Query readiness targeting accessible Stage 1 -> PERMITTED
            s1_readiness = json.loads(query_project_readiness(str(self.project_a.project_id), stage_id=str(self.stage_1.stage_id)))
            self.assertNotIn("error", s1_readiness)
            self.assertEqual(s1_readiness["readiness_status"], "READY")  # No blockers in Stage 1!

            # 3. Project gaps: Stage 2 blockers (R002/R008 for ConfidentialStage2Doc) must be REDACTED
            gaps = json.loads(query_project_gaps(str(self.project_a.project_id)))
            self.assertNotIn("error", gaps)
            unapproved = gaps.get("unapproved_gate_documents", [])
            for desc in unapproved:
                self.assertNotIn("ConfidentialStage2Doc", desc)
                self.assertNotIn("Confidential Stage 2", desc)

            # 4. Neighborhood on inaccessible Document 2 -> ACCESS DENIED
            nh_doc2 = json.loads(query_entity_neighborhood(
                project_id=str(self.project_a.project_id),
                entity_type="document",
                entity_id=str(self.doc_2.document_id),
            ))
            self.assertIn("error", nh_doc2)
            self.assertIn("inaccessible", nh_doc2["error"].lower())

            # 5. Neighborhood on Stage 1 does not link to Stage 2
            nh_stage1 = json.loads(query_entity_neighborhood(
                project_id=str(self.project_a.project_id),
                entity_type="stage",
                entity_id=str(self.stage_1.stage_id),
            ))
            self.assertNotIn("error", nh_stage1)
            for conn in nh_stage1.get("connections", []):
                self.assertNotEqual(conn.get("related_entity"), "Confidential Stage 2")

        finally:
            reset_query_context(token)

    def test_cross_tenant_agent_access_denial(self):
        """
        User from Tenant B attempting to call agent tool on Project A in Tenant A -> Access denied
        """
        tenant_b = Tenant(name=f"Tenant B {uuid.uuid4().hex[:6]}")
        self.db.add(tenant_b)
        self.db.commit()

        self.db.info["tenant_id"] = str(tenant_b.tenant_id)
        self.db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(tenant_b.tenant_id)})
        user_b = User(tenant_id=tenant_b.tenant_id, email=f"user_b_{uuid.uuid4().hex[:6]}@example.com")
        self.db.add(user_b)
        self.db.commit()

        token = set_query_context(user_id=user_b.user_id, project_id=self.project_a_id)
        try:
            res = json.loads(query_project_readiness(str(self.project_a_id)))
            self.assertIn("error", res)
            self.assertIn("Access denied", res["error"])
        finally:
            reset_query_context(token)

        self.db.info["tenant_id"] = str(tenant_b.tenant_id)
        self.db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(tenant_b.tenant_id)})
        self.db.query(User).filter(User.user_id == user_b.user_id).delete()
        self.db.query(Tenant).filter(Tenant.tenant_id == tenant_b.tenant_id).delete()
        self.db.commit()

        self.db.info["tenant_id"] = str(self.tenant_a.tenant_id)
        self.db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(self.tenant_a.tenant_id)})

    def test_llm_cannot_spoof_caller_identity(self):
        """
        Verify that an authenticated user's session cannot be hijacked by the LLM supplying
        a different user_id parameter to the agent tool.
        """
        # User A1 is the real authenticated caller
        token = set_query_context(user_id=self.user_a1.user_id, project_id=self.project_a.project_id)
        try:
            # Model attempts to pass User Outsider's ID or another user's ID
            res_str = query_project_readiness(
                project_id=str(self.project_a.project_id),
                user_id=str(self.user_outsider.user_id),
            )
            res = json.loads(res_str)
            self.assertIn("error", res)
            self.assertIn("cannot impersonate", res["error"])
        finally:
            reset_query_context(token)


if __name__ == "__main__":
    unittest.main()
