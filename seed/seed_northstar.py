"""
DocFlow AI — Northstar Commerce Demo Seed & Evidence Verification Script

Populates and verifies an isolated demo dataset for Northstar Commerce
(Holiday Checkout Modernization project) strictly according to the
Northstar Demo Seed Manifest.

Usage:
    python -m seed.seed_northstar            # Seed demo data (idempotent: validate & preserve)
    python -m seed.seed_northstar --reset    # Wipe Northstar Commerce demo data and re-seed
    python -m seed.seed_northstar --verify   # Run verification suite and print results
"""

import argparse
import hashlib
import json
import logging
import re
import sys
import time
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
from app.models.graph import (
    Node,
    Edge,
    ExtractionRun,
    Claim,
    AuditRun,
    AuditFinding,
    ProjectMetricSnapshot,
    StageMetricSnapshot,
)
from app.services.auth import hash_password, create_session_token, ResolvedIdentity
from app.services.access_control import (
    classify_document_visibility,
    DocumentVisibility,
    has_stage_access,
    has_permission,
)
from app.services.rag.collection_setup import (
    get_qdrant_client,
    collection_name_for_tenant,
)
from app.services.graph.sync import sync_project_graph
from app.services.graph.relationship_extractor import extract_document_relationships
from app.services.graph.claims_analyzer import (
    extract_claims_from_text,
    persist_claims,
    detect_project_contradictions,
)
from app.services.graph.audit_engine import execute_project_audit
from app.services.graph.metrics_service import get_latest_project_health
from qdrant_client import models as qm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_northstar")

# ---------------------------------------------------------------------------
# Authoritative Configuration from Manifest
# ---------------------------------------------------------------------------

NORTHSTAR_TENANT_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
COMPANY_NAME = "Northstar Commerce"
PROJECT_NAME = "Holiday Checkout Modernization"
DEFAULT_PASSWORD = "DemoPass123!"

LUMEN_TENANT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")

TEAMS = [
    "Engineering",
    "Product",
    "QA",
    "Leadership",
]

# Stages in order
STAGES = [
    {"name": "Discovery", "order_index": 1, "requires_approval": False},
    {"name": "UX Design", "order_index": 2, "requires_approval": False},
    {"name": "Engineering", "order_index": 3, "requires_approval": False},
    {"name": "Validation", "order_index": 4, "requires_approval": True},
    {"name": "Launch", "order_index": 5, "requires_approval": True},
]

# Stage Whitelist: Launch is deliberately empty (Project Admin / Ishita only)
STAGE_ACCESS = {
    "Discovery": ["Product", "Leadership", "Engineering", "QA"],
    "UX Design": ["Product", "Engineering", "Leadership", "QA"],
    "Engineering": ["Engineering", "Product", "QA", "Leadership"],
    "Validation": ["Engineering", "QA", "Product", "Leadership"],
    "Launch": [],  # Project admin only
}

USERS = [
    {
        "name": "Ananya Mehta",
        "email": "ananya.mehta@northstarcommerce.com",
        "is_org_admin": False,
        "is_project_admin": False,
        "memberships": [
            ("Engineering", TeamRole.team_lead),
            ("Product", TeamRole.viewer),
        ],
    },
    {
        "name": "Kabir Singh",
        "email": "kabir.singh@northstarcommerce.com",
        "is_org_admin": False,
        "is_project_admin": False,
        "memberships": [
            ("Engineering", TeamRole.contributor),
        ],
    },
    {
        "name": "Ishita Malhotra",
        "email": "ishita.malhotra@northstarcommerce.com",
        "is_org_admin": False,
        "is_project_admin": True,
        "memberships": [
            ("Engineering", TeamRole.team_lead),
        ],
    },
    {
        "name": "Dev Malhotra",
        "email": "dev.malhotra@northstarcommerce.com",
        "is_org_admin": False,
        "is_project_admin": False,
        "memberships": [
            ("Leadership", TeamRole.team_lead),
        ],
    },
    {
        "name": "Tara Kapoor",
        "email": "tara.kapoor@northstarcommerce.com",
        "is_org_admin": False,
        "is_project_admin": False,
        "memberships": [
            ("Product", TeamRole.contributor),
        ],
    },
    {
        "name": "Nikhil Joshi",
        "email": "nikhil.joshi@northstarcommerce.com",
        "is_org_admin": False,
        "is_project_admin": False,
        "memberships": [
            ("QA", TeamRole.contributor),
        ],
    },
    {
        "name": "Samar Gupta",
        "email": "samar.gupta@northstarcommerce.com",
        "is_org_admin": True,
        "is_project_admin": False,
        "memberships": [],
    },
]

