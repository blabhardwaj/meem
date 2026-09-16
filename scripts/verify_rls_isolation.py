"""
Automated Verification Suite for Step 5: Multi-Tenant PostgreSQL RLS & Session Isolation
Strictly implementing the verification specification in implementation_plan.md lines 739–779.

Protocol:
- Test Fixture Setup:
  * Seed Tenant A:
    - 2 projects (Project A1, Project A2)
    - 4 stages (2 stages per project)
    - 4 teams (2 teams per project)
    - 2 documents (Doc A1, Doc A2)
    - Supporting rows in 2-hop tables: team_stage_access, stage_references, required_documents, chat_messages, document_scans
  * Seed Tenant B:
    - 2 projects (Project B1, Project B2)
    - 4 stages (2 stages per project)
    - 4 teams (2 teams per project)
    - 2 documents (Doc B1, Doc B2)
    - Supporting rows in 2-hop tables for Tenant B

- Pass 1: EXPLAIN ANALYZE on 2-Hop Tables:
  * Execute on active connection with SET LOCAL app.current_tenant_id = '<Tenant A>'
  * SET LOCAL enable_seqscan = off
  * EXPLAIN ANALYZE on:
    - team_stage_access
    - stage_references
    - required_documents
    - chat_messages
    - document_scans
  * Assertions:
    - Zero recursion errors (no "infinite recursion detected in policy")
    - Query plan executes using index scans ("Index Scan" or "Bitmap Index Scan")

- Pass 2: Positive and Negative Isolation Assertions:
  * Context = Tenant A:
    - Query projects: len == 2, all tenant_id == Tenant A, Project B1 not in results
    - Query stages: len == 4, all project_id in [A1, A2], Stage B1 not in results
    - Query documents: len == 2, all tenant_id == Tenant A, Doc B1 not in results
  * Context = Tenant B:
    - Query projects: len == 2, all tenant_id == Tenant B, Project A1 not in results
    - Query stages: len == 4, all project_id in [B1, B2], Stage A1 not in results
    - Query documents: len == 2, all tenant_id == Tenant B, Doc A1 not in results
  * Context = Unset / Empty string ('app.current_tenant_id = ""'):
    - Query projects: len == 0
    - Query stages: len == 0
    - Query documents: len == 0
    - Query teams: len == 0
"""
import os
import sys
import uuid
from sqlalchemy import text

sys.path.insert(0, os.path.abspath("."))

from app.database import SessionLocal
from app.models.tenant import Tenant
from app.models.user import User
from app.models.project import Project
from app.models.stage import Stage, StageReference, TeamStageAccess
from app.models.team import Team
from app.models.required_document import RequiredDocument
from app.models.document import Document, DocumentVersion, DocumentScan
from app.models.chat import ChatSession, ChatMessage


