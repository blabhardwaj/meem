"""
DocFlow AI — Meridian Pay Demo Seed Script

Populates a brand-new tenant/organization ("Meridian Pay", a B2B payments
platform) with real human-named users, teams, roles, two projects (an
8-stage "Merchant Instant Payouts Launch" and a 3-stage "Dispute
Auto-Tagging Sprint"), per-stage required-document checklists, and real
documents (uploaded through the actual upload+scan+finalize pipeline,
same as production traffic) spanning public/internal/confidential
sensitivity levels. A few stages are deliberately left with an
outstanding required document that was never uploaded, so gap-detection
has something real to show.

Usage:
    python -m seed.seed_meridianpay            # Seed (idempotent: validate & preserve)
    python -m seed.seed_meridianpay --reset    # Wipe Meridian Pay demo data and re-seed
"""

import argparse
import logging
import time
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models.tenant import Tenant
from app.models.project import Project
from app.models.team import Team, TeamRole, UserTeamMembership, ProjectAdmin
from app.models.stage import Stage, StageReference, TeamStageAccess
from app.models.required_document import RequiredDocument, RequirementSource
from app.models.user import User
from app.models.notification import Notification
from app.models.audit import AuditLog
from app.models.document import (
    Document,
    DocumentVersion,
    DocumentScan,
    DocumentTeamVisibility,
    SensitivityLevel,
)
from app.models.workflow import WorkflowState, WorkflowStatus
from app.models.graph import Node, Edge, ExtractionRun, Claim, AuditRun, AuditFinding, ProjectMetricSnapshot, StageMetricSnapshot
from app.services.auth import hash_password
from app.services.document_upload_review import upload_and_scan
from app.services.document_finalize import finalize_document_revision
from app.services.workflow import submit_for_review, approve_document
from app.services.rag.collection_setup import get_qdrant_client, collection_name_for_tenant
from app.services.graph.sync import sync_project_graph
from app.services.graph.relationship_extractor import extract_document_relationships
from app.services.graph.claims_analyzer import (
    extract_claims_from_text,
    persist_claims,
    detect_project_contradictions,
)
from app.services.graph.audit_engine import execute_project_audit

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_meridianpay")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MERIDIANPAY_TENANT_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
COMPANY_NAME = "Meridian Pay"
DEFAULT_PASSWORD = "MeridianDemo123!"

DOCS_DIR_NAME = "docs for meridian pay"

TEAMS = ["Engineering", "Product", "Security", "QA", "Leadership"]

USERS = [
    {
        "name": "Rohan Desai",
        "email": "rohan.desai@meridianpay.com",
        "is_org_admin": True,
        "memberships": [],  # org_admin bypasses team-scoped checks entirely
    },
    {
        "name": "Priya Nair",
        "email": "priya.nair@meridianpay.com",
        "is_org_admin": False,
        "memberships": [("Product", TeamRole.team_lead)],
    },
    {
        "name": "Meera Iyer",
        "email": "meera.iyer@meridianpay.com",
        "is_org_admin": False,
        "memberships": [("Leadership", TeamRole.contributor)],
    },
    {
        "name": "Arjun Rao",
        "email": "arjun.rao@meridianpay.com",
        "is_org_admin": False,
        "memberships": [("Engineering", TeamRole.team_lead)],
    },
    {
        "name": "Kavya Menon",
        "email": "kavya.menon@meridianpay.com",
        "is_org_admin": False,
        "memberships": [("Security", TeamRole.team_lead)],
    },
    {
        "name": "Fatima Sheikh",
        "email": "fatima.sheikh@meridianpay.com",
        "is_org_admin": False,
        "memberships": [("QA", TeamRole.team_lead)],
    },
    {
        "name": "Ishaan Verma",
        "email": "ishaan.verma@meridianpay.com",
        "is_org_admin": False,
        "memberships": [("Engineering", TeamRole.contributor)],
    },
    {
        "name": "Ananya Bhatt",
        "email": "ananya.bhatt@meridianpay.com",
        "is_org_admin": False,
        # Project admin on the big project only (assigned below via PROJECT_ADMINS)
        "memberships": [("Leadership", TeamRole.team_lead)],
    },
]

# email -> list of project names this user is project_admin on
PROJECT_ADMINS: dict[str, list[str]] = {
    "ananya.bhatt@meridianpay.com": ["Merchant Instant Payouts Launch"],
}

# ---------------------------------------------------------------------------
# Project 1: Merchant Instant Payouts Launch (8 stages)
# ---------------------------------------------------------------------------

PROJECT_1_NAME = "Merchant Instant Payouts Launch"
PROJECT_1_DESCRIPTION = (
    "Launching an opt-in Instant Payouts feature: eligible merchants can "
    "push balances to a linked debit card within 30 minutes for a "
    "per-transaction fee, alongside the existing standard T+2 payout "
    "schedule. Full lifecycle from business case through GA launch."
)