DOCUMENTS = [
    {
        "filename": "01-holiday-checkout-business-requirements.md",
        "stage": "Discovery",
        "team": "Product",
        "sensitivity": "internal",
        "author_email": "tara.kapoor@northstarcommerce.com",
        "upload_date": "2026-08-18T09:42:00Z",
        "action": "finalize",
    },
    {
        "filename": "02-checkout-experience-design-spec.md",
        "stage": "UX Design",
        "team": "Product",
        "sensitivity": "internal",
        "author_email": "tara.kapoor@northstarcommerce.com",
        "upload_date": "2026-08-24T14:17:00Z",
        "action": "finalize",
    },
    {
        "filename": "03-holiday-commercial-pricing-strategy.md",
        "stage": "Discovery",
        "team": "Leadership",
        "sensitivity": "confidential",
        "author_email": "dev.malhotra@northstarcommerce.com",
        "upload_date": "2026-08-27T11:08:00Z",
        "action": "finalize",
    },
    {
        "filename": "04-checkout-payment-service-design.md",
        "stage": "Engineering",
        "team": "Engineering",
        "sensitivity": "internal",
        "author_email": "kabir.singh@northstarcommerce.com",
        "upload_date": "2026-09-02T16:26:00Z",
        "action": "finalize",
    },
    {
        "filename": "05-checkout-validation-plan.md",
        "stage": "Validation",
        "team": "QA",
        "sensitivity": "internal",
        "author_email": "nikhil.joshi@northstarcommerce.com",
        "upload_date": "2026-09-06T10:34:00Z",
        "action": "finalize_submit_approve",
        "approver_email": "ishita.malhotra@northstarcommerce.com",
    },
    {
        "filename": "06-payment-failover-validation-results.md",
        "stage": "Validation",
        "team": "Engineering",
        "sensitivity": "internal",
        "author_email": "kabir.singh@northstarcommerce.com",
        "upload_date": "2026-09-10T15:51:00Z",
        "action": "finalize_submit",  # Ends in pending_review, unapproved
    },
    {
        "filename": "07-holiday-launch-readiness-checklist.md",
        "stage": "Launch",
        "team": "Engineering",
        "sensitivity": "internal",
        "author_email": "ishita.malhotra@northstarcommerce.com",  # Project admin -> auto-approved
        "upload_date": "2026-09-11T17:12:00Z",
        "action": "finalize",
    },
]


def find_docs_dir() -> Path:
    """Finds the directory containing the demo2 markdown files."""
    candidates = [
        Path("docs for demo2"),
        Path("../docs for demo2"),
        Path(__file__).resolve().parent.parent.parent / "docs for demo2",
        Path("d:/Ra/DocFlowAI/docs for demo2"),
    ]
    for c in candidates:
        if c.exists() and (c / "01-holiday-checkout-business-requirements.md").exists():
            return c.resolve()
    raise FileNotFoundError("Could not find 'docs for demo2' directory with seed files.")


# ---------------------------------------------------------------------------
# Strict Tenant Identity Resolution
# ---------------------------------------------------------------------------

