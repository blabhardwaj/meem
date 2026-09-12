"""
DocFlow AI — Repeatable Demo Seed Script

Populates the database with the Lumen Retail organization, Product Launch Q1 project,
teams, stages, users with specific ABAC permissions, and demo documents processed
through the real application ingestion pipeline.

Usage:
    python -m seed.seed_demo            # Seed demo data (idempotent)
    python -m seed.seed_demo --reset    # Wipe Lumen Retail demo data and re-seed
    python -m seed.seed_demo --verify   # Run verification suite and print results
"""

import argparse
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select, func
from sqlalchemy.orm import Session

# Application imports
from app.main import app
from app.database import SessionLocal
from app.models.tenant import Tenant
from app.models.project import Project
from app.models.team import Team, TeamRole, UserTeamMembership, ProjectAdmin
from app.models.stage import Stage, TeamStageAccess
from app.models.user import User
from app.models.document import (
    Document,
    DocumentVersion,
    DocumentScan,
    DocumentTeamVisibility,
    DocumentStatus,
    SensitivityLevel,
)
from app.models.workflow import WorkflowState, WorkflowStatus
from app.models.audit import AuditLog
from app.services.auth import hash_password, create_session_token
from app.services.access_control import (
    classify_document_visibility,
    DocumentVisibility,
    has_stage_access,
)
from app.services.rag.collection_setup import (
    get_qdrant_client,
    collection_name_for_tenant,
)
from qdrant_client import models as qm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_demo")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LUMEN_TENANT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
COMPANY_NAME = "Lumen Retail"
PROJECT_NAME = "Product Launch Q1"
DEFAULT_PASSWORD = "DemoPass123!"

TEAMS = [
    "Engineering",
    "Marketing",
    "Leadership",
]

# Stages in order
STAGES = [
    {"name": "Planning", "order_index": 1, "requires_approval": False},
    {"name": "Design", "order_index": 2, "requires_approval": False},
    {"name": "Development", "order_index": 3, "requires_approval": False},
    {"name": "Testing", "order_index": 4, "requires_approval": True},
    {"name": "Release", "order_index": 5, "requires_approval": True},
]

# Stage Whitelist: which teams can see and upload to each stage.
# Release is deliberately empty so only project_admin / org_admin can upload.
STAGE_ACCESS = {
    "Planning": ["Engineering", "Marketing", "Leadership"],
    "Design": ["Engineering", "Marketing", "Leadership"],
    "Development": ["Engineering", "Marketing", "Leadership"],
    "Testing": ["Engineering", "Marketing"],
    "Release": [],  # Project admin only
}

USERS = [
    {
        "name": "Meera Kapoor",
        "email": "meera.kapoor@lumenretail.com",
        "is_org_admin": False,
        "is_project_admin": False,
        "memberships": [
            ("Engineering", TeamRole.team_lead),
            ("Marketing", TeamRole.viewer),
        ],
    },
    {
        "name": "Arjun Verma",
        "email": "arjun.verma@lumenretail.com",
        "is_org_admin": False,
        "is_project_admin": False,
        "memberships": [
            ("Engineering", TeamRole.contributor),
        ],
    },
    {
        "name": "Divya Shah",
        "email": "divya.shah@lumenretail.com",
        "is_org_admin": False,
        "is_project_admin": False,
        "memberships": [
            ("Marketing", TeamRole.contributor),
            # Engineering contributor membership added to author Doc 5
            ("Engineering", TeamRole.contributor),
        ],
    },
    {
        "name": "Vikram Nair",
        "email": "vikram.nair@lumenretail.com",
        "is_org_admin": False,
        "is_project_admin": False,
        "memberships": [
            ("Leadership", TeamRole.team_lead),
            # Marketing team_lead membership so he can author confidential Doc 3 and receive access requests
            ("Marketing", TeamRole.team_lead),
        ],
    },
    {
        "name": "Sneha Rao",
        "email": "sneha.rao@lumenretail.com",
        "is_org_admin": False,
        "is_project_admin": True,
        "memberships": [],
    },
    {
        "name": "Rohan Iyer",
        "email": "rohan.iyer@lumenretail.com",
        "is_org_admin": True,
        "is_project_admin": False,
        "memberships": [],
    },
    {
        "name": "Abhardwaj",
        "email": "blabhardwaj@gmail.com",
        "is_org_admin": True,
        "is_project_admin": True,
        "memberships": [],
    },
]