PROJECT_1_STAGES = [
    {"name": "Discovery", "order_index": 1, "requires_approval": False},
    {"name": "Requirements", "order_index": 2, "requires_approval": False},
    {"name": "UX & API Design", "order_index": 3, "requires_approval": False},
    {"name": "Architecture", "order_index": 4, "requires_approval": False},
    {"name": "Engineering", "order_index": 5, "requires_approval": False},
    {"name": "Security Review", "order_index": 6, "requires_approval": True},
    {"name": "QA & Validation", "order_index": 7, "requires_approval": True},
    {"name": "Launch", "order_index": 8, "requires_approval": True},
]

PROJECT_1_STAGE_ACCESS = {
    "Discovery": ["Product", "Leadership", "Engineering"],
    "Requirements": ["Product", "Leadership", "Engineering"],
    "UX & API Design": ["Product", "Engineering"],
    "Architecture": ["Engineering", "Security"],
    "Engineering": ["Engineering", "Product"],
    "Security Review": ["Security", "Engineering", "Leadership"],
    "QA & Validation": ["QA", "Engineering", "Product"],
    "Launch": ["Leadership", "Product", "Engineering", "Security", "QA"],
}

PROJECT_1_STAGE_REFERENCES = {
    "Requirements": ["Discovery"],
    "UX & API Design": ["Discovery", "Requirements"],
    "Architecture": ["Requirements", "UX & API Design"],
    "Engineering": ["Architecture"],
    "Security Review": ["Architecture", "Engineering"],
    "QA & Validation": ["Requirements", "Engineering", "Security Review"],
    "Launch": ["Security Review", "QA & Validation"],
}

# One or two required documents per stage. `filename=None` marks a
# requirement that is deliberately left unsatisfied (no document ever
# uploaded against it) so gap-detection/coverage math has something real
# to report, per the seeding brief's "a few left upload" instruction.
PROJECT_1_REQUIRED_DOCS = [
    {"stage": "Discovery", "name": "Business Case", "is_mandatory": True, "filename": "01-instant-payouts-business-case.md",
     "description": "Justifies WHY this project should be built at all — problem statement, target users, expected business value. Written before requirements or design exist."},
    {"stage": "Requirements", "name": "Product Requirements Document", "is_mandatory": True, "filename": "02-instant-payouts-prd.md",
     "description": "Defines WHAT the feature must do — functional requirements, user flows, acceptance criteria. Does not cover pricing/financials or technical implementation."},
    {"stage": "Requirements", "name": "Revenue Model", "is_mandatory": True, "filename": "03-instant-payouts-revenue-model.md",
     "description": "Pricing, unit economics, and revenue projections specifically — cost basis, margin targets, volume forecasts. Not the functional PRD."},
    {"stage": "UX & API Design", "name": "API Specification", "is_mandatory": True, "filename": "04-instant-payouts-api-spec.md",
     "description": "Defines the API contract — endpoints, request/response shapes, error codes. Not the user-facing UX flow."},
    {"stage": "UX & API Design", "name": "UX Flows", "is_mandatory": True, "filename": "05-instant-payouts-ux-flows.md",
     "description": "Defines the user-facing screens and interaction flow. Not the API contract."},
    {"stage": "Architecture", "name": "System Architecture", "is_mandatory": True, "filename": "06-instant-payouts-architecture.md",
     "description": "Describes the system's technical design — components, data flow, integration points. Not a security/threat analysis."},
    {"stage": "Architecture", "name": "Threat Model", "is_mandatory": True, "filename": "07-instant-payouts-threat-model.md",
     "description": "Identifies specific security threats and mitigations for this system. Not the general system architecture."},
    {"stage": "Engineering", "name": "Implementation Notes", "is_mandatory": True, "filename": None,
     "description": "Engineering's own notes on how the feature was actually built, deviations from the design docs, and known technical debt."},
    {"stage": "Security Review", "name": "Penetration Test Findings", "is_mandatory": True, "filename": "08-instant-payouts-pentest-findings.md",
     "description": "Results of an ACTIVE penetration test against the running system — specific vulnerabilities found, severity, remediation status. Not a checklist of compliance controls."},
    {"stage": "Security Review", "name": "Compliance Checklist", "is_mandatory": True, "filename": "09-instant-payouts-compliance-checklist.md",
     "description": "A checklist of regulatory/compliance controls and their status. Not penetration test results."},
    {"stage": "QA & Validation", "name": "Test Plan", "is_mandatory": True, "filename": "10-instant-payouts-test-plan.md",
     "description": "The document that DEFINES test cases and records their pass/fail RESULT at the time each case was run — written DURING testing, by QA. Its own purpose is to enumerate test cases and results, not to assess overall launch readiness. Distinct from Validation Results, which is written AFTER testing to summarize load/soak testing and give the final go/no-go assessment."},
    {"stage": "QA & Validation", "name": "Validation Results", "is_mandatory": True, "filename": "11-instant-payouts-validation-results.md",
     "description": "The document that SUMMARIZES the outcome of the whole validation stage — load testing, soak testing, and a final go/no-go readiness assessment — written AFTER the Test Plan's test cases have already been run. Its own purpose is the launch-readiness summary and recommendation, not enumerating individual test cases (that's the Test Plan's job)."},
    {"stage": "Launch", "name": "Launch Readiness Checklist", "is_mandatory": True, "filename": "12-instant-payouts-launch-readiness.md",
     "description": "Final go/no-go checklist confirming every prior stage's exit criteria are met, immediately before launch."},
    {"stage": "Launch", "name": "Public Release Notes", "is_mandatory": False, "filename": "13-instant-payouts-release-notes.md",
     "description": "Customer-facing announcement of the new feature. Not an internal readiness document."},
]