def get_or_create_tenant(db: Session) -> Tenant:
    """
    Resolves Northstar tenant strictly by exact (UUID, name) pair.
    - UUID exists + correct name -> reuse
    - UUID exists + wrong name -> FAIL
    - name exists under another UUID -> FAIL
    - neither exists -> create
    """
    by_id = db.get(Tenant, NORTHSTAR_TENANT_ID)
    by_name = db.execute(select(Tenant).where(Tenant.name == COMPANY_NAME)).scalar_one_or_none()

    if by_id and by_name:
        if by_id.tenant_id != by_name.tenant_id:
            raise RuntimeError(
                f"Tenant identity conflict: ID {NORTHSTAR_TENANT_ID} has name '{by_id.name}', "
                f"but name '{COMPANY_NAME}' has ID {by_name.tenant_id}."
            )
        print(f"  [=] Validated existing Northstar tenant: {by_id.name} ({by_id.tenant_id})")
        return by_id
    elif by_id and not by_name:
        raise RuntimeError(
            f"Tenant identity mismatch: ID {NORTHSTAR_TENANT_ID} exists with name '{by_id.name}', "
            f"expected '{COMPANY_NAME}'."
        )
    elif by_name and not by_id:
        raise RuntimeError(
            f"Tenant identity mismatch: Name '{COMPANY_NAME}' exists under foreign ID {by_name.tenant_id}, "
            f"expected '{NORTHSTAR_TENANT_ID}'."
        )
    else:
        tenant = Tenant(tenant_id=NORTHSTAR_TENANT_ID, name=COMPANY_NAME)
        db.add(tenant)
        db.flush()
        print(f"  [+] Created Northstar Tenant: {COMPANY_NAME} ({tenant.tenant_id})")
        return tenant


# ---------------------------------------------------------------------------
# Reset Logic (Strictly Scoped & Guarded)
# ---------------------------------------------------------------------------