DOCUMENTS = [
    {
        "filename": "01-checkout-redesign-requirements.md",
        "stage": "Planning",
        "team": "Engineering",
        "sensitivity": "internal",
        "author_email": "arjun.verma@lumenretail.com",
        "upload_date": "2026-08-14T10:00:00Z",
        "action": "finalize",  # non-approval stage: finalize triggers indexing
    },
    {
        "filename": "02-q1-launch-gtm-plan.md",
        "stage": "Planning",
        "team": "Marketing",
        "sensitivity": "internal",
        "author_email": "divya.shah@lumenretail.com",
        "upload_date": "2026-08-28T10:00:00Z",
        "action": "finalize",
    },
    {
        "filename": "03-executive-pricing-strategy.md",
        "stage": "Planning",
        "team": "Marketing",
        "sensitivity": "confidential",
        "author_email": "vikram.nair@lumenretail.com",
        "upload_date": "2026-09-02T10:00:00Z",
        "action": "finalize",
    },
    {
        "filename": "04-checkout-payment-integration-spec.md",
        "stage": "Development",
        "team": "Engineering",
        "sensitivity": "internal",
        "author_email": "arjun.verma@lumenretail.com",
        "upload_date": "2026-09-04T10:00:00Z",
        "action": "finalize",
    },
    {
        "filename": "05-mobile-compatibility-test-plan.md",
        "stage": "Testing",
        "team": "Engineering",
        "sensitivity": "internal",
        "author_email": "divya.shah@lumenretail.com",
        "upload_date": "2026-09-06T10:00:00Z",
        "action": "finalize_submit_approve",  # Approver: Meera Kapoor
        "approver_email": "meera.kapoor@lumenretail.com",
    },
    {
        "filename": "06-payment-gateway-failover-test-plan.md",
        "stage": "Testing",
        "team": "Engineering",
        "sensitivity": "internal",
        "author_email": "arjun.verma@lumenretail.com",
        "upload_date": "2026-09-09T10:00:00Z",
        "action": "finalize_submit",  # Ends in pending_review, unapproved, 0 Qdrant points
    },
    {
        "filename": "07-release-readiness-checklist.md",
        "stage": "Release",
        "team": "Engineering",
        "sensitivity": "internal",
        "author_email": "sneha.rao@lumenretail.com",  # Project admin -> auto-approved on upload
        "upload_date": "2026-09-07T10:00:00Z",
        "action": "finalize",
    },
]


def find_docs_dir() -> Path:
    """Finds the directory containing the demo markdown files."""
    candidates = [
        Path("docs for demo"),
        Path("../docs for demo"),
        Path(__file__).resolve().parent.parent.parent / "docs for demo",
        Path("d:/Ra/DocFlowAI/docs for demo"),
    ]
    for c in candidates:
        if c.exists() and (c / "01-checkout-redesign-requirements.md").exists():
            return c.resolve()
    raise FileNotFoundError("Could not find 'docs for demo' directory with seed files.")


# ---------------------------------------------------------------------------
# Reset Logic
# ---------------------------------------------------------------------------