# action: "auto" (non-approval stage, upload+finalize only, no explicit
# submit/approve calls needed since finalize promotes it directly) or
# "submit_approve" (approval-required stage: upload, finalize, submit,
# approve) or "submit_only" (uploaded+submitted but left un-approved, to
# show a genuinely pending item).
PROJECT_1_DOCS = [
    {"filename": "01-instant-payouts-business-case.md", "stage": "Discovery", "team": "Product", "sensitivity": "internal", "author_email": "priya.nair@meridianpay.com", "action": "auto", "upload_date": "2026-07-02T14:10:00Z"},
    {"filename": "02-instant-payouts-prd.md", "stage": "Requirements", "team": "Product", "sensitivity": "internal", "author_email": "priya.nair@meridianpay.com", "action": "auto", "upload_date": "2026-07-09T10:20:00Z"},
    {"filename": "03-instant-payouts-revenue-model.md", "stage": "Requirements", "team": "Leadership", "sensitivity": "confidential", "author_email": "meera.iyer@meridianpay.com", "action": "auto", "upload_date": "2026-07-11T16:45:00Z"},
    {"filename": "04-instant-payouts-api-spec.md", "stage": "UX & API Design", "team": "Product", "sensitivity": "internal", "author_email": "priya.nair@meridianpay.com", "action": "auto", "upload_date": "2026-07-13T09:05:00Z"},
    {"filename": "05-instant-payouts-ux-flows.md", "stage": "UX & API Design", "team": "Product", "sensitivity": "internal", "author_email": "priya.nair@meridianpay.com", "action": "auto", "upload_date": "2026-07-15T11:30:00Z"},
    {"filename": "06-instant-payouts-architecture.md", "stage": "Architecture", "team": "Engineering", "sensitivity": "internal", "author_email": "arjun.rao@meridianpay.com", "action": "auto", "upload_date": "2026-07-22T13:50:00Z"},
    {"filename": "07-instant-payouts-threat-model.md", "stage": "Architecture", "team": "Security", "sensitivity": "confidential", "author_email": "kavya.menon@meridianpay.com", "action": "auto", "upload_date": "2026-08-01T15:15:00Z"},
    # Engineering stage: deliberately no document uploaded at all.
    {"filename": "08-instant-payouts-pentest-findings.md", "stage": "Security Review", "team": "Security", "sensitivity": "confidential", "author_email": "kavya.menon@meridianpay.com", "action": "submit_approve", "approver_email": "kavya.menon@meridianpay.com", "upload_date": "2026-08-09T09:30:00Z"},
    {"filename": "09-instant-payouts-compliance-checklist.md", "stage": "Security Review", "team": "Security", "sensitivity": "internal", "author_email": "kavya.menon@meridianpay.com", "action": "submit_approve", "approver_email": "kavya.menon@meridianpay.com", "upload_date": "2026-08-09T09:40:00Z"},
    {"filename": "10-instant-payouts-test-plan.md", "stage": "QA & Validation", "team": "QA", "sensitivity": "internal", "author_email": "fatima.sheikh@meridianpay.com", "action": "submit_only", "upload_date": "2026-09-11T10:00:00Z"},
    {"filename": "11-instant-payouts-validation-results.md", "stage": "QA & Validation", "team": "QA", "sensitivity": "internal", "author_email": "fatima.sheikh@meridianpay.com", "action": "submit_approve", "approver_email": "fatima.sheikh@meridianpay.com", "upload_date": "2026-09-12T14:20:00Z"},
    {"filename": "12-instant-payouts-launch-readiness.md", "stage": "Launch", "team": "Leadership", "sensitivity": "internal", "author_email": "ananya.bhatt@meridianpay.com", "action": "auto", "upload_date": "2026-09-16T11:00:00Z"},
    {"filename": "13-instant-payouts-release-notes.md", "stage": "Launch", "team": "Product", "sensitivity": "public", "author_email": "priya.nair@meridianpay.com", "action": "submit_approve", "approver_email": "ananya.bhatt@meridianpay.com", "upload_date": "2026-09-16T11:30:00Z"},
]