def run_rls_verification():
    print("================================================================================")
    print("STARTING STRICT RLS ISOLATION VERIFICATION (implementation_plan.md §8)")
    print("================================================================================")

    db = SessionLocal()
    tenant_a_id = uuid.uuid4()
    tenant_b_id = uuid.uuid4()

    try:
        print(f"\n[SETUP] Creating Tenant A ({tenant_a_id}) and Tenant B ({tenant_b_id})...")
        tenant_a = Tenant(tenant_id=tenant_a_id, name=f"RLS Org A {tenant_a_id.hex[:6]}")
        tenant_b = Tenant(tenant_id=tenant_b_id, name=f"RLS Org B {tenant_b_id.hex[:6]}")
        db.add_all([tenant_a, tenant_b])
        db.commit()

        # Seed Tenant A Fixture (2 projects, 4 stages, 4 teams, 2 documents + 2-hop rows)
        print("  -> Seeding Tenant A fixture: 2 projects, 4 stages, 4 teams, 2 documents...")
        db.info["tenant_id"] = str(tenant_a_id)
        db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(tenant_a_id)})

        user_a = User(email=f"user_a_{tenant_a_id.hex[:6]}@docflow.test", tenant_id=tenant_a_id)
        db.add(user_a)
        db.commit()
        user_a_id = user_a.user_id

        p_a1 = Project(name="Project A1", tenant_id=tenant_a_id)
        p_a2 = Project(name="Project A2", tenant_id=tenant_a_id)
        db.add_all([p_a1, p_a2])
        db.commit()
        p_a1_id = p_a1.project_id
        p_a2_id = p_a2.project_id

        s_a1_1 = Stage(name="Stage A1-1", project_id=p_a1_id, order_index=1)
        s_a1_2 = Stage(name="Stage A1-2", project_id=p_a1_id, order_index=2)
        s_a2_1 = Stage(name="Stage A2-1", project_id=p_a2_id, order_index=1)
        s_a2_2 = Stage(name="Stage A2-2", project_id=p_a2_id, order_index=2)
        db.add_all([s_a1_1, s_a1_2, s_a2_1, s_a2_2])

        t_a1_1 = Team(name="Team A1-1", project_id=p_a1_id)
        t_a1_2 = Team(name="Team A1-2", project_id=p_a1_id)
        t_a2_1 = Team(name="Team A2-1", project_id=p_a2_id)
        t_a2_2 = Team(name="Team A2-2", project_id=p_a2_id)
        db.add_all([t_a1_1, t_a1_2, t_a2_1, t_a2_2])
        db.commit()

        s_a_ids = [s_a1_1.stage_id, s_a1_2.stage_id, s_a2_1.stage_id, s_a2_2.stage_id]
        t_a_ids = [t_a1_1.team_id, t_a1_2.team_id, t_a2_1.team_id, t_a2_2.team_id]

        d_a1 = Document(
            tenant_id=tenant_a_id, project_id=p_a1_id, stage_id=s_a_ids[0],
            uploaded_by=user_a_id, uploaded_as_team_id=t_a_ids[0],
            original_filename="doc_a1.pdf", mime_type="application/pdf",
        )
        d_a2 = Document(
            tenant_id=tenant_a_id, project_id=p_a2_id, stage_id=s_a_ids[2],
            uploaded_by=user_a_id, uploaded_as_team_id=t_a_ids[2],
            original_filename="doc_a2.pdf", mime_type="application/pdf",
        )
        db.add_all([d_a1, d_a2])
        db.commit()
        d_a_ids = [d_a1.document_id, d_a2.document_id]

        # 2-Hop rows for Tenant A
        dv_a1 = DocumentVersion(
            document_id=d_a_ids[0], version_number=1, file_data=b"%PDF-1.4 sample",
            file_size_bytes=15, uploaded_by=user_a_id,
        )
        db.add(dv_a1)
        db.commit()
        dv_a1_id = dv_a1.version_id

        scan_a1 = DocumentScan(
            version_id=dv_a1_id, overall_score=88, criteria=[{"name": "completeness", "score": 90}],
        )
        tsa_a = TeamStageAccess(team_id=t_a_ids[0], stage_id=s_a_ids[0])
        sref_a = StageReference(stage_id=s_a_ids[0], references_stage_id=s_a_ids[1])
        req_a = RequiredDocument(stage_id=s_a_ids[0], name="Project Charter")
        chat_sess_a = ChatSession(user_id=user_a_id, project_id=p_a1_id)
        db.add_all([scan_a1, tsa_a, sref_a, req_a, chat_sess_a])
        db.commit()
        chat_sess_a_id = chat_sess_a.session_id

        chat_msg_a = ChatMessage(session_id=chat_sess_a_id, role="user", content="Hello RLS A")
        db.add(chat_msg_a)
        db.commit()

        # Seed Tenant B Fixture (2 projects, 4 stages, 4 teams, 2 documents + 2-hop rows)
        print("  -> Seeding Tenant B fixture: 2 projects, 4 stages, 4 teams, 2 documents...")
        db.info["tenant_id"] = str(tenant_b_id)
        db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(tenant_b_id)})

        user_b = User(email=f"user_b_{tenant_b_id.hex[:6]}@docflow.test", tenant_id=tenant_b_id)
        db.add(user_b)
        db.commit()
        user_b_id = user_b.user_id

        p_b1 = Project(name="Project B1", tenant_id=tenant_b_id)
        p_b2 = Project(name="Project B2", tenant_id=tenant_b_id)
        db.add_all([p_b1, p_b2])
        db.commit()
        p_b1_id = p_b1.project_id
        p_b2_id = p_b2.project_id

        s_b1_1 = Stage(name="Stage B1-1", project_id=p_b1_id, order_index=1)
        s_b1_2 = Stage(name="Stage B1-2", project_id=p_b1_id, order_index=2)
        s_b2_1 = Stage(name="Stage B2-1", project_id=p_b2_id, order_index=1)
        s_b2_2 = Stage(name="Stage B2-2", project_id=p_b2_id, order_index=2)
        db.add_all([s_b1_1, s_b1_2, s_b2_1, s_b2_2])

        t_b1_1 = Team(name="Team B1-1", project_id=p_b1_id)
        t_b1_2 = Team(name="Team B1-2", project_id=p_b1_id)
        t_b2_1 = Team(name="Team B2-1", project_id=p_b2_id)
        t_b2_2 = Team(name="Team B2-2", project_id=p_b2_id)
        db.add_all([t_b1_1, t_b1_2, t_b2_1, t_b2_2])
        db.commit()

        s_b_ids = [s_b1_1.stage_id, s_b1_2.stage_id, s_b2_1.stage_id, s_b2_2.stage_id]
        t_b_ids = [t_b1_1.team_id, t_b1_2.team_id, t_b2_1.team_id, t_b2_2.team_id]

        d_b1 = Document(
            tenant_id=tenant_b_id, project_id=p_b1_id, stage_id=s_b_ids[0],
            uploaded_by=user_b_id, uploaded_as_team_id=t_b_ids[0],
            original_filename="doc_b1.pdf", mime_type="application/pdf",
        )
        d_b2 = Document(
            tenant_id=tenant_b_id, project_id=p_b2_id, stage_id=s_b_ids[2],
            uploaded_by=user_b_id, uploaded_as_team_id=t_b_ids[2],
            original_filename="doc_b2.pdf", mime_type="application/pdf",
        )
        db.add_all([d_b1, d_b2])
        db.commit()
        d_b_ids = [d_b1.document_id, d_b2.document_id]

        # 2-Hop rows for Tenant B
        dv_b1 = DocumentVersion(
            document_id=d_b_ids[0], version_number=1, file_data=b"%PDF-1.4 sample b",
            file_size_bytes=17, uploaded_by=user_b_id,
        )
        db.add(dv_b1)
        db.commit()
        dv_b1_id = dv_b1.version_id

        scan_b1 = DocumentScan(
            version_id=dv_b1_id, overall_score=92, criteria=[{"name": "completeness", "score": 95}],
        )
        tsa_b = TeamStageAccess(team_id=t_b_ids[0], stage_id=s_b_ids[0])
        sref_b = StageReference(stage_id=s_b_ids[0], references_stage_id=s_b_ids[1])
        req_b = RequiredDocument(stage_id=s_b_ids[0], name="Security Spec")
        chat_sess_b = ChatSession(user_id=user_b_id, project_id=p_b1_id)
        db.add_all([scan_b1, tsa_b, sref_b, req_b, chat_sess_b])
        db.commit()
        chat_sess_b_id = chat_sess_b.session_id

        chat_msg_b = ChatMessage(session_id=chat_sess_b_id, role="user", content="Hello RLS B")
        db.add(chat_msg_b)
        db.commit()
        print("  -> Both fixtures successfully seeded.")

        # ----------------------------------------------------------------------
        # VERIFICATION PASS 1: EXPLAIN ANALYZE on 2-Hop Tables (Plan lines 752–762)
        # ----------------------------------------------------------------------
        print("\n[VERIFICATION PASS 1] Running EXPLAIN ANALYZE on 2-Hop RLS Tables...")
        tables_to_verify = [
            "team_stage_access",
            "stage_references",
            "required_documents",
            "chat_messages",
            "document_scans",
        ]

        sess_explain = SessionLocal()
        sess_explain.info["tenant_id"] = str(tenant_a_id)
        sess_explain.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(tenant_a_id)})
        # As explicitly specified in the plan: enable_seqscan = off to verify index scan path
        sess_explain.execute(text("SET LOCAL enable_seqscan = off;"))

        for tbl in tables_to_verify:
            explain_query = text(f"EXPLAIN ANALYZE SELECT * FROM {tbl};")
            try:
                res = sess_explain.execute(explain_query).fetchall()
                plan_str = "\n".join([row[0] for row in res])
            except Exception as exc:
                raise AssertionError(f"EXPLAIN ANALYZE failed on {tbl}: {exc}") from exc

            # Criterion 1: Zero recursion errors
            assert "infinite recursion detected in policy" not in plan_str.lower(), (
                f"Infinite recursion detected in policy for {tbl}!"
            )

            # Criterion 2: Query plan executes using index scans
            has_index_scan = "Index Scan" in plan_str or "Bitmap Index Scan" in plan_str
            assert has_index_scan, (
                f"Expected index scan in plan for {tbl}, but got:\n{plan_str}"
            )
            print(f"  -> {tbl:22}: PASSED (No recursion, Verified Index Scan)")

        sess_explain.close()

        # ----------------------------------------------------------------------
        # VERIFICATION PASS 2: Positive & Negative Isolation (Plan lines 763–779)
        # ----------------------------------------------------------------------
        print("\n[VERIFICATION PASS 2] Running Exact Multi-Tenant Isolation Assertions...")

        all_stage_ids_ab = s_a_ids + s_b_ids
        all_team_ids_ab = t_a_ids + t_b_ids

        # 1. Context = Tenant A
        print("  [Context = Tenant A]")
        sess_a = SessionLocal()
        sess_a.info["tenant_id"] = str(tenant_a_id)
        sess_a.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(tenant_a_id)})

        # Query projects: assert len == 2, all tenant_id == Tenant A, Project B1 not in results
        projects_a = sess_a.query(Project).filter(Project.tenant_id.in_([tenant_a_id, tenant_b_id])).all()
        assert len(projects_a) == 2, f"Expected 2 projects for Tenant A, got {len(projects_a)}"
        assert all(p.tenant_id == tenant_a_id for p in projects_a), "Cross-tenant project leaked to Tenant A!"
        assert p_b1_id not in [p.project_id for p in projects_a], "Project B1 leaked to Tenant A!"
        print("    * projects: exactly 2 returned, all match Tenant A, Project B1 excluded")

        # Query stages: assert len == 4, all project_id in [A1, A2], Stage B1 not in results
        stages_a = sess_a.query(Stage).filter(Stage.stage_id.in_(all_stage_ids_ab)).all()
        assert len(stages_a) == 4, f"Expected 4 stages for Tenant A, got {len(stages_a)}"
        assert all(s.project_id in [p_a1_id, p_a2_id] for s in stages_a), "Stage from Tenant B leaked!"
        assert s_b_ids[0] not in [s.stage_id for s in stages_a], "Stage B1-1 leaked to Tenant A!"
        print("    * stages: exactly 4 returned, all match Tenant A projects, Stage B1 excluded")

        # Query documents: assert len == 2, all tenant_id == Tenant A, Doc B1 not in results
        docs_a = sess_a.query(Document).filter(Document.tenant_id.in_([tenant_a_id, tenant_b_id])).all()
        assert len(docs_a) == 2, f"Expected 2 documents for Tenant A, got {len(docs_a)}"
        assert all(d.tenant_id == tenant_a_id for d in docs_a), "Cross-tenant document leaked to Tenant A!"
        assert d_b_ids[0] not in [d.document_id for d in docs_a], "Doc B1 leaked to Tenant A!"
        print("    * documents: exactly 2 returned, all match Tenant A, Doc B1 excluded")
        sess_a.close()

        # 2. Context = Tenant B
        print("  [Context = Tenant B]")
        sess_b = SessionLocal()
        sess_b.info["tenant_id"] = str(tenant_b_id)
        sess_b.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(tenant_b_id)})

        # Query projects: assert len == 2, all tenant_id == Tenant B, Project A1 not in results
        projects_b = sess_b.query(Project).filter(Project.tenant_id.in_([tenant_a_id, tenant_b_id])).all()
        assert len(projects_b) == 2, f"Expected 2 projects for Tenant B, got {len(projects_b)}"
        assert all(p.tenant_id == tenant_b_id for p in projects_b), "Cross-tenant project leaked to Tenant B!"
        assert p_a1_id not in [p.project_id for p in projects_b], "Project A1 leaked to Tenant B!"
        print("    * projects: exactly 2 returned, all match Tenant B, Project A1 excluded")

        # Query stages: assert len == 4, all project_id in [B1, B2], Stage A1 not in results
        stages_b = sess_b.query(Stage).filter(Stage.stage_id.in_(all_stage_ids_ab)).all()
        assert len(stages_b) == 4, f"Expected 4 stages for Tenant B, got {len(stages_b)}"
        assert all(s.project_id in [p_b1_id, p_b2_id] for s in stages_b), "Stage from Tenant A leaked!"
        assert s_a_ids[0] not in [s.stage_id for s in stages_b], "Stage A1-1 leaked to Tenant B!"
        print("    * stages: exactly 4 returned, all match Tenant B projects, Stage A1 excluded")

        # Query documents: assert len == 2, all tenant_id == Tenant B, Doc A1 not in results
        docs_b = sess_b.query(Document).filter(Document.tenant_id.in_([tenant_a_id, tenant_b_id])).all()
        assert len(docs_b) == 2, f"Expected 2 documents for Tenant B, got {len(docs_b)}"
        assert all(d.tenant_id == tenant_b_id for d in docs_b), "Cross-tenant document leaked to Tenant B!"
        assert d_a_ids[0] not in [d.document_id for d in docs_b], "Doc A1 leaked to Tenant B!"
        print("    * documents: exactly 2 returned, all match Tenant B, Doc A1 excluded")
        sess_b.close()

        # 3. Context = Unset / Empty string (app.current_tenant_id = '')
        print("  [Context = Unset / Empty string]")
        sess_none = SessionLocal()
        sess_none.execute(text("SET LOCAL app.current_tenant_id = '';"))

        p_none = sess_none.query(Project).filter(Project.tenant_id.in_([tenant_a_id, tenant_b_id])).all()
        assert len(p_none) == 0, f"Unauthenticated session saw {len(p_none)} projects (expected 0)!"

        s_none = sess_none.query(Stage).filter(Stage.stage_id.in_(all_stage_ids_ab)).all()
        assert len(s_none) == 0, f"Unauthenticated session saw {len(s_none)} stages (expected 0)!"

        d_none = sess_none.query(Document).filter(Document.tenant_id.in_([tenant_a_id, tenant_b_id])).all()
        assert len(d_none) == 0, f"Unauthenticated session saw {len(d_none)} documents (expected 0)!"

        t_none = sess_none.query(Team).filter(Team.team_id.in_(all_team_ids_ab)).all()
        assert len(t_none) == 0, f"Unauthenticated session saw {len(t_none)} teams (expected 0)!"
        print("    * projects: exactly 0 returned")
        print("    * stages:   exactly 0 returned")
        print("    * documents: exactly 0 returned")
        print("    * teams:    exactly 0 returned")
        sess_none.close()

        print("\n================================================================================")
        print("ALL RLS ISOLATION PROTOCOL CHECKS (PASS 1 & PASS 2) PASSED STRICTLY!")
        print("================================================================================")

    finally:
        print("\n[CLEANUP] Tearing down test fixtures...")
        try:
            # Clean Tenant A
            cleanup_a = SessionLocal()
            cleanup_a.info["tenant_id"] = str(tenant_a_id)
            cleanup_a.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(tenant_a_id)})

            cleanup_a.query(ChatMessage).filter(ChatMessage.session_id == chat_sess_a_id).delete(synchronize_session=False)
            cleanup_a.query(ChatSession).filter(ChatSession.session_id == chat_sess_a_id).delete(synchronize_session=False)
            cleanup_a.query(DocumentScan).filter(DocumentScan.version_id == dv_a1_id).delete(synchronize_session=False)
            cleanup_a.query(DocumentVersion).filter(DocumentVersion.version_id == dv_a1_id).delete(synchronize_session=False)
            cleanup_a.query(StageReference).filter(StageReference.stage_id.in_(s_a_ids)).delete(synchronize_session=False)
            cleanup_a.query(RequiredDocument).filter(RequiredDocument.stage_id.in_(s_a_ids)).delete(synchronize_session=False)
            cleanup_a.query(TeamStageAccess).filter(TeamStageAccess.team_id.in_(t_a_ids)).delete(synchronize_session=False)
            cleanup_a.query(Document).filter(Document.document_id.in_(d_a_ids)).delete(synchronize_session=False)
            cleanup_a.query(Stage).filter(Stage.stage_id.in_(s_a_ids)).delete(synchronize_session=False)
            cleanup_a.query(Team).filter(Team.team_id.in_(t_a_ids)).delete(synchronize_session=False)
            cleanup_a.query(Project).filter(Project.project_id.in_([p_a1_id, p_a2_id])).delete(synchronize_session=False)
            cleanup_a.query(User).filter(User.user_id == user_a_id).delete(synchronize_session=False)
            cleanup_a.commit()
            cleanup_a.close()

            # Clean Tenant B
            cleanup_b = SessionLocal()
            cleanup_b.info["tenant_id"] = str(tenant_b_id)
            cleanup_b.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(tenant_b_id)})

            cleanup_b.query(ChatMessage).filter(ChatMessage.session_id == chat_sess_b_id).delete(synchronize_session=False)
            cleanup_b.query(ChatSession).filter(ChatSession.session_id == chat_sess_b_id).delete(synchronize_session=False)
            cleanup_b.query(DocumentScan).filter(DocumentScan.version_id == dv_b1_id).delete(synchronize_session=False)
            cleanup_b.query(DocumentVersion).filter(DocumentVersion.version_id == dv_b1_id).delete(synchronize_session=False)
            cleanup_b.query(StageReference).filter(StageReference.stage_id.in_(s_b_ids)).delete(synchronize_session=False)
            cleanup_b.query(RequiredDocument).filter(RequiredDocument.stage_id.in_(s_b_ids)).delete(synchronize_session=False)
            cleanup_b.query(TeamStageAccess).filter(TeamStageAccess.team_id.in_(t_b_ids)).delete(synchronize_session=False)
            cleanup_b.query(Document).filter(Document.document_id.in_(d_b_ids)).delete(synchronize_session=False)
            cleanup_b.query(Stage).filter(Stage.stage_id.in_(s_b_ids)).delete(synchronize_session=False)
            cleanup_b.query(Team).filter(Team.team_id.in_(t_b_ids)).delete(synchronize_session=False)
            cleanup_b.query(Project).filter(Project.project_id.in_([p_b1_id, p_b2_id])).delete(synchronize_session=False)
            cleanup_b.query(User).filter(User.user_id == user_b_id).delete(synchronize_session=False)
            cleanup_b.commit()
            cleanup_b.close()

            # Clean Tenants
            clean_tenants = SessionLocal()
            clean_tenants.query(Tenant).filter(Tenant.tenant_id.in_([tenant_a_id, tenant_b_id])).delete(synchronize_session=False)
            clean_tenants.commit()
            clean_tenants.close()
            print("  -> Cleanup complete.")
        except Exception as exc:
            print(f"  -> Note on cleanup: {exc}")

        db.close()


if __name__ == "__main__":
    run_rls_verification()