def reset_northstar_data(db: Session):
    """
    Wipes all demo data belonging to Northstar Commerce.
    Guard: Proves both tenant_id == NORTHSTAR_TENANT_ID AND name == 'Northstar Commerce'.
    Never uses an OR condition.
    """
    print(f"\n[*] Guarded reset check for '{COMPANY_NAME}' ({NORTHSTAR_TENANT_ID})...")

    tenant = db.get(Tenant, NORTHSTAR_TENANT_ID)
    if not tenant:
        print(f"  [-] No tenant found with ID {NORTHSTAR_TENANT_ID}. Nothing to reset.")
        return

    if tenant.name != COMPANY_NAME:
        raise RuntimeError(
            f"ABORT RESET: Tenant with ID {NORTHSTAR_TENANT_ID} has name '{tenant.name}', "
            f"expected exactly '{COMPANY_NAME}'. Aborting to prevent data corruption."
        )

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

    # 2. Database cascading deletion scoped strictly to Northstar tenant
    projects = db.execute(select(Project).where(Project.tenant_id == tenant_id)).scalars().all()
    project_ids = [p.project_id for p in projects]

    if project_ids:
        db.query(Claim).filter(Claim.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(StageMetricSnapshot).filter(StageMetricSnapshot.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(ProjectMetricSnapshot).filter(ProjectMetricSnapshot.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(AuditFinding).filter(AuditFinding.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(AuditRun).filter(AuditRun.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(ExtractionRun).filter(ExtractionRun.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(Edge).filter(Edge.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(Node).filter(Node.project_id.in_(project_ids)).delete(synchronize_session=False)

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
    print("  [+] Northstar guarded reset complete.")


# ---------------------------------------------------------------------------
# Seeding Logic (Validate & Preserve)
# ---------------------------------------------------------------------------

def seed_structure_and_users(db: Session) -> tuple[Tenant, Project, dict[str, Team], dict[str, Stage], dict[str, User]]:
    """Seeds tenant, project, teams, dynamic stages, and users for Northstar Commerce."""
    print("\n[*] Seeding Organization, Project, Teams & Stages...")

    # 1. Tenant
    tenant = get_or_create_tenant(db)

    # 2. Project
    project = db.execute(
        select(Project).where(Project.tenant_id == tenant.tenant_id, Project.name == PROJECT_NAME)
    ).scalar_one_or_none()
    if not project:
        project = Project(
            tenant_id=tenant.tenant_id,
            name=PROJECT_NAME,
        )
        db.add(project)
        db.flush()
        print(f"  [+] Created Project: {PROJECT_NAME} ({project.project_id})")
    else:
        print(f"  [=] Validated existing Project: {PROJECT_NAME} ({project.project_id})")

    # 3. Teams
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

    # 4. Stages
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
                raise RuntimeError(
                    f"Stage mismatch for '{stage.name}': requires_approval={stage.requires_approval} (expected {stg['requires_approval']}), "
                    f"order_index={stage.order_index} (expected {stg['order_index']})"
                )
        stages[stg["name"]] = stage

    # 5. TeamStageAccess grants
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

    # 6. Users & Memberships (Strict Tenant Scoping)
    print("\n[*] Seeding Users and Roles...")
    users: dict[str, User] = {}
    pw_hash = hash_password(DEFAULT_PASSWORD)

    for u_info in USERS:
        user = db.execute(select(User).where(User.email == u_info["email"])).scalar_one_or_none()
        if user:
            # Must belong strictly to Northstar tenant
            if user.tenant_id != tenant.tenant_id:
                raise RuntimeError(
                    f"User identity conflict: User '{u_info['email']}' belongs to foreign tenant {user.tenant_id}, "
                    f"expected Northstar tenant {tenant.tenant_id}."
                )
            # Validate user details match manifest
            if user.full_name != u_info["name"]:
                raise RuntimeError(
                    f"User validation mismatch for '{u_info['email']}': full_name is '{user.full_name}', expected '{u_info['name']}'."
                )
            if user.is_org_admin != u_info["is_org_admin"]:
                raise RuntimeError(
                    f"User validation mismatch for '{u_info['email']}': is_org_admin is '{user.is_org_admin}', expected '{u_info['is_org_admin']}'."
                )
            print(f"  [=] Validated existing User: {u_info['name']} <{u_info['email']}>")
        else:
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
            else:
                if mem.role != role:
                    raise RuntimeError(
                        f"Membership role mismatch for {u_info['name']} in {t_name}: {mem.role.value} != {role.value}"
                    )

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
    Ingests all 7 demo documents through authentic API endpoints.
    Includes rate-limit avoidance pauses between uploads.
    """
    print(f"\n[*] Ingesting 7 demo documents from '{docs_dir}'...")

    for i, d_spec in enumerate(DOCUMENTS):
        fname = d_spec["filename"]
        stage = stages[d_spec["stage"]]
        team = teams[d_spec["team"]]
        author = users[d_spec["author_email"]]
        fpath = docs_dir / fname

        if not fpath.exists():
            raise FileNotFoundError(f"Missing required document file: {fpath}")

        # Check if already seeded -> validate and preserve
        existing_doc = db.execute(
            select(Document).where(
                Document.project_id == project.project_id,
                Document.original_filename == fname,
            )
        ).scalar_one_or_none()

        if existing_doc:
            # Validate existing document state against manifest
            if existing_doc.stage_id != stage.stage_id:
                raise RuntimeError(f"Existing document '{fname}' has stage_id {existing_doc.stage_id}, expected {stage.stage_id}")
            if existing_doc.uploaded_as_team_id != team.team_id:
                raise RuntimeError(f"Existing document '{fname}' has uploaded_as_team_id {existing_doc.uploaded_as_team_id}, expected {team.team_id}")
            if existing_doc.sensitivity_level.name != d_spec["sensitivity"]:
                raise RuntimeError(f"Existing document '{fname}' has sensitivity {existing_doc.sensitivity_level.name}, expected {d_spec['sensitivity']}")
            print(f"  [=] Validated existing document: '{fname}' ({existing_doc.document_id})")
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
            # Document 6: Kabir Singh submits for review (Validation stage)
            print("      Submitting for review (Validation stage)...")
            sub_res = client.post(
                f"/documents/{doc_id}/submit",
                headers=auth_headers,
            )
            if sub_res.status_code != 200:
                raise RuntimeError(f"Failed to submit '{fname}': {sub_res.status_code} - {sub_res.text}")
            print("      Submitted: awaiting approval by Engineering team_lead (Ananya Mehta). NOT APPROVED.")

        elif d_spec["action"] == "finalize_submit_approve":
            # Document 5: Nikhil Joshi submits, Ishita Malhotra approves
            print("      Submitting for review (Validation stage)...")
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

        # Rate-limiting mitigation: pause briefly between LLM calls
        if i < len(DOCUMENTS) - 1:
            time.sleep(2.0)

    print("\n[+] All 7 documents successfully ingested and processed.")


def run_graph_and_audit(db: Session, project: Project, tenant: Tenant):
    """
    Synchronizes the knowledge graph, extracts cross-document relationships,
    extracts factual claims, detects contradictions, runs project audit,
    and captures project/stage metrics snapshots.
    """
    tenant_id = tenant.tenant_id
    project_id = project.project_id
    print(f"\n[*] Running Graph Synchronization for project '{project.name}' ({project_id})...")

    # 1. Base Graph Synchronization
    sync_res = sync_project_graph(db, project_id)
    print(f"  [+] Nodes synced: {sync_res.nodes_synced}, Edges synced: {sync_res.edges_synced}")
    if sync_res.errors:
        print(f"  [!] Sync warnings/errors: {sync_res.errors}")
    db.commit()

    # 2. Relationship and Claims Extraction
    docs = db.query(Document).filter(Document.project_id == project_id).all()
    for doc in docs:
        if not doc.current_version_id:
            continue
        ver = db.get(DocumentVersion, doc.current_version_id)
        if not ver or not ver.file_data:
            continue
        try:
            content_str = ver.file_data.decode("utf-8")
        except UnicodeDecodeError:
            content_str = ver.file_data.decode("latin1", errors="ignore")

        # Semantic relationships
        rel_res = extract_document_relationships(
            db=db,
            document_id=doc.document_id,
            version_id=ver.version_id,
            content=content_str,
        )
        print(f"  [+] Document '{doc.original_filename}': {rel_res.edges_created} relationships extracted")

        # Factual claims
        claims_data = extract_claims_from_text(
            text=content_str,
            tenant_id=tenant_id,
            project_id=project_id,
            document_id=doc.document_id,
            version_id=ver.version_id,
        )
        if claims_data:
            persisted = persist_claims(
                db,
                tenant_id,
                project_id,
                doc.document_id,
                ver.version_id,
                claims_data,
            )
            print(f"  [+] Document '{doc.original_filename}': {len(persisted)} factual claims persisted")
        db.commit()

    # 3. Contradiction Detection
    contradictions = detect_project_contradictions(db, tenant_id, project_id)
    print(f"  [+] Project contradictions detected: {len(contradictions)}")
    db.commit()

    # 4. Project Audit Execution
    print("\n[*] Executing Project Audit...")
    audit_run = execute_project_audit(db, project_id)
    blockers = sum(1 for f in audit_run.findings if f.is_blocker)
    print(f"  [+] Audit Run ID: {audit_run.run_id}")
    print(f"  [+] Readiness Status: {audit_run.readiness_status}")
    print(f"  [+] Blockers Count: {blockers}")
    print(f"  [+] Total Findings: {len(audit_run.findings)}")
    db.commit()

    # 5. Project Health & Metric Snapshots
    print("\n[*] Capturing Project Health & Snapshots...")
    health = get_latest_project_health(db, project_id)
    print(f"  [+] Project Readiness: {health.get('project_metric', {}).get('readiness_status')}")
    print(f"  [+] Completeness Score: {health.get('project_metric', {}).get('completeness_score')}%")
    db.commit()


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="DocFlow AI Northstar Commerce Demo Seed Script")
    parser.add_argument("--reset", action="store_true", help="Wipe Northstar Commerce demo data before seeding")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        if args.reset:
            reset_northstar_data(db)

        # Seed data
        docs_dir = find_docs_dir()
        tenant, project, teams, stages, users = seed_structure_and_users(db)

        client = TestClient(app)
        seed_documents(db, client, docs_dir, project, teams, stages, users)
    finally:
        db.close()

    # Run graph sync, extraction, audit, metrics with a fresh connection
    db = SessionLocal()
    try:
        tenant = get_or_create_tenant(db)
        project = db.execute(
            select(Project).where(Project.tenant_id == tenant.tenant_id, Project.name == PROJECT_NAME)
        ).scalar_one()
        run_graph_and_audit(db, project, tenant)
        print("\n[+] Northstar Commerce seed completed successfully.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