# ---------------------------------------------------------------------------
# Project 2: Dispute Auto-Tagging Sprint (3 stages)
# ---------------------------------------------------------------------------

PROJECT_2_NAME = "Dispute Auto-Tagging Sprint"
PROJECT_2_DESCRIPTION = (
    "Two-week sprint to add a classifier that pre-fills the likely dispute "
    "category at intake, so support agents spend their time verifying and "
    "resolving instead of manually categorizing every incoming chargeback."
)

PROJECT_2_STAGES = [
    {"name": "Sprint Planning", "order_index": 1, "requires_approval": False},
    {"name": "Build", "order_index": 2, "requires_approval": False},
    {"name": "Review & Ship", "order_index": 3, "requires_approval": True},
]

PROJECT_2_STAGE_ACCESS = {
    "Sprint Planning": ["Product", "Engineering", "QA"],
    "Build": ["Engineering"],
    "Review & Ship": ["QA", "Product", "Engineering"],
}

PROJECT_2_STAGE_REFERENCES = {
    "Build": ["Sprint Planning"],
    "Review & Ship": ["Sprint Planning", "Build"],
}

PROJECT_2_REQUIRED_DOCS = [
    {"stage": "Sprint Planning", "name": "Sprint Brief", "is_mandatory": True, "filename": "14-dispute-autotag-sprint-brief.md",
     "description": "Scopes the sprint before any work starts — goal, what's in/out of scope, target acceptance rate."},
    {"stage": "Build", "name": "Implementation Notes", "is_mandatory": True, "filename": None,
     "description": "Engineering's own notes on how the classifier was actually implemented."},
    {"stage": "Review & Ship", "name": "Test Results", "is_mandatory": True, "filename": "15-dispute-autotag-test-results.md",
     "description": "Classifier accuracy measured against a labeled historical sample, with a ship/no-ship recommendation. Written after the Sprint Brief, before shipping."},
    {"stage": "Review & Ship", "name": "Changelog", "is_mandatory": False, "filename": "16-dispute-autotag-changelog.md",
     "description": "User-facing summary of what changed for support agents. Not the accuracy test results."},
]

PROJECT_2_DOCS = [
    {"filename": "14-dispute-autotag-sprint-brief.md", "stage": "Sprint Planning", "team": "Engineering", "sensitivity": "internal", "author_email": "ishaan.verma@meridianpay.com", "action": "auto", "upload_date": "2026-09-08T09:00:00Z"},
    # Build stage: deliberately no document uploaded at all.
    {"filename": "15-dispute-autotag-test-results.md", "stage": "Review & Ship", "team": "QA", "sensitivity": "internal", "author_email": "fatima.sheikh@meridianpay.com", "action": "submit_approve", "approver_email": "fatima.sheikh@meridianpay.com", "upload_date": "2026-09-19T10:15:00Z"},
    {"filename": "16-dispute-autotag-changelog.md", "stage": "Review & Ship", "team": "Product", "sensitivity": "public", "author_email": "priya.nair@meridianpay.com", "action": "submit_approve", "approver_email": "priya.nair@meridianpay.com", "upload_date": "2026-09-19T10:30:00Z"},
]

SENSITIVITY_MAP = {
    "public": SensitivityLevel.public,
    "internal": SensitivityLevel.internal,
    "confidential": SensitivityLevel.confidential,
}


def find_docs_dir() -> Path:
    candidates = [
        Path(DOCS_DIR_NAME),
        Path("..") / DOCS_DIR_NAME,
        Path(__file__).resolve().parent.parent.parent / DOCS_DIR_NAME,
        Path("d:/Ra/DocFlowAI") / DOCS_DIR_NAME,
    ]
    for c in candidates:
        if c.exists() and (c / "01-instant-payouts-business-case.md").exists():
            return c.resolve()
    raise FileNotFoundError(f"Could not find '{DOCS_DIR_NAME}' directory with seed files.")


# ---------------------------------------------------------------------------
# Tenant resolution (same guarded pattern as seed_northstar.py)
# ---------------------------------------------------------------------------

def get_or_create_tenant(db: Session) -> Tenant:
    by_id = db.get(Tenant, MERIDIANPAY_TENANT_ID)
    by_name = db.execute(select(Tenant).where(Tenant.name == COMPANY_NAME)).scalar_one_or_none()

    if by_id and by_name:
        if by_id.tenant_id != by_name.tenant_id:
            raise RuntimeError(
                f"Tenant identity conflict: ID {MERIDIANPAY_TENANT_ID} has name '{by_id.name}', "
                f"but name '{COMPANY_NAME}' has ID {by_name.tenant_id}."
            )
        print(f"  [=] Validated existing tenant: {by_id.name} ({by_id.tenant_id})")
        return by_id
    elif by_id and not by_name:
        raise RuntimeError(
            f"Tenant identity mismatch: ID {MERIDIANPAY_TENANT_ID} exists with name '{by_id.name}', "
            f"expected '{COMPANY_NAME}'."
        )
    elif by_name and not by_id:
        raise RuntimeError(
            f"Tenant identity mismatch: Name '{COMPANY_NAME}' exists under foreign ID {by_name.tenant_id}, "
            f"expected '{MERIDIANPAY_TENANT_ID}'."
        )
    else:
        tenant = Tenant(tenant_id=MERIDIANPAY_TENANT_ID, name=COMPANY_NAME)
        db.add(tenant)
        db.flush()
        print(f"  [+] Created tenant: {COMPANY_NAME} ({tenant.tenant_id})")
        return tenant