def reset_demo_data(db: Session):
    """
    Wipes all demo data belonging to Lumen Retail.
    Strictly scoped to LUMEN_TENANT_ID and COMPANY_NAME.
    """
    print(f"\n[*] Resetting demo data for '{COMPANY_NAME}' ({LUMEN_TENANT_ID})...")

    tenant = db.execute(
        select(Tenant).where(
            (Tenant.tenant_id == LUMEN_TENANT_ID) | (Tenant.name == COMPANY_NAME)
        )
    ).scalar_one_or_none()

    if not tenant:
        print("  [-] No existing Lumen Retail tenant found in DB.")
        return

    tenant_id = tenant.tenant_id

    # 1. Clean up Qdrant points for this tenant
    try:
        q_client = get_qdrant_client()
        col_name = collection_name_for_tenant(tenant_id)
        if q_client.collection_exists(col_name):
            print(f"  [-] Deleting Qdrant collection: {col_name}")
            q_client.delete_collection(col_name)
    except Exception as exc:
        print(f"  [!] Note: Qdrant cleanup notice: {exc}")

    # 2. Database cascading deletion scoped strictly to this tenant
    projects = db.execute(select(Project).where(Project.tenant_id == tenant_id)).scalars().all()
    project_ids = [p.project_id for p in projects]

    docs = db.execute(select(Document).where(Document.tenant_id == tenant_id)).scalars().all()
    doc_ids = [d.document_id for d in docs]

    if doc_ids:
        versions = db.execute(
            select(DocumentVersion).where(DocumentVersion.document_id.in_(doc_ids))
        ).scalars().all()
        version_ids = [v.version_id for v in versions]

        # Break current_version circular FK
        db.query(Document).filter(Document.document_id.in_(doc_ids)).update(
            {"current_version_id": None}, synchronize_session=False
        )
        db.flush()

        if version_ids:
            db.query(DocumentScan).filter(DocumentScan.version_id.in_(version_ids)).delete(synchronize_session=False)

        db.query(WorkflowState).filter(WorkflowState.document_id.in_(doc_ids)).delete(synchronize_session=False)
        db.query(DocumentTeamVisibility).filter(DocumentTeamVisibility.document_id.in_(doc_ids)).delete(synchronize_session=False)
        db.query(DocumentVersion).filter(DocumentVersion.document_id.in_(doc_ids)).delete(synchronize_session=False)
        db.query(Document).filter(Document.document_id.in_(doc_ids)).delete(synchronize_session=False)

    if project_ids:
        db.query(TeamStageAccess).filter(
            TeamStageAccess.stage_id.in_(
                select(Stage.stage_id).where(Stage.project_id.in_(project_ids))
            )
        ).delete(synchronize_session=False)

        db.query(Stage).filter(Stage.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(UserTeamMembership).filter(UserTeamMembership.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(ProjectAdmin).filter(ProjectAdmin.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(Team).filter(Team.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(Project).filter(Project.project_id.in_(project_ids)).delete(synchronize_session=False)

    users = db.execute(select(User).where(User.tenant_id == tenant_id)).scalars().all()
    user_ids = [u.user_id for u in users]
    if user_ids:
        db.query(AuditLog).filter(AuditLog.user_id.in_(user_ids)).delete(synchronize_session=False)
        db.query(User).filter(User.tenant_id == tenant_id).delete(synchronize_session=False)

    db.query(Tenant).filter(Tenant.tenant_id == tenant_id).delete(synchronize_session=False)
    db.commit()
    print("  [+] Reset complete.")


# ---------------------------------------------------------------------------
# Seed Logic
# ---------------------------------------------------------------------------

def seed_structure_and_users(db: Session) -> tuple[Tenant, Project, dict[str, Team], dict[str, Stage], dict[str, User]]:
    """Seeds tenant, project, teams, stages, and users."""
    print("\n[*] Seeding Organization, Project, Teams & Stages...")

    # Tenant
    tenant = db.get(Tenant, LUMEN_TENANT_ID)
    if not tenant:
        tenant = Tenant(tenant_id=LUMEN_TENANT_ID, name=COMPANY_NAME)
        db.add(tenant)
        db.flush()
        print(f"  [+] Created Tenant: {COMPANY_NAME} ({tenant.tenant_id})")
    else:
        print(f"  [=] Tenant already exists: {COMPANY_NAME}")

    # Project
    project = db.execute(
        select(Project).where(Project.tenant_id == tenant.tenant_id, Project.name == PROJECT_NAME)
    ).scalar_one_or_none()
    if not project:
        project = Project(tenant_id=tenant.tenant_id, name=PROJECT_NAME)
        db.add(project)
        db.flush()
        print(f"  [+] Created Project: {PROJECT_NAME} ({project.project_id})")
    else:
        print(f"  [=] Project already exists: {PROJECT_NAME}")

    # Teams
    teams: dict[str, Team] = {}
    for team_name in TEAMS:
        team = db.execute(
            select(Team).where(Team.project_id == project.project_id, Team.name == team_name)
        ).scalar_one_or_none()
        if not team:
            team = Team(project_id=project.project_id, name=team_name)
            db.add(team)
            db.flush()
            print(f"  [+] Created Team: {team_name}")
        teams[team_name] = team

    # Stages
    stages: dict[str, Stage] = {}
    for stg in STAGES:
        stage = db.execute(
            select(Stage).where(
                Stage.project_id == project.project_id,
                Stage.name == stg["name"],
                Stage.deleted_at.is_(None),
            )
        ).scalar_one_or_none()
        if not stage:
            stage = Stage(
                project_id=project.project_id,
                name=stg["name"],
                order_index=stg["order_index"],
                requires_approval=stg["requires_approval"],
            )
            db.add(stage)
            db.flush()
            print(f"  [+] Created Stage: {stg['name']} (order={stg['order_index']}, requires_approval={stg['requires_approval']})")
        else:
            if stage.requires_approval != stg["requires_approval"] or stage.order_index != stg["order_index"]:
                stage.requires_approval = stg["requires_approval"]
                stage.order_index = stg["order_index"]
                db.flush()
        stages[stg["name"]] = stage

    # TeamStageAccess grants
    for stage_name, team_names in STAGE_ACCESS.items():
        stage = stages[stage_name]
        for t_name in team_names:
            t = teams[t_name]
            existing = db.execute(
                select(TeamStageAccess).where(
                    TeamStageAccess.team_id == t.team_id,
                    TeamStageAccess.stage_id == stage.stage_id,
                )
            ).scalar_one_or_none()
            if not existing:
                db.add(TeamStageAccess(team_id=t.team_id, stage_id=stage.stage_id))
                db.flush()
                print(f"  [+] Granted Stage Access: Team '{t_name}' -> Stage '{stage_name}'")

    # Users
    print("\n[*] Seeding Users and Roles...")
    users: dict[str, User] = {}
    pw_hash = hash_password(DEFAULT_PASSWORD)

    for u_info in USERS:
        user = db.execute(select(User).where(User.email == u_info["email"])).scalar_one_or_none()
        if not user:
            user = User(
                email=u_info["email"],
                tenant_id=tenant.tenant_id,
                full_name=u_info["name"],
                password_hash=pw_hash,
                is_org_admin=u_info["is_org_admin"],
            )
            db.add(user)
            db.flush()
            print(f"  [+] Created User: {u_info['name']} <{u_info['email']}>")
        else:
            user.tenant_id = tenant.tenant_id
            user.full_name = u_info["name"]
            user.is_org_admin = u_info["is_org_admin"]
            user.password_hash = pw_hash
            db.flush()
        users[u_info["email"]] = user

        # Project admin
        if u_info["is_project_admin"]:
            pa = db.execute(
                select(ProjectAdmin).where(
                    ProjectAdmin.user_id == user.user_id,
                    ProjectAdmin.project_id == project.project_id,
                )
            ).scalar_one_or_none()
            if not pa:
                db.add(ProjectAdmin(user_id=user.user_id, project_id=project.project_id))
                db.flush()
                print(f"  [+] Assigned Project Admin: {u_info['name']}")

        # Team memberships
        for t_name, role in u_info["memberships"]:
            t = teams[t_name]
            mem = db.execute(
                select(UserTeamMembership).where(
                    UserTeamMembership.user_id == user.user_id,
                    UserTeamMembership.team_id == t.team_id,
                    UserTeamMembership.project_id == project.project_id,
                )
            ).scalar_one_or_none()
            if not mem:
                db.add(
                    UserTeamMembership(
                        user_id=user.user_id,
                        team_id=t.team_id,
                        project_id=project.project_id,
                        role=role,
                    )
                )
                db.flush()
                print(f"  [+] Membership: {u_info['name']} -> {t_name} ({role.value})")

    db.commit()
    return tenant, project, teams, stages, users


def seed_documents(
    db: Session,
    client: TestClient,
    docs_dir: Path,
    project: Project,
    teams: dict[str, Team],
    stages: dict[str, Stage],
    users: dict[str, User],
):
    """
    Ingests all 7 demo documents through the authentic API endpoints.
    """
    print(f"\n[*] Ingesting 7 demo documents from '{docs_dir}'...")

    for d_spec in DOCUMENTS:
        fname = d_spec["filename"]
        stage = stages[d_spec["stage"]]
        team = teams[d_spec["team"]]
        author = users[d_spec["author_email"]]
        fpath = docs_dir / fname

        if not fpath.exists():
            raise FileNotFoundError(f"Missing required document file: {fpath}")

        # Check if already seeded
        existing_doc = db.execute(
            select(Document).where(
                Document.project_id == project.project_id,
                Document.original_filename == fname,
            )
        ).scalar_one_or_none()

        if existing_doc:
            print(f"  [=] Document '{fname}' already exists ({existing_doc.document_id}). Skipping upload.")
            continue

        print(f"\n  --> Uploading '{fname}' as {d_spec['author_email']} ({d_spec['team']} -> {d_spec['stage']})...")

        with open(fpath, "rb") as f:
            file_bytes = f.read()

        auth_headers = {"Authorization": f"Bearer {create_session_token(str(author.user_id))}"}

        # 1. Real Upload Endpoint
        up_res = client.post(
            "/documents/upload-file",
            headers=auth_headers,
            files={"file": (fname, file_bytes, "text/markdown")},
            data={
                "stage_id": str(stage.stage_id),
                "team_id": str(team.team_id),
                "sensitivity_level": d_spec["sensitivity"],
            },
        )
        if up_res.status_code != 201:
            raise RuntimeError(f"Failed to upload '{fname}': {up_res.status_code} - {up_res.text}")

        up_data = up_res.json()
        doc_id = up_data["document_id"]
        session_id = up_data["session_id"]
        print(f"      Uploaded: doc_id={doc_id}, status={up_data['status']}, score={(up_data.get('scan') or {}).get('overall_score')}")

        # 2. Finalize Endpoint
        print(f"      Finalizing review session {session_id}...")
        fin_res = client.post(
            "/documents/review/message",
            headers=auth_headers,
            json={
                "document_id": doc_id,
                "session_id": session_id,
                "message": "Looks good, finalize it.",
            },
        )
        if fin_res.status_code != 200:
            raise RuntimeError(f"Failed to finalize '{fname}': {fin_res.status_code} - {fin_res.text}")

        fin_data = fin_res.json()
        print(f"      Finalized: status={fin_data.get('status')}, should_index={fin_data.get('should_index')}")

        # 3. Workflow Actions (Submit / Approve)
        if d_spec["action"] == "finalize_submit":
            # Document 6: Arjun Verma submits for review
            print("      Submitting for review (Testing stage)...")
            sub_res = client.post(
                f"/documents/{doc_id}/submit",
                headers=auth_headers,
            )
            if sub_res.status_code != 200:
                raise RuntimeError(f"Failed to submit '{fname}': {sub_res.status_code} - {sub_res.text}")
            print("      Submitted: awaiting approval by Engineering team_lead (Meera Kapoor).")

        elif d_spec["action"] == "finalize_submit_approve":
            # Document 5: Divya Shah submits, Meera Kapoor approves
            print("      Submitting for review (Testing stage)...")
            sub_res = client.post(
                f"/documents/{doc_id}/submit",
                headers=auth_headers,
            )
            if sub_res.status_code != 200:
                raise RuntimeError(f"Failed to submit '{fname}': {sub_res.status_code} - {sub_res.text}")

            approver = users[d_spec["approver_email"]]
            approver_headers = {"Authorization": f"Bearer {create_session_token(str(approver.user_id))}"}
            print(f"      Approving as {d_spec['approver_email']}...")
            app_res = client.post(
                f"/documents/{doc_id}/approve",
                headers=approver_headers,
            )
            if app_res.status_code != 200:
                raise RuntimeError(f"Failed to approve '{fname}': {app_res.status_code} - {app_res.text}")
            print("      Approved and indexed!")

        # 4. Backdate timestamp to match manifest consistency
        if d_spec.get("upload_date"):
            dt = datetime.fromisoformat(d_spec["upload_date"].replace("Z", "+00:00"))
            doc_obj = db.get(Document, uuid.UUID(doc_id))
            if doc_obj:
                doc_obj.created_at = dt
                for v in db.execute(select(DocumentVersion).where(DocumentVersion.document_id == doc_obj.document_id)).scalars():
                    v.created_at = dt
                db.commit()

    print("\n[+] All 7 documents successfully ingested and processed.")


# ---------------------------------------------------------------------------
# Verification Suite (--verify)
# ---------------------------------------------------------------------------

def run_verification(db: Session) -> bool:
    """Runs all 16 verification checks and prints a detailed report."""
    print("\n" + "=" * 80)
    print(" DOCFLOW AI DEMO SEED VERIFICATION SUITE")
    print("=" * 80)

    results: list[tuple[int, str, bool, str]] = []

    def check(num: int, title: str, condition: bool, note: str = ""):
        results.append((num, title, condition, note))

    # Fetch foundational records
    tenant = db.execute(
        select(Tenant).where((Tenant.tenant_id == LUMEN_TENANT_ID) | (Tenant.name == COMPANY_NAME))
    ).scalar_one_or_none()

    if not tenant:
        print("[!] FATAL: Lumen Retail tenant not found in database.")
        return False

    project = db.execute(
        select(Project).where(Project.tenant_id == tenant.tenant_id, Project.name == PROJECT_NAME)
    ).scalar_one_or_none()

    if not project:
        print("[!] FATAL: Product Launch Q1 project not found in database.")
        return False

    stages = db.execute(
        select(Stage).where(Stage.project_id == project.project_id, Stage.deleted_at.is_(None)).order_by(Stage.order_index)
    ).scalars().all()

    teams = db.execute(select(Team).where(Team.project_id == project.project_id)).scalars().all()
    team_map = {t.name: t for t in teams}

    # 1. Structure: 3 teams, 5 stages in right order
    stage_names = [s.name for s in stages]
    exp_stages = ["Planning", "Design", "Development", "Testing", "Release"]
    check(
        1,
        "Company, project, 3 teams, and 5 stages exist with right order",
        len(teams) == 3 and stage_names == exp_stages,
        f"Teams: {[t.name for t in teams]}, Stages: {stage_names}",
    )

    # 2. Testing and Release have requires_approval set
    stage_by_name = {s.name: s for s in stages}
    testing_req = stage_by_name.get("Testing") and stage_by_name["Testing"].requires_approval
    release_req = stage_by_name.get("Release") and stage_by_name["Release"].requires_approval
    plan_no_req = stage_by_name.get("Planning") and not stage_by_name["Planning"].requires_approval
    check(
        2,
        "Testing and Release require approval; earlier stages do not",
        bool(testing_req and release_req and plan_no_req),
        f"Planning={plan_no_req}, Testing={testing_req}, Release={release_req}",
    )

    # 3. All 7 users exist and can authenticate via /auth/login
    client = TestClient(app)
    auth_ok = True
    auth_notes = []
    for u in USERS:
        r = client.post("/auth/login", json={"email": u["email"], "password": DEFAULT_PASSWORD})
        if r.status_code != 200:
            auth_ok = False
            auth_notes.append(f"{u['email']}: {r.status_code}")
        else:
            token = r.json().get("access_token")
            if not token:
                auth_ok = False
                auth_notes.append(f"{u['email']}: no token")
    check(
        3,
        "All 7 users exist and can authenticate with password",
        auth_ok,
        "All authenticated successfully" if auth_ok else f"Failed: {', '.join(auth_notes)}",
    )

    # 4 & 5. Meera memberships
    meera = db.execute(select(User).where(User.email == "meera.kapoor@lumenretail.com")).scalar_one_or_none()
    meera_mems = db.execute(
        select(UserTeamMembership).where(
            UserTeamMembership.user_id == meera.user_id,
            UserTeamMembership.project_id == project.project_id,
        )
    ).scalars().all() if meera else []
    meera_roles = {m.team_id: m.role for m in meera_mems}

    eng_team = team_map.get("Engineering")
    mkt_team = team_map.get("Marketing")
    ldr_team = team_map.get("Leadership")

    m4_ok = (
        len(meera_mems) == 2
        and meera_roles.get(eng_team.team_id if eng_team else None) == TeamRole.team_lead
        and meera_roles.get(mkt_team.team_id if mkt_team else None) == TeamRole.viewer
    )
    check(
        4,
        "Meera has exactly 2 memberships: team_lead on Engineering, viewer on Marketing",
        m4_ok,
        f"Memberships: {len(meera_mems)}",
    )

    m5_ok = ldr_team and (ldr_team.team_id not in meera_roles)
    check(5, "Meera has no membership on Leadership", bool(m5_ok))

    # Documents checks
    docs = db.execute(select(Document).where(Document.project_id == project.project_id)).scalars().all()
    doc_by_fname = {d.original_filename: d for d in docs}

    # 6. All 7 documents exist with correct stage, team, sensitivity
    d6_ok = len(docs) == 7
    d6_notes = []
    for d_spec in DOCUMENTS:
        fn = d_spec["filename"]
        d = doc_by_fname.get(fn)
        if not d:
            d6_ok = False
            d6_notes.append(f"{fn} missing")
            continue
        stg = stage_by_name.get(d_spec["stage"])
        tm = team_map.get(d_spec["team"])
        if d.stage_id != stg.stage_id or d.uploaded_as_team_id != tm.team_id or d.sensitivity_level.name != d_spec["sensitivity"]:
            d6_ok = False
            d6_notes.append(f"{fn} metadata mismatch")
    check(
        6,
        "All seven documents exist with correct stage, team, and sensitivity",
        d6_ok,
        "All 7 present and matched" if d6_ok else "; ".join(d6_notes),
    )

    # 7. Document 6 is in submitted state (pending_review)
    doc6 = doc_by_fname.get("06-payment-gateway-failover-test-plan.md")
    wf6 = db.execute(select(WorkflowState).where(WorkflowState.document_id == doc6.document_id)).scalar_one_or_none() if doc6 else None
    check(
        7,
        "Document 6 is in submitted state with pending review (Meera is approver)",
        bool(wf6 and wf6.state == WorkflowStatus.pending_review and wf6.approved_by is None),
        f"State: {wf6.state.value if wf6 else 'None'}",
    )

    # 8. The other six are approved
    other_approved = True
    for d_spec in DOCUMENTS:
        if d_spec["filename"] == "06-payment-gateway-failover-test-plan.md":
            continue
        d = doc_by_fname.get(d_spec["filename"])
        if not d:
            other_approved = False
            continue
        stg = stage_by_name.get(d_spec["stage"])
        if stg.requires_approval:
            wf = db.execute(select(WorkflowState).where(WorkflowState.document_id == d.document_id)).scalar_one_or_none()
            if not wf or wf.state != WorkflowStatus.approved:
                other_approved = False
        else:
            ver = db.get(DocumentVersion, d.current_version_id) if d.current_version_id else None
            if not ver or ver.status != DocumentStatus.indexed:
                other_approved = False
    check(8, "The other six documents are approved / finalized", other_approved)

    # 9. Every approved document has a document_version row marked current
    current_vers_ok = True
    for d_spec in DOCUMENTS:
        if d_spec["filename"] == "06-payment-gateway-failover-test-plan.md":
            continue
        d = doc_by_fname.get(d_spec["filename"])
        if not d or not d.current_version_id:
            current_vers_ok = False
    check(9, "Every approved document has a document_version row marked current", current_vers_ok)

    # 10. uploaded_by on document 4 is Arjun Verma
    doc4 = doc_by_fname.get("04-checkout-payment-integration-spec.md")
    arjun = db.execute(select(User).where(User.email == "arjun.verma@lumenretail.com")).scalar_one_or_none()
    check(
        10,
        "uploaded_by on document 4 is Arjun Verma",
        bool(doc4 and arjun and doc4.uploaded_by == arjun.user_id),
    )

    # 11 & 12. Qdrant indexing check
    q_client = get_qdrant_client()
    col_name = collection_name_for_tenant(tenant.tenant_id)
    col_exists = q_client.collection_exists(col_name)

    qdrant_indexed_ok = col_exists
    qdrant_notes = []
    doc6_zero_points = True

    if col_exists:
        for d_spec in DOCUMENTS:
            d = doc_by_fname.get(d_spec["filename"])
            if not d:
                continue
            res, _ = q_client.scroll(
                collection_name=col_name,
                scroll_filter=qm.Filter(
                    must=[qm.FieldCondition(key="document_id", match=qm.MatchValue(value=str(d.document_id)))]
                ),
                limit=10,
                with_payload=True,
            )
            count = len(res)
            if d_spec["filename"] == "06-payment-gateway-failover-test-plan.md":
                if count != 0:
                    doc6_zero_points = False
            else:
                if count == 0:
                    qdrant_indexed_ok = False
                    qdrant_notes.append(f"{d_spec['filename']}: 0 points")
                else:
                    # Verify payload fields
                    sample_payload = res[0].payload
                    req_fields = ["document_id", "version_id", "stage_id", "chunk_text", "section_title"]
                    if not all(k in sample_payload for k in req_fields):
                        qdrant_indexed_ok = False
                        qdrant_notes.append(f"{d_spec['filename']}: missing payload keys")
    else:
        qdrant_indexed_ok = False
        qdrant_notes.append("Collection does not exist")

    check(
        11,
        "Qdrant contains indexed points for approved documents with valid payloads",
        qdrant_indexed_ok,
        "Points present with payload fields" if qdrant_indexed_ok else "; ".join(qdrant_notes),
    )

    check(
        12,
        "Qdrant contains NO points for document 6 (unapproved / submitted)",
        doc6_zero_points,
    )

    # 13, 14, 15, 16. Access control as Meera
    doc1 = doc_by_fname.get("01-checkout-redesign-requirements.md")
    vis1 = classify_document_visibility(db, meera.user_id, doc1) if (meera and doc1) else None
    check(13, "Meera access to Document 1 (Engineering, internal) -> allowed", vis1 == DocumentVisibility.fully_allowed)

    doc2 = doc_by_fname.get("02-q1-launch-gtm-plan.md")
    vis2 = classify_document_visibility(db, meera.user_id, doc2) if (meera and doc2) else None
    check(14, "Meera access to Document 2 (Marketing, internal) -> allowed (viewer)", vis2 == DocumentVisibility.fully_allowed)

    doc3 = doc_by_fname.get("03-executive-pricing-strategy.md")
    vis3 = classify_document_visibility(db, meera.user_id, doc3) if (meera and doc3) else None
    check(
        15,
        "Meera access to Document 3 (Marketing, confidential) -> blocked by sensitivity",
        vis3 == DocumentVisibility.blocked_by_sensitivity,
        f"Result: {vis3.value if vis3 else 'None'}",
    )

    # 16. Meera uploading to Release stage -> denied
    rel_stage = stage_by_name.get("Release")
    rel_denied = True
    if meera and eng_team and rel_stage:
        # Check via has_stage_access
        eng_has_access = has_stage_access(db, meera.user_id, eng_team.team_id, rel_stage.stage_id, project.project_id)
        if eng_has_access:
            rel_denied = False
    check(
        16,
        "Meera uploading to Release stage -> denied (project admin only)",
        rel_denied,
        "has_stage_access == False",
    )

    # Print summary table
    print("\n" + "-" * 80)
    print(f"{'#':<3} | {'STATUS':<6} | {'CHECK DESCRIPTION':<50} | {'NOTES'}")
    print("-" * 80)

    all_passed = True
    for num, title, passed, note in results:
        status_str = "PASS" if passed else "FAIL"
        if not passed:
            all_passed = False
        print(f"{num:<3} | {status_str:<6} | {title:<50} | {note}")

    print("-" * 80)
    print(f"Result: {'ALL 16 CHECKS PASSED!' if all_passed else 'SOME CHECKS FAILED'}\n")
    return all_passed


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="DocFlow AI Demo Seed Script")
    parser.add_argument("--reset", action="store_true", help="Wipe Lumen Retail demo data before seeding")
    parser.add_argument("--verify", action="store_true", help="Run verification checks without seeding")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.verify:
            success = run_verification(db)
            sys.exit(0 if success else 1)

        if args.reset:
            reset_demo_data(db)

        # Seed data
        docs_dir = find_docs_dir()
        tenant, project, teams, stages, users = seed_structure_and_users(db)

        client = TestClient(app)
        seed_documents(db, client, docs_dir, project, teams, stages, users)

        # Run verification after seeding
        print("\n[*] Running post-seed verification...")
        success = run_verification(db)
        if not success:
            logger.error("Seeding completed but one or more verification checks failed.")
            sys.exit(1)
        else:
            print("[+] Seed and verification finished successfully!")

    finally:
        db.close()


if __name__ == "__main__":
    main()