# ---------------------------------------------------------------------------
# Reset (guarded, strictly scoped to this tenant)
# ---------------------------------------------------------------------------

def reset_meridianpay_data(db: Session):
    print(f"\n[*] Guarded reset check for '{COMPANY_NAME}' ({MERIDIANPAY_TENANT_ID})...")
    tenant = db.get(Tenant, MERIDIANPAY_TENANT_ID)
    if not tenant:
        print(f"  [-] No tenant found with ID {MERIDIANPAY_TENANT_ID}. Nothing to reset.")
        return
    if tenant.name != COMPANY_NAME:
        raise RuntimeError(
            f"ABORT RESET: Tenant with ID {MERIDIANPAY_TENANT_ID} has name '{tenant.name}', "
            f"expected exactly '{COMPANY_NAME}'. Aborting to prevent data corruption."
        )
    tenant_id = tenant.tenant_id

    try:
        q_client = get_qdrant_client()
        col_name = collection_name_for_tenant(tenant_id)
        if q_client.collection_exists(col_name):
            print(f"  [-] Deleting Qdrant collection: {col_name}")
            q_client.delete_collection(col_name)
    except Exception as exc:
        print(f"  [!] Note: Qdrant cleanup notice: {exc}")

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
        db.query(Notification).filter(Notification.project_id.in_(project_ids)).delete(synchronize_session=False)

    docs = db.execute(select(Document).where(Document.tenant_id == tenant_id)).scalars().all()
    doc_ids = [d.document_id for d in docs]
    if doc_ids:
        versions = db.execute(select(DocumentVersion).where(DocumentVersion.document_id.in_(doc_ids))).scalars().all()
        version_ids = [v.version_id for v in versions]
        db.query(Document).filter(Document.document_id.in_(doc_ids)).update({"current_version_id": None}, synchronize_session=False)
        db.flush()
        if version_ids:
            db.query(DocumentScan).filter(DocumentScan.version_id.in_(version_ids)).delete(synchronize_session=False)
        db.query(WorkflowState).filter(WorkflowState.document_id.in_(doc_ids)).delete(synchronize_session=False)
        db.query(DocumentTeamVisibility).filter(DocumentTeamVisibility.document_id.in_(doc_ids)).delete(synchronize_session=False)
        db.query(DocumentVersion).filter(DocumentVersion.document_id.in_(doc_ids)).delete(synchronize_session=False)
        db.query(Document).filter(Document.document_id.in_(doc_ids)).delete(synchronize_session=False)

    if project_ids:
        db.query(RequiredDocument).filter(
            RequiredDocument.stage_id.in_(select(Stage.stage_id).where(Stage.project_id.in_(project_ids)))
        ).delete(synchronize_session=False)
        db.query(TeamStageAccess).filter(
            TeamStageAccess.stage_id.in_(select(Stage.stage_id).where(Stage.project_id.in_(project_ids)))
        ).delete(synchronize_session=False)
        db.query(StageReference).filter(
            StageReference.stage_id.in_(select(Stage.stage_id).where(Stage.project_id.in_(project_ids)))
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
    print("  [+] Meridian Pay guarded reset complete.")


# ---------------------------------------------------------------------------
# Users (shared across both projects)
# ---------------------------------------------------------------------------

def seed_users(db: Session, tenant: Tenant) -> dict[str, User]:
    print("\n[*] Seeding Users...")
    users: dict[str, User] = {}
    pw_hash = hash_password(DEFAULT_PASSWORD)

    for u_info in USERS:
        user = db.execute(select(User).where(User.email == u_info["email"])).scalar_one_or_none()
        if user:
            if user.tenant_id != tenant.tenant_id:
                raise RuntimeError(
                    f"User identity conflict: '{u_info['email']}' belongs to foreign tenant {user.tenant_id}."
                )
            print(f"  [=] Validated existing user: {u_info['name']} <{u_info['email']}>")
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
            print(f"  [+] Created user: {u_info['name']} <{u_info['email']}>")
        users[u_info["email"]] = user

    db.commit()
    return users


# ---------------------------------------------------------------------------
# Project structure (teams/stages/access/references/required-docs/roles)
# ---------------------------------------------------------------------------

def seed_project_structure(
    db: Session,
    tenant: Tenant,
    users: dict[str, User],
    *,
    project_name: str,
    project_description: str,
    stage_specs: list[dict],
    stage_access: dict[str, list[str]],
    stage_references: dict[str, list[str]],
    required_docs: list[dict],
    project_admin_emails: list[str],
) -> tuple[Project, dict[str, Team], dict[str, Stage]]:
    print(f"\n[*] Seeding Project '{project_name}'...")

    project = db.execute(
        select(Project).where(Project.tenant_id == tenant.tenant_id, Project.name == project_name)
    ).scalar_one_or_none()
    if not project:
        project = Project(tenant_id=tenant.tenant_id, name=project_name, description=project_description)
        db.add(project)
        db.flush()
        print(f"  [+] Created project: {project_name} ({project.project_id})")
    else:
        print(f"  [=] Validated existing project: {project_name} ({project.project_id})")

    # Teams (shared TEAMS list — created per-project, since Team.project_id
    # is a real FK; a team named "Engineering" in project 1 is a distinct
    # row from "Engineering" in project 2).
    teams: dict[str, Team] = {}
    for team_name in TEAMS:
        team = db.execute(
            select(Team).where(Team.project_id == project.project_id, Team.name == team_name)
        ).scalar_one_or_none()
        if not team:
            team = Team(project_id=project.project_id, name=team_name)
            db.add(team)
            db.flush()
            print(f"  [+] Created team: {team_name}")
        teams[team_name] = team

    # Stages
    stages: dict[str, Stage] = {}
    for stg in stage_specs:
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
            print(f"  [+] Created stage: {stg['name']} (order={stg['order_index']}, requires_approval={stg['requires_approval']})")
        stages[stg["name"]] = stage

    # TeamStageAccess
    for stage_name, team_names in stage_access.items():
        stage = stages[stage_name]
        for t_name in team_names:
            t = teams[t_name]
            existing = db.execute(
                select(TeamStageAccess).where(
                    TeamStageAccess.team_id == t.team_id, TeamStageAccess.stage_id == stage.stage_id,
                )
            ).scalar_one_or_none()
            if not existing:
                db.add(TeamStageAccess(team_id=t.team_id, stage_id=stage.stage_id))
                db.flush()
                print(f"  [+] Access: '{stage_name}' <- '{t_name}'")

    # StageReferences
    for consumer_name, referenced_names in stage_references.items():
        consumer_stage = stages[consumer_name]
        for ref_name in referenced_names:
            ref_stage = stages[ref_name]
            existing_ref = db.execute(
                select(StageReference).where(
                    StageReference.stage_id == consumer_stage.stage_id,
                    StageReference.references_stage_id == ref_stage.stage_id,
                )
            ).scalar_one_or_none()
            if not existing_ref:
                db.add(StageReference(stage_id=consumer_stage.stage_id, references_stage_id=ref_stage.stage_id))
                db.flush()
                print(f"  [+] Reference: {consumer_name} -> {ref_name}")

    # RequiredDocument checklist per stage
    for rd in required_docs:
        stage = stages[rd["stage"]]
        existing_rd = db.execute(
            select(RequiredDocument).where(
                RequiredDocument.stage_id == stage.stage_id, RequiredDocument.name == rd["name"],
            )
        ).scalar_one_or_none()
        if not existing_rd:
            db.add(RequiredDocument(
                stage_id=stage.stage_id, name=rd["name"], is_mandatory=rd["is_mandatory"],
                source=RequirementSource.custom,
                description=(
                    "Deliberately left unsatisfied for this demo — no document has been uploaded against it."
                    if rd["filename"] is None else rd.get("description")
                ),
            ))
            db.flush()
            status = "OUTSTANDING (no doc)" if rd["filename"] is None else "will be satisfied"
            print(f"  [+] Required doc: '{rd['stage']}' needs '{rd['name']}' ({status})")

    # Project admins
    for email in project_admin_emails:
        user = users[email]
        pa = db.execute(
            select(ProjectAdmin).where(ProjectAdmin.user_id == user.user_id, ProjectAdmin.project_id == project.project_id)
        ).scalar_one_or_none()
        if not pa:
            db.add(ProjectAdmin(user_id=user.user_id, project_id=project.project_id))
            db.flush()
            print(f"  [+] Project admin: {user.full_name}")

    # Team memberships — driven by each USER's own `memberships` list,
    # applied to every team of the same name within THIS project.
    for u_info in USERS:
        user = users[u_info["email"]]
        for t_name, role in u_info["memberships"]:
            if t_name not in teams:
                continue
            t = teams[t_name]
            mem = db.execute(
                select(UserTeamMembership).where(
                    UserTeamMembership.user_id == user.user_id,
                    UserTeamMembership.team_id == t.team_id,
                    UserTeamMembership.project_id == project.project_id,
                )
            ).scalar_one_or_none()
            if not mem:
                db.add(UserTeamMembership(user_id=user.user_id, team_id=t.team_id, project_id=project.project_id, role=role))
                db.flush()
                print(f"  [+] Membership: {u_info['name']} -> {t_name} ({role.value})")

    db.commit()
    return project, teams, stages


# ---------------------------------------------------------------------------
# Documents — real upload + scan + finalize + (submit/approve) pipeline
# ---------------------------------------------------------------------------

def seed_documents(
    db: Session,
    docs_dir: Path,
    project: Project,
    teams: dict[str, Team],
    stages: dict[str, Stage],
    users: dict[str, User],
    doc_specs: list[dict],
):
    print(f"\n[*] Ingesting {len(doc_specs)} documents for project '{project.name}'...")

    for i, d_spec in enumerate(doc_specs):
        fname = d_spec["filename"]
        stage = stages[d_spec["stage"]]
        team = teams[d_spec["team"]]
        author = users[d_spec["author_email"]]
        fpath = docs_dir / fname
        if not fpath.exists():
            raise FileNotFoundError(f"Missing seed document file: {fpath}")

        existing_doc = db.execute(
            select(Document).where(Document.project_id == project.project_id, Document.original_filename == fname)
        ).scalar_one_or_none()
        if existing_doc:
            print(f"  [=] Validated existing document: '{fname}' ({existing_doc.document_id})")
            continue

        print(f"\n  --> Uploading '{fname}' as {author.full_name} ({d_spec['team']} -> {d_spec['stage']})...")
        with open(fpath, "rb") as f:
            file_bytes = f.read()

        role = "org_admin" if author.is_org_admin else "team_lead"
        session_id = str(uuid.uuid4())

        result = upload_and_scan(
            db,
            user_id=author.user_id, team_id=team.team_id, project_id=project.project_id, role=role,
            stage_id=stage.stage_id, original_filename=fname, mime_type="text/markdown",
            file_data=file_bytes, session_id=session_id, sensitivity=d_spec["sensitivity"],
        )
        doc_id = result["created"].document_id
        print(f"      Uploaded: doc_id={doc_id}, status={result['status']}, score={(result.get('scan') or {}).get('overall_score')}")

        # Finalize via the real chat-finalize pipeline (writes a v2, and for
        # a non-approval stage this is the ONLY thing that ever promotes a
        # passing scan to indexed — see document_finalize.py).
        fin_result = finalize_document_revision(db, document_id=doc_id, user_id=author.user_id, session_id=session_id)
        print(f"      Finalized: status={fin_result['status']}, should_index={fin_result['should_index']}")

        action = d_spec["action"]
        workflow = db.execute(select(WorkflowState).where(WorkflowState.document_id == doc_id)).scalar_one_or_none()
        already_approved = workflow is not None and workflow.state == WorkflowStatus.approved

        if already_approved:
            # The author already held 'approve' rights on this team, so
            # upload_and_scan()'s auto-approve path (BUGFIXES_2026-09-15.md)
            # already promoted this document straight to approved+indexed —
            # submit_for_review()/approve_document() would raise
            # WorkflowError, since only a 'draft'/'rejected' document can be
            # submitted. Nothing further to do; this is the expected outcome
            # for a team_lead/org_admin author on an approval-required stage.
            print("      Already auto-approved at upload (author holds approve rights) — no explicit submit/approve needed.")
        elif action in ("submit_only", "submit_approve"):
            submit_for_review(db, doc_id, author.user_id, team.team_id, project.project_id, role)
            print("      Submitted for review.")
            if action == "submit_approve":
                approver = users[d_spec["approver_email"]]
                approver_role = "org_admin" if approver.is_org_admin else "team_lead"
                approve_document(db, doc_id, approver.user_id, team.team_id, project.project_id, approver_role)
                print(f"      Approved by {approver.full_name}.")

        if d_spec.get("upload_date"):
            dt = datetime.fromisoformat(d_spec["upload_date"].replace("Z", "+00:00"))
            doc_obj = db.get(Document, uuid.UUID(str(doc_id)))
            if doc_obj:
                doc_obj.created_at = dt
                for v in db.execute(select(DocumentVersion).where(DocumentVersion.document_id == doc_obj.document_id)).scalars():
                    v.created_at = dt
                db.commit()

        if i < len(doc_specs) - 1:
            time.sleep(2.0)

    print(f"\n[+] All documents ingested for '{project.name}'.")


# ---------------------------------------------------------------------------
# Graph sync + audit (per project)
# ---------------------------------------------------------------------------

def run_graph_and_audit(db: Session, project: Project, tenant: Tenant):
    project_id = project.project_id
    tenant_id = tenant.tenant_id
    print(f"\n[*] Running graph sync for '{project.name}' ({project_id})...")

    sync_res = sync_project_graph(db, project_id)
    print(f"  [+] Nodes synced: {sync_res.nodes_synced}, Edges synced: {sync_res.edges_synced}")
    if sync_res.errors:
        print(f"  [!] Sync warnings: {sync_res.errors}")
    db.commit()

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

        rel_res = extract_document_relationships(db=db, document_id=doc.document_id, version_id=ver.version_id, content=content_str)
        print(f"  [+] '{doc.original_filename}': {rel_res.edges_created} relationships extracted")

        claims_data = extract_claims_from_text(
            text=content_str, tenant_id=tenant_id, project_id=project_id,
            document_id=doc.document_id, version_id=ver.version_id,
        )
        if claims_data:
            persisted = persist_claims(db, tenant_id, project_id, doc.document_id, ver.version_id, claims_data)
            print(f"  [+] '{doc.original_filename}': {len(persisted)} claims persisted")
        db.commit()

    contradictions = detect_project_contradictions(db, tenant_id, project_id)
    print(f"  [+] Contradictions detected: {len(contradictions)}")
    db.commit()

    audit_run = execute_project_audit(db, project_id)
    blockers = sum(1 for f in audit_run.findings if f.is_blocker)
    print(f"  [+] Audit run: {audit_run.run_id}, readiness={audit_run.readiness_status}, blockers={blockers}, findings={len(audit_run.findings)}")
    db.commit()


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="DocFlow AI Meridian Pay Demo Seed Script")
    parser.add_argument("--reset", action="store_true", help="Wipe Meridian Pay demo data before seeding")
    args = parser.parse_args()

    docs_dir = find_docs_dir()

    db = SessionLocal()
    db.info["tenant_id"] = str(MERIDIANPAY_TENANT_ID)
    try:
        if args.reset:
            reset_meridianpay_data(db)
        tenant = get_or_create_tenant(db)
        db.commit()
        users = seed_users(db, tenant)

        project1, teams1, stages1 = seed_project_structure(
            db, tenant, users,
            project_name=PROJECT_1_NAME, project_description=PROJECT_1_DESCRIPTION,
            stage_specs=PROJECT_1_STAGES, stage_access=PROJECT_1_STAGE_ACCESS,
            stage_references=PROJECT_1_STAGE_REFERENCES, required_docs=PROJECT_1_REQUIRED_DOCS,
            project_admin_emails=[email for email, projs in PROJECT_ADMINS.items() if PROJECT_1_NAME in projs],
        )
        project2, teams2, stages2 = seed_project_structure(
            db, tenant, users,
            project_name=PROJECT_2_NAME, project_description=PROJECT_2_DESCRIPTION,
            stage_specs=PROJECT_2_STAGES, stage_access=PROJECT_2_STAGE_ACCESS,
            stage_references=PROJECT_2_STAGE_REFERENCES, required_docs=PROJECT_2_REQUIRED_DOCS,
            project_admin_emails=[email for email, projs in PROJECT_ADMINS.items() if PROJECT_2_NAME in projs],
        )
    finally:
        db.close()

    db = SessionLocal()
    db.info["tenant_id"] = str(MERIDIANPAY_TENANT_ID)
    try:
        tenant = get_or_create_tenant(db)
        users = {u.email: u for u in db.execute(select(User).where(User.tenant_id == tenant.tenant_id)).scalars()}
        project1 = db.execute(select(Project).where(Project.tenant_id == tenant.tenant_id, Project.name == PROJECT_1_NAME)).scalar_one()
        teams1 = {t.name: t for t in db.execute(select(Team).where(Team.project_id == project1.project_id)).scalars()}
        stages1 = {s.name: s for s in db.execute(select(Stage).where(Stage.project_id == project1.project_id)).scalars()}
        seed_documents(db, docs_dir, project1, teams1, stages1, users, PROJECT_1_DOCS)
    finally:
        db.close()

    db = SessionLocal()
    db.info["tenant_id"] = str(MERIDIANPAY_TENANT_ID)
    try:
        tenant = get_or_create_tenant(db)
        users = {u.email: u for u in db.execute(select(User).where(User.tenant_id == tenant.tenant_id)).scalars()}
        project2 = db.execute(select(Project).where(Project.tenant_id == tenant.tenant_id, Project.name == PROJECT_2_NAME)).scalar_one()
        teams2 = {t.name: t for t in db.execute(select(Team).where(Team.project_id == project2.project_id)).scalars()}
        stages2 = {s.name: s for s in db.execute(select(Stage).where(Stage.project_id == project2.project_id)).scalars()}
        seed_documents(db, docs_dir, project2, teams2, stages2, users, PROJECT_2_DOCS)
    finally:
        db.close()

    db = SessionLocal()
    db.info["tenant_id"] = str(MERIDIANPAY_TENANT_ID)
    try:
        tenant = get_or_create_tenant(db)
        project1 = db.execute(select(Project).where(Project.tenant_id == tenant.tenant_id, Project.name == PROJECT_1_NAME)).scalar_one()
        project2 = db.execute(select(Project).where(Project.tenant_id == tenant.tenant_id, Project.name == PROJECT_2_NAME)).scalar_one()
        run_graph_and_audit(db, project1, tenant)
        run_graph_and_audit(db, project2, tenant)
        print("\n[+] Meridian Pay seed completed successfully.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
