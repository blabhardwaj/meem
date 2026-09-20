"""
DocFlow AI — Lendora Financial Demo Seed Script

Populates the database with the Lendora Financial organization, the
"SME FlexCredit Launch" project, 6 functional teams, 7 lifecycle stages with
per-stage mandatory requirements, 6 named users with distinct ABAC roles,
and ~27 demo documents processed through the real application ingestion
pipeline (upload -> scan -> finalize -> [submit -> approve] -> index).

Deliberately seeds a few "live demo" moments rather than a fully finished
state:
  - `09_fraud_controls_framework_BAD.md` is uploaded and auto-scanned but
    NEVER finalized -- it sits in pending_review with a failing scan score,
    ready to be shown live as "this document fails the structure scan."
  - `09_fraud_and_transaction_monitoring_framework.md` (the good version of
    the same requirement, deliberately named differently) is uploaded and
    finalized live to show the scan passing and the semantic-evidence match
    against the "Fraud Controls" requirement despite the filename mismatch.
  - `21_go_live_readiness_checklist_v1.md` / `_v2.md` are seeded as two
    real, finalized versions of the SAME document (via upload-version) so
    the version-diff/compare panel has real content differences to show,
    with both versions passing the scan cleanly.
  - `11_master_lending_agreement.md` is sensitivity=confidential, restricted
    to Legal/Compliance/Admin, to demonstrate Engineering being blocked.
  - `27_launch_approval_memo.md` deliberately has no matching "Business
    sign-off" requirement evidence -- a real, visible audit gap for the
    Launch Readiness stage's readiness/gap-analysis view.

Usage:
    python -m seed.seed_lendora            # Seed demo data (idempotent)
    python -m seed.seed_lendora --reset    # Wipe Lendora demo data and re-seed
    python -m seed.seed_lendora --verify   # Run verification suite
"""

import argparse
import logging
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

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
from app.models.required_document import RequiredDocument
from app.services.auth import hash_password, create_session_token
from app.services.rag.collection_setup import get_qdrant_client, collection_name_for_tenant
from qdrant_client import models as qm

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("seed_lendora")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LENDORA_TENANT_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
COMPANY_NAME = "Lendora Financial"
PROJECT_NAME = "SME FlexCredit Launch"
PROJECT_DESCRIPTION = (
    "Launch Lendora's SME FlexCredit product, a revolving working-capital "
    "credit line for small and medium businesses. The launch requires "
    "coordinated product, risk, compliance, legal, engineering, operations, "
    "and go-to-market documentation before regulatory and internal launch "
    "gates are cleared."
)
DEFAULT_PASSWORD = "DemoPass123!"

TEAMS = ["Product", "Risk", "Compliance", "Engineering", "Legal", "Operations"]

STAGES = [
    {
        "name": "Product Discovery", "order_index": 1, "requires_approval": False,
        "description": (
            "Define the target SME segment, customer problem, product "
            "objectives, eligibility assumptions, and business case for "
            "SME FlexCredit."
        ),
        "owning_teams": ["Product"],
    },
    {
        "name": "Product Definition", "order_index": 2, "requires_approval": False,
        "description": (
            "Translate the product concept into detailed credit-line terms, "
            "eligibility criteria, pricing, limits, repayment behavior, and "
            "customer experience requirements."
        ),
        "owning_teams": ["Product"],
    },
    {
        "name": "Risk & Compliance", "order_index": 3, "requires_approval": True,
        "description": (
            "Establish the risk, compliance, and regulatory controls "
            "required to safely launch SME FlexCredit."
        ),
        "owning_teams": ["Risk", "Compliance"],
    },
    {
        "name": "Legal & Policy", "order_index": 4, "requires_approval": True,
        "description": (
            "Prepare the contractual, customer-facing, and internal policy "
            "documentation required for product launch."
        ),
        "owning_teams": ["Legal", "Compliance"],
    },
    {
        "name": "Technology & Security", "order_index": 5, "requires_approval": False,
        "description": (
            "Build and validate the technology platform, integrations, "
            "data controls, and security mechanisms required to operate "
            "SME FlexCredit."
        ),
        "owning_teams": ["Engineering"],
    },
    {
        "name": "Operations Readiness", "order_index": 6, "requires_approval": False,
        "description": (
            "Prepare operational teams, servicing workflows, customer "
            "support processes, monitoring, and incident procedures for "
            "production launch."
        ),
        "owning_teams": ["Operations"],
    },
    {
        "name": "Launch Readiness", "order_index": 7, "requires_approval": True,
        "description": (
            "Consolidate final approvals, readiness evidence, outstanding "
            "risks, and launch decisions before releasing SME FlexCredit "
            "to customers."
        ),
        "owning_teams": ["Product", "Risk", "Compliance", "Legal", "Engineering", "Operations"],
    },
]

# Every team can see every stage's existence/name (app-wide invariant); this
# only controls native document-visibility/upload access per stage. Every
# stage is also fully open to project_admin/org_admin regardless of this map.
STAGE_ACCESS = {
    "Product Discovery": ["Product", "Risk", "Compliance", "Engineering", "Legal", "Operations"],
    "Product Definition": ["Product", "Risk", "Compliance", "Engineering", "Legal", "Operations"],
    "Risk & Compliance": ["Risk", "Compliance", "Product"],
    "Legal & Policy": ["Legal", "Compliance", "Product"],
    "Technology & Security": ["Engineering", "Risk"],
    "Operations Readiness": ["Operations", "Engineering", "Risk"],
    "Launch Readiness": ["Product", "Risk", "Compliance", "Legal", "Engineering", "Operations"],
}

REQUIREMENTS = {
    "Product Discovery": [
        ("Market/problem statement", "Documented SME market sizing and customer problem statement."),
        ("Target customer profile", "Defined target SME segment: turnover band, vintage, digital footprint."),
        ("Product concept", "High-level credit-line concept and differentiation from competitors."),
        ("Business case", "Financial projections, investment required, and key risks for the product."),
        ("Competitive analysis", "Landscape of comparable SME lending products and pricing."),
        ("Product success metrics", "Discovery-stage validation metrics and targets."),
    ],
    "Product Definition": [
        ("Product requirements document", "Full functional PRD covering credit-line structure and onboarding."),
        ("Eligibility criteria", "Hard eligibility gates and credit-limit assignment bands."),
        ("Pricing and fee structure", "Interest rate structure and complete fee schedule."),
        ("Credit-limit framework", "Limit assignment, increase, and decrease/freeze rules."),
        ("Customer journey", "End-to-end borrower experience mapping from application to closure."),
        ("Product risk assumptions", "Key product-level risk assumptions feeding the Risk & Compliance stage."),
    ],
    "Risk & Compliance": [
        ("Credit risk policy", "Board-level risk appetite, underwriting framework, and provisioning policy."),
        ("Underwriting framework", "Scoring model inputs, decisioning thresholds, and manual review SLA."),
        ("KYC / AML requirements", "Customer due diligence, risk categorization, and ongoing monitoring controls."),
        ("Regulatory applicability assessment", "Every regulatory framework applicable to the product's launch and operation."),
        ("Risk appetite statement", "Board-approved NPA ceiling, exposure limits, and concentration limits."),
        (
            "Fraud controls",
            "Fraud detection rules, transaction monitoring, and escalation procedures covering "
            "identity fraud, shell-company fraud, bust-out fraud, and fund diversion.",
        ),
        ("Credit decisioning documentation", "Auditable record of underwriting decision logic and thresholds."),
        ("Compliance sign-off", "Formal Compliance Officer sign-off on regulatory readiness."),
    ],
    "Legal & Policy": [
        ("Master lending agreement", "The borrower-facing credit line contract and its terms."),
        ("Customer terms and conditions", "Terms governing borrower use of the credit line product."),
        ("Privacy policy updates", "DPDPA-compliant privacy policy covering new SME FlexCredit data categories."),
        ("Consent language", "Consent artifacts for AA data pull, GST data, and ongoing monitoring."),
        ("Disclosure requirements", "Key Fact Statement and Fair Practices Code disclosure obligations."),
        ("Collections policy", "Delinquency stages, conduct standards, and recovery escalation."),
        ("Legal review / approval", "Formal Legal Counsel sign-off on all launch contractual documentation."),
    ],
    "Technology & Security": [
        ("Technical architecture", "Platform component architecture, data model, and integration points."),
        ("API specification", "Bureau and Account Aggregator integration request/response contracts."),
        ("Credit-bureau integration", "Bureau pull flow, data fields consumed, and refresh cadence."),
        ("Data model", "Core platform entities: Application, CreditLine, Transaction, BorrowerProfile."),
        ("Security architecture", "Access control design, data protection, and audit logging controls."),
        ("Threat model", "STRIDE-based threat analysis and fraud-specific threat scenarios."),
        ("Access-control design", "RBAC roles, service-to-service auth, and borrower-facing auth model."),
        ("Production deployment plan", "Rollout topology and deployment sequencing for go-live."),
    ],
    "Operations Readiness": [
        ("Operations runbook", "Daily/weekly operational checklists and standard operating procedures."),
        ("Customer support playbook", "Support scripts, escalation triggers, and compliance conduct guidelines."),
        ("Loan servicing workflow", "Draw-down, repayment, NACH mandate, and limit-review servicing flow."),
        ("Incident response procedure", "Severity classification, response roles, and post-incident review process."),
        ("Escalation matrix", "Named contact escalation paths by issue type and severity."),
        ("Monitoring and alerting plan", "Platform, portfolio, fraud, and compliance monitoring thresholds."),
        ("Training material", "Operations/Support/Credit Officer onboarding and certification curriculum."),
    ],
    "Launch Readiness": [
        ("Final readiness checklist", "Consolidated go-live readiness status across every workstream."),
        ("Go-live approval", "Documented decision to proceed with the go-live date."),
        ("Outstanding risk register", "Non-blocking risks carried into post-launch monitoring."),
        ("Production validation report", "End-to-end production validation test results ahead of go-live."),
        ("Business sign-off", "Executive sponsor's final business approval memo."),
        ("Compliance sign-off", "Compliance's final launch-readiness approval."),
        ("Launch communications plan", "Customer and internal communications plan for the go-live date."),
    ],
}

USERS = [
    {
        "name": "Aditi Mehra", "email": "aditi.mehra@lendorafinancial.com",
        "title": "Product Manager", "is_org_admin": False, "is_project_admin": True,
        "memberships": [("Product", TeamRole.team_lead)],
    },
    {
        "name": "Rohan Kapoor", "email": "rohan.kapoor@lendorafinancial.com",
        "title": "Risk Lead", "is_org_admin": False, "is_project_admin": False,
        "memberships": [("Risk", TeamRole.team_lead)],
    },
    {
        "name": "Neha Sharma", "email": "neha.sharma@lendorafinancial.com",
        "title": "Compliance Officer", "is_org_admin": False, "is_project_admin": False,
        "memberships": [("Compliance", TeamRole.team_lead)],
    },
    {
        "name": "Arjun Malhotra", "email": "arjun.malhotra@lendorafinancial.com",
        "title": "Engineering Lead", "is_org_admin": False, "is_project_admin": False,
        "memberships": [("Engineering", TeamRole.team_lead)],
    },
    {
        "name": "Priya Nair", "email": "priya.nair@lendorafinancial.com",
        "title": "Legal Counsel", "is_org_admin": False, "is_project_admin": False,
        "memberships": [("Legal", TeamRole.team_lead)],
    },
    {
        "name": "Kabir Singh", "email": "kabir.singh@lendorafinancial.com",
        "title": "Operations Lead", "is_org_admin": False, "is_project_admin": False,
        "memberships": [("Operations", TeamRole.team_lead)],
    },
    {
        "name": "Abhardwaj", "email": "blabhardwaj@gmail.com",
        "title": "Admin / Project Owner", "is_org_admin": True, "is_project_admin": True,
        "memberships": [],
    },
]

# Single-version documents. Each is uploaded and finalized; for stages that
# require_approval, "approver_email" submits/approves it after finalize.
DOCUMENTS = [
    ("01_sme_market_analysis.md", "Product Discovery", "Product", "internal", "aditi.mehra@lendorafinancial.com", "2026-06-10T10:00:00Z", None),
    ("02_product_business_case.md", "Product Discovery", "Product", "internal", "aditi.mehra@lendorafinancial.com", "2026-06-16T10:00:00Z", None),
    ("03_sme_flexcredit_prd.md", "Product Definition", "Product", "internal", "aditi.mehra@lendorafinancial.com", "2026-06-29T10:00:00Z", None),
    ("04_eligibility_and_credit_limits.md", "Product Definition", "Product", "internal", "aditi.mehra@lendorafinancial.com", "2026-07-03T10:00:00Z", None),
    ("05_pricing_and_fee_schedule.md", "Product Definition", "Product", "internal", "aditi.mehra@lendorafinancial.com", "2026-07-06T10:00:00Z", None),
    ("06_customer_journey.md", "Product Definition", "Product", "internal", "aditi.mehra@lendorafinancial.com", "2026-07-09T10:00:00Z", None),
    ("07_credit_risk_policy.md", "Risk & Compliance", "Risk", "internal", "rohan.kapoor@lendorafinancial.com", "2026-07-16T10:00:00Z", "rohan.kapoor@lendorafinancial.com"),
    ("08_kyc_aml_assessment.md", "Risk & Compliance", "Compliance", "internal", "neha.sharma@lendorafinancial.com", "2026-07-19T10:00:00Z", "rohan.kapoor@lendorafinancial.com"),
    ("10_regulatory_applicability_assessment.md", "Risk & Compliance", "Compliance", "internal", "neha.sharma@lendorafinancial.com", "2026-07-26T10:00:00Z", "rohan.kapoor@lendorafinancial.com"),
    ("11_master_lending_agreement.md", "Legal & Policy", "Legal", "confidential", "priya.nair@lendorafinancial.com", "2026-07-29T10:00:00Z", "priya.nair@lendorafinancial.com"),
    ("12_customer_terms.md", "Legal & Policy", "Legal", "internal", "priya.nair@lendorafinancial.com", "2026-07-31T10:00:00Z", "priya.nair@lendorafinancial.com"),
    ("13_privacy_and_consent_review.md", "Legal & Policy", "Legal", "internal", "priya.nair@lendorafinancial.com", "2026-08-02T10:00:00Z", "priya.nair@lendorafinancial.com"),
    ("24_collections_policy.md", "Legal & Policy", "Legal", "internal", "priya.nair@lendorafinancial.com", "2026-08-05T10:00:00Z", "priya.nair@lendorafinancial.com"),
    ("14_platform_architecture.md", "Technology & Security", "Engineering", "internal", "arjun.malhotra@lendorafinancial.com", "2026-08-05T10:00:00Z", None),
    ("15_credit_bureau_integration_spec.md", "Technology & Security", "Engineering", "internal", "arjun.malhotra@lendorafinancial.com", "2026-08-07T10:00:00Z", None),
    ("16_security_architecture.md", "Technology & Security", "Engineering", "internal", "arjun.malhotra@lendorafinancial.com", "2026-08-09T10:00:00Z", None),
    ("17_threat_model.md", "Technology & Security", "Engineering", "internal", "arjun.malhotra@lendorafinancial.com", "2026-08-11T10:00:00Z", None),
    ("18_operations_runbook.md", "Operations Readiness", "Operations", "internal", "kabir.singh@lendorafinancial.com", "2026-08-13T10:00:00Z", None),
    ("19_customer_support_playbook.md", "Operations Readiness", "Operations", "internal", "kabir.singh@lendorafinancial.com", "2026-08-15T10:00:00Z", None),
    ("20_incident_response_plan.md", "Operations Readiness", "Operations", "internal", "kabir.singh@lendorafinancial.com", "2026-08-17T10:00:00Z", None),
    ("22_escalation_matrix.md", "Operations Readiness", "Operations", "internal", "kabir.singh@lendorafinancial.com", "2026-08-18T10:00:00Z", None),
    ("23_monitoring_and_alerting_plan.md", "Operations Readiness", "Operations", "internal", "kabir.singh@lendorafinancial.com", "2026-08-20T10:00:00Z", None),
    ("25_training_material.md", "Operations Readiness", "Operations", "internal", "kabir.singh@lendorafinancial.com", "2026-08-21T10:00:00Z", None),
    ("26_production_validation_report.md", "Launch Readiness", "Engineering", "internal", "arjun.malhotra@lendorafinancial.com", "2026-09-06T10:00:00Z", "aditi.mehra@lendorafinancial.com"),
    ("27_launch_approval_memo.md", "Launch Readiness", "Product", "internal", "aditi.mehra@lendorafinancial.com", "2026-09-11T10:00:00Z", "aditi.mehra@lendorafinancial.com"),
]

# The fraud-controls scan pair: BAD is uploaded+scanned only (left pending,
# never finalized -- "awaiting first scan" / failed-scan live demo state).
# GOOD is the real evidence for the "Fraud controls" requirement, uploaded
# and finalized live in the demo.
FRAUD_BAD_FILENAME = "09_fraud_controls_framework_BAD.md"
FRAUD_GOOD_FILENAME = "09_fraud_and_transaction_monitoring_framework.md"
FRAUD_STAGE = "Risk & Compliance"
FRAUD_TEAM = "Risk"
FRAUD_AUTHOR_EMAIL = "rohan.kapoor@lendorafinancial.com"
FRAUD_APPROVER_EMAIL = "rohan.kapoor@lendorafinancial.com"

# The version-diff pair: v1 uploaded+finalized first, v2 uploaded as a NEW
# VERSION of the same document via upload-version + version-message.
CHECKLIST_V1_FILENAME = "21_go_live_readiness_checklist_v1.md"
CHECKLIST_V2_FILENAME = "21_go_live_readiness_checklist_v2.md"
CHECKLIST_DISPLAY_FILENAME = "21_go_live_readiness_checklist.md"
CHECKLIST_STAGE = "Launch Readiness"
CHECKLIST_TEAM = "Product"
CHECKLIST_AUTHOR_EMAIL = "aditi.mehra@lendorafinancial.com"
CHECKLIST_APPROVER_EMAIL = "aditi.mehra@lendorafinancial.com"
CHECKLIST_V1_DATE = "2026-08-22T10:00:00Z"
CHECKLIST_V2_DATE = "2026-09-03T10:00:00Z"

# Hand-authored audit-log entries (account/permission-management actions,
# per app/services/audit.py's AUDIT_LOG_ACTIONS) backdated across the
# project's timeline, since these actions are not driven through their real
# endpoints during seeding (see module docstring / seed script design notes).
AUDIT_LOG_ENTRIES = [
    # (actor_email, action, resource_type, target_email_or_None, details_fn, when)
    #
    # details_fn is (project, teams) -> dict, resolved at seed time from the
    # real Project/Team rows just created — app/services/audit.py's
    # list_audit_log() scopes ASSIGN_ROLE/GRANT_PROJECT_ADMIN visibility by
    # reading details["team_id"]/details["project_id"] as real UUIDs
    # (_row_scope). A row storing the human-readable name instead (as this
    # file used to) resolves to (None, None) scope for every non-org-admin
    # viewer -- team leads and project admins (e.g. Aditi, a project_admin,
    # not org_admin) then see zero audit-log rows even though qualifying
    # rows exist, which is exactly the "why is the audit log empty" bug this
    # replaces.
    ("blabhardwaj@gmail.com", "SIGNUP", "user", "blabhardwaj@gmail.com", lambda p, t: {"method": "org_creation"}, "2026-06-01T09:00:00Z"),
    ("blabhardwaj@gmail.com", "INVITE_USER", "user", "aditi.mehra@lendorafinancial.com", lambda p, t: {"email": "aditi.mehra@lendorafinancial.com"}, "2026-06-02T09:15:00Z"),
    ("blabhardwaj@gmail.com", "INVITE_USER", "user", "rohan.kapoor@lendorafinancial.com", lambda p, t: {"email": "rohan.kapoor@lendorafinancial.com"}, "2026-06-02T09:20:00Z"),
    ("blabhardwaj@gmail.com", "INVITE_USER", "user", "neha.sharma@lendorafinancial.com", lambda p, t: {"email": "neha.sharma@lendorafinancial.com"}, "2026-06-02T09:25:00Z"),
    ("blabhardwaj@gmail.com", "INVITE_USER", "user", "arjun.malhotra@lendorafinancial.com", lambda p, t: {"email": "arjun.malhotra@lendorafinancial.com"}, "2026-06-02T09:30:00Z"),
    ("blabhardwaj@gmail.com", "INVITE_USER", "user", "priya.nair@lendorafinancial.com", lambda p, t: {"email": "priya.nair@lendorafinancial.com"}, "2026-06-02T09:35:00Z"),
    ("blabhardwaj@gmail.com", "INVITE_USER", "user", "kabir.singh@lendorafinancial.com", lambda p, t: {"email": "kabir.singh@lendorafinancial.com"}, "2026-06-02T09:40:00Z"),
    ("blabhardwaj@gmail.com", "GRANT_PROJECT_ADMIN", "user", "aditi.mehra@lendorafinancial.com", lambda p, t: {"email": "aditi.mehra@lendorafinancial.com", "project_id": str(p.project_id)}, "2026-06-02T10:00:00Z"),
    ("blabhardwaj@gmail.com", "ASSIGN_ROLE", "user", "aditi.mehra@lendorafinancial.com", lambda p, t: {"email": "aditi.mehra@lendorafinancial.com", "team_id": str(t["Product"].team_id), "project_id": str(p.project_id), "role": "team_lead"}, "2026-06-02T10:05:00Z"),
    ("blabhardwaj@gmail.com", "ASSIGN_ROLE", "user", "rohan.kapoor@lendorafinancial.com", lambda p, t: {"email": "rohan.kapoor@lendorafinancial.com", "team_id": str(t["Risk"].team_id), "project_id": str(p.project_id), "role": "team_lead"}, "2026-06-02T10:10:00Z"),
    ("blabhardwaj@gmail.com", "ASSIGN_ROLE", "user", "neha.sharma@lendorafinancial.com", lambda p, t: {"email": "neha.sharma@lendorafinancial.com", "team_id": str(t["Compliance"].team_id), "project_id": str(p.project_id), "role": "team_lead"}, "2026-06-02T10:15:00Z"),
    ("blabhardwaj@gmail.com", "ASSIGN_ROLE", "user", "arjun.malhotra@lendorafinancial.com", lambda p, t: {"email": "arjun.malhotra@lendorafinancial.com", "team_id": str(t["Engineering"].team_id), "project_id": str(p.project_id), "role": "team_lead"}, "2026-06-02T10:20:00Z"),
    ("blabhardwaj@gmail.com", "ASSIGN_ROLE", "user", "priya.nair@lendorafinancial.com", lambda p, t: {"email": "priya.nair@lendorafinancial.com", "team_id": str(t["Legal"].team_id), "project_id": str(p.project_id), "role": "team_lead"}, "2026-06-02T10:25:00Z"),
    ("blabhardwaj@gmail.com", "ASSIGN_ROLE", "user", "kabir.singh@lendorafinancial.com", lambda p, t: {"email": "kabir.singh@lendorafinancial.com", "team_id": str(t["Operations"].team_id), "project_id": str(p.project_id), "role": "team_lead"}, "2026-06-02T10:30:00Z"),
]


def find_docs_dir() -> Path:
    candidates = [
        Path("docs for demo3"),
        Path("../docs for demo3"),
        Path(__file__).resolve().parent.parent.parent / "docs for demo3",
        Path("d:/Ra/DocFlowAI/docs for demo3"),
    ]
    for c in candidates:
        if c.exists() and (c / "01_sme_market_analysis.md").exists():
            return c.resolve()
    raise FileNotFoundError("Could not find 'docs for demo3' directory with seed files.")


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def reset_demo_data(db: Session):
    print(f"\n[*] Resetting demo data for '{COMPANY_NAME}' ({LENDORA_TENANT_ID})...")

    tenant = db.execute(
        select(Tenant).where((Tenant.tenant_id == LENDORA_TENANT_ID) | (Tenant.name == COMPANY_NAME))
    ).scalar_one_or_none()
    if not tenant:
        print("  [-] No existing Lendora tenant found in DB.")
        return

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

    docs = db.execute(select(Document).where(Document.tenant_id == tenant_id)).scalars().all()
    doc_ids = [d.document_id for d in docs]

    if doc_ids:
        versions = db.execute(select(DocumentVersion).where(DocumentVersion.document_id.in_(doc_ids))).scalars().all()
        version_ids = [v.version_id for v in versions]

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
        db.query(RequiredDocument).filter(
            RequiredDocument.stage_id.in_(select(Stage.stage_id).where(Stage.project_id.in_(project_ids)))
        ).delete(synchronize_session=False)
        db.query(TeamStageAccess).filter(
            TeamStageAccess.stage_id.in_(select(Stage.stage_id).where(Stage.project_id.in_(project_ids)))
        ).delete(synchronize_session=False)
        db.query(Stage).filter(Stage.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(UserTeamMembership).filter(UserTeamMembership.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(ProjectAdmin).filter(ProjectAdmin.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(Team).filter(Team.project_id.in_(project_ids)).delete(synchronize_session=False)
        db.query(Project).filter(Project.project_id.in_(project_ids)).delete(synchronize_session=False)

    # audit_log is append-only at the database level (a trigger rejects any
    # UPDATE/DELETE) -- by design, per its own model docstring ("Append-only
    # record ... for security / compliance review"). Since AuditLog.user_id
    # is a non-nullable FK to users, and users.tenant_id is a non-nullable FK
    # to tenants, neither Users nor the Tenant itself can be deleted once any
    # audit history exists for them. Reset therefore only clears
    # project-scoped data (project/teams/stages/requirements/documents);
    # the tenant and its users are left in place and re-seeding is
    # idempotent by email/name, so nothing is duplicated on the next run.
    db.commit()
    print("  [+] Reset complete (project/teams/stages/documents cleared; tenant, users, and audit history preserved -- append-only by design).")


# ---------------------------------------------------------------------------
# Seed: structure, users, requirements
# ---------------------------------------------------------------------------

def seed_structure_and_users(db: Session):
    print("\n[*] Seeding Organization, Project, Teams & Stages...")

    tenant = db.get(Tenant, LENDORA_TENANT_ID)
    if not tenant:
        tenant = Tenant(tenant_id=LENDORA_TENANT_ID, name=COMPANY_NAME)
        db.add(tenant)
        db.flush()
        print(f"  [+] Created Tenant: {COMPANY_NAME} ({tenant.tenant_id})")
    else:
        print(f"  [=] Tenant already exists: {COMPANY_NAME}")

    project = db.execute(
        select(Project).where(Project.tenant_id == tenant.tenant_id, Project.name == PROJECT_NAME)
    ).scalar_one_or_none()
    if not project:
        project = Project(tenant_id=tenant.tenant_id, name=PROJECT_NAME, description=PROJECT_DESCRIPTION)
        db.add(project)
        db.flush()
        print(f"  [+] Created Project: {PROJECT_NAME} ({project.project_id})")
    else:
        print(f"  [=] Project already exists: {PROJECT_NAME}")

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
            stage.requires_approval = stg["requires_approval"]
            stage.order_index = stg["order_index"]
            db.flush()
        stages[stg["name"]] = stage

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
                db.add(UserTeamMembership(
                    user_id=user.user_id, team_id=t.team_id,
                    project_id=project.project_id, role=role,
                ))
                db.flush()
                print(f"  [+] Membership: {u_info['name']} -> {t_name} ({role.value})")

    db.commit()
    return tenant, project, teams, stages, users


def seed_requirements(db: Session, project, stages: dict[str, Stage], users: dict[str, User]):
    """
    Inserts RequiredDocument rows directly rather than through the API --
    the real create_requirement endpoint schedules a full project graph
    re-sync as a background task on EVERY call (10-15s each with Qdrant
    round trips), which is fine for one-off interactive use but far too
    slow for seeding ~44 requirements in a row. A single graph sync runs
    once at the end of the whole seed (trigger_project_audit), which is
    exactly what every one of those per-call syncs would have produced
    anyway -- so nothing is lost, seeding just doesn't pay for it 44 times.
    """
    print("\n[*] Seeding Stage Requirements...")

    for stage_name, reqs in REQUIREMENTS.items():
        stage = stages[stage_name]
        existing_names = {
            r.name for r in db.execute(
                select(RequiredDocument).where(RequiredDocument.stage_id == stage.stage_id)
            ).scalars()
        }
        for name, description in reqs:
            if name in existing_names:
                continue
            db.add(RequiredDocument(
                stage_id=stage.stage_id, name=name, description=description, is_mandatory=True,
            ))
            print(f"  [+] Requirement: '{stage_name}' -> {name}")

    db.commit()
    print("[+] All stage requirements seeded.")


# ---------------------------------------------------------------------------
# Seed: documents
# ---------------------------------------------------------------------------

def _upload_and_finalize(
    client: TestClient, db: Session, docs_dir: Path,
    fname: str, stage: Stage, team: Team, sensitivity: str,
    author: User, upload_date: str,
    approver: User | None,
) -> str:
    """Uploads via the real endpoint, finalizes, and (if given) submits+approves.
    Returns the document_id. Backdates created_at on doc + version."""
    fpath = docs_dir / fname
    if not fpath.exists():
        raise FileNotFoundError(f"Missing seed file: {fpath}")

    with open(fpath, "rb") as f:
        file_bytes = f.read()

    auth_headers = {"Authorization": f"Bearer {create_session_token(str(author.user_id))}"}

    up_res = client.post(
        "/documents/upload-file",
        headers=auth_headers,
        files={"file": (fname, file_bytes, "text/markdown")},
        data={
            "stage_id": str(stage.stage_id),
            "team_id": str(team.team_id),
            "sensitivity_level": sensitivity,
        },
    )
    if up_res.status_code != 201:
        raise RuntimeError(f"Failed to upload '{fname}': {up_res.status_code} - {up_res.text}")

    up_data = up_res.json()
    doc_id = up_data["document_id"]
    session_id = up_data["session_id"]
    print(f"      Uploaded: doc_id={doc_id}, status={up_data['status']}, score={(up_data.get('scan') or {}).get('overall_score')}")

    fin_res = client.post(
        "/documents/review/message",
        headers=auth_headers,
        json={"document_id": doc_id, "session_id": session_id, "message": "Looks good, finalize it."},
    )
    if fin_res.status_code != 200:
        raise RuntimeError(f"Failed to finalize '{fname}': {fin_res.status_code} - {fin_res.text}")
    fin_data = fin_res.json()
    print(f"      Finalized: status={fin_data.get('status')}, should_index={fin_data.get('should_index')}")

    # A team_lead (or project_admin) uploading to their OWN team's stage
    # auto-approves at upload time if the scan passes (has_permission(...,
    # "approve", ...) check in document_upload_review.py's can_auto_approve)
    # -- finalize's own status already reflects that ("indexed" means a
    # WorkflowState(approved) row was already created). Only call
    # submit/approve when finalize DIDN'T already auto-approve, otherwise
    # /submit correctly 409s ("only a draft or rejected document can be
    # submitted").
    if approver is not None and fin_data.get("status") != "indexed":
        sub_res = client.post(f"/documents/{doc_id}/submit", headers=auth_headers)
        if sub_res.status_code != 200:
            raise RuntimeError(f"Failed to submit '{fname}': {sub_res.status_code} - {sub_res.text}")
        approver_headers = {"Authorization": f"Bearer {create_session_token(str(approver.user_id))}"}
        app_res = client.post(f"/documents/{doc_id}/approve", headers=approver_headers)
        if app_res.status_code != 200:
            raise RuntimeError(f"Failed to approve '{fname}': {app_res.status_code} - {app_res.text}")
        print(f"      Submitted and approved by {approver.email}.")
    elif approver is not None:
        print(f"      Already auto-approved at finalize (uploader has approve rights on {team.name}).")

    if upload_date:
        dt = datetime.fromisoformat(upload_date.replace("Z", "+00:00"))
        doc_obj = db.get(Document, uuid.UUID(doc_id))
        if doc_obj:
            doc_obj.created_at = dt
            for v in db.execute(select(DocumentVersion).where(DocumentVersion.document_id == doc_obj.document_id)).scalars():
                v.created_at = dt
            db.commit()

    return doc_id


def _upload_only_no_finalize(
    client: TestClient, fname: str, stage: Stage, team: Team, sensitivity: str, author: User,
) -> dict:
    """Uploads + auto-scans but deliberately never finalizes -- leaves the
    document sitting in pending_review with its scan result visible, for a
    live 'this fails the structure scan' demo moment. Returns the raw
    upload response payload (document_id, session_id, scan, etc.)."""
    fpath_dir = find_docs_dir()
    fpath = fpath_dir / fname
    with open(fpath, "rb") as f:
        file_bytes = f.read()

    auth_headers = {"Authorization": f"Bearer {create_session_token(str(author.user_id))}"}
    up_res = client.post(
        "/documents/upload-file",
        headers=auth_headers,
        files={"file": (fname, file_bytes, "text/markdown")},
        data={
            "stage_id": str(stage.stage_id),
            "team_id": str(team.team_id),
            "sensitivity_level": sensitivity,
        },
    )
    if up_res.status_code != 201:
        raise RuntimeError(f"Failed to upload '{fname}': {up_res.status_code} - {up_res.text}")
    up_data = up_res.json()
    print(
        f"      Uploaded (left UNFINALIZED for live demo): doc_id={up_data['document_id']}, "
        f"status={up_data['status']}, score={(up_data.get('scan') or {}).get('overall_score')}, "
        f"failed_criteria={up_data.get('failed_criteria')}"
    )
    return up_data


def seed_documents(client: TestClient, db: Session, docs_dir: Path, teams, stages, users):
    print(f"\n[*] Ingesting demo documents from '{docs_dir}'...")

    for fname, stage_name, team_name, sensitivity, author_email, upload_date, approver_email in DOCUMENTS:
        existing_doc = db.execute(
            select(Document).where(Document.original_filename == fname)
        ).scalar_one_or_none()
        if existing_doc:
            print(f"  [=] Document '{fname}' already exists ({existing_doc.document_id}). Skipping.")
            continue

        stage = stages[stage_name]
        team = teams[team_name]
        author = users[author_email]
        approver = users[approver_email] if approver_email else None

        print(f"\n  --> Uploading '{fname}' as {author_email} ({team_name} -> {stage_name})...")
        _upload_and_finalize(client, db, docs_dir, fname, stage, team, sensitivity, author, upload_date, approver)

    # --- Fraud controls scan-fail/pass pair -------------------------------
    fraud_stage = stages[FRAUD_STAGE]
    fraud_team = teams[FRAUD_TEAM]
    fraud_author = users[FRAUD_AUTHOR_EMAIL]
    fraud_approver = users[FRAUD_APPROVER_EMAIL]

    existing_bad = db.execute(select(Document).where(Document.original_filename == FRAUD_BAD_FILENAME)).scalar_one_or_none()
    if not existing_bad:
        print(f"\n  --> Uploading '{FRAUD_BAD_FILENAME}' (deliberately left pending -- fails the structure scan)...")
        _upload_only_no_finalize(client, FRAUD_BAD_FILENAME, fraud_stage, fraud_team, "internal", fraud_author)
    else:
        print(f"  [=] Document '{FRAUD_BAD_FILENAME}' already exists. Skipping.")

    existing_good = db.execute(select(Document).where(Document.original_filename == FRAUD_GOOD_FILENAME)).scalar_one_or_none()
    if not existing_good:
        print(f"\n  --> Uploading '{FRAUD_GOOD_FILENAME}' (passes the scan, satisfies 'Fraud controls' via semantic match)...")
        _upload_and_finalize(
            client, db, docs_dir, FRAUD_GOOD_FILENAME, fraud_stage, fraud_team, "internal",
            fraud_author, "2026-07-22T10:00:00Z", fraud_approver,
        )
    else:
        print(f"  [=] Document '{FRAUD_GOOD_FILENAME}' already exists. Skipping.")

    # --- Go-live checklist version-diff pair -------------------------------
    checklist_stage = stages[CHECKLIST_STAGE]
    checklist_team = teams[CHECKLIST_TEAM]
    checklist_author = users[CHECKLIST_AUTHOR_EMAIL]
    checklist_approver = users[CHECKLIST_APPROVER_EMAIL]

    existing_checklist = db.execute(
        select(Document).where(Document.original_filename == CHECKLIST_DISPLAY_FILENAME)
    ).scalar_one_or_none()

    if not existing_checklist:
        print(f"\n  --> Uploading v1 of '{CHECKLIST_DISPLAY_FILENAME}' (draft, not ready for go-live)...")
        v1_path = docs_dir / CHECKLIST_V1_FILENAME
        with open(v1_path, "rb") as f:
            v1_bytes = f.read()
        auth_headers = {"Authorization": f"Bearer {create_session_token(str(checklist_author.user_id))}"}
        up_res = client.post(
            "/documents/upload-file",
            headers=auth_headers,
            files={"file": (CHECKLIST_DISPLAY_FILENAME, v1_bytes, "text/markdown")},
            data={
                "stage_id": str(checklist_stage.stage_id),
                "team_id": str(checklist_team.team_id),
                "sensitivity_level": "internal",
            },
        )
        if up_res.status_code != 201:
            raise RuntimeError(f"Failed to upload checklist v1: {up_res.status_code} - {up_res.text}")
        up_data = up_res.json()
        doc_id = up_data["document_id"]
        session_id = up_data["session_id"]
        print(f"      v1 uploaded: doc_id={doc_id}, score={(up_data.get('scan') or {}).get('overall_score')}")

        fin_res = client.post(
            "/documents/review/message",
            headers=auth_headers,
            json={"document_id": doc_id, "session_id": session_id, "message": "Finalize as draft v1."},
        )
        if fin_res.status_code != 200:
            raise RuntimeError(f"Failed to finalize checklist v1: {fin_res.status_code} - {fin_res.text}")
        fin_data = fin_res.json()
        print(f"      v1 finalized: {fin_data.get('status')}")

        approver_headers = {"Authorization": f"Bearer {create_session_token(str(checklist_approver.user_id))}"}
        if fin_data.get("status") != "indexed":
            sub_res = client.post(f"/documents/{doc_id}/submit", headers=auth_headers)
            if sub_res.status_code != 200:
                raise RuntimeError(f"Failed to submit checklist v1: {sub_res.status_code} - {sub_res.text}")
            app_res = client.post(f"/documents/{doc_id}/approve", headers=approver_headers)
            if app_res.status_code != 200:
                raise RuntimeError(f"Failed to approve checklist v1: {app_res.status_code} - {app_res.text}")
            print("      v1 submitted and approved.")
        else:
            print("      v1 already auto-approved at finalize.")

        dt1 = datetime.fromisoformat(CHECKLIST_V1_DATE.replace("Z", "+00:00"))
        doc_obj = db.get(Document, uuid.UUID(doc_id))
        doc_obj.created_at = dt1
        for v in db.execute(select(DocumentVersion).where(DocumentVersion.document_id == doc_obj.document_id)).scalars():
            v.created_at = dt1
        db.commit()

        # --- v2: real new version via upload-version + version-message ---
        print(f"  --> Uploading v2 of '{CHECKLIST_DISPLAY_FILENAME}' (final, ready for go-live)...")
        v2_path = docs_dir / CHECKLIST_V2_FILENAME
        with open(v2_path, "rb") as f:
            v2_bytes = f.read()
        uv_res = client.post(
            f"/documents/{doc_id}/upload-version",
            headers=auth_headers,
            files={"file": (CHECKLIST_DISPLAY_FILENAME, v2_bytes, "text/markdown")},
        )
        if uv_res.status_code != 201:
            raise RuntimeError(f"Failed to upload checklist v2: {uv_res.status_code} - {uv_res.text}")
        uv_data = uv_res.json()
        v2_session_id = uv_data["session_id"]
        print(f"      v2 diff: +{uv_data['diff']['added_lines']} / -{uv_data['diff']['removed_lines']} lines, score={(uv_data.get('scan') or {}).get('overall_score')}")

        v2_fin_res = client.post(
            "/documents/review/version-message",
            headers=auth_headers,
            json={"document_id": doc_id, "session_id": v2_session_id, "message": "Finalize as v2, final readiness."},
        )
        if v2_fin_res.status_code != 200:
            raise RuntimeError(f"Failed to finalize checklist v2: {v2_fin_res.status_code} - {v2_fin_res.text}")
        v2_fin_data = v2_fin_res.json()
        print(f"      v2 finalized: version_number={v2_fin_data.get('version_number')}, should_index={v2_fin_data.get('should_index')}")

        if v2_fin_data.get("status") != "indexed":
            sub2_res = client.post(f"/documents/{doc_id}/submit", headers=auth_headers)
            if sub2_res.status_code != 200:
                raise RuntimeError(f"Failed to submit checklist v2: {sub2_res.status_code} - {sub2_res.text}")
            app2_res = client.post(f"/documents/{doc_id}/approve", headers=approver_headers)
            if app2_res.status_code != 200:
                raise RuntimeError(f"Failed to approve checklist v2: {app2_res.status_code} - {app2_res.text}")
            print("      v2 submitted and approved.")

        dt2 = datetime.fromisoformat(CHECKLIST_V2_DATE.replace("Z", "+00:00"))
        for v in db.execute(select(DocumentVersion).where(DocumentVersion.document_id == doc_obj.document_id)).scalars():
            if v.created_at == dt1:
                continue
            v.created_at = dt2
        db.commit()
    else:
        print(f"  [=] Document '{CHECKLIST_DISPLAY_FILENAME}' already exists. Skipping version pair.")

    print("\n[+] Document ingestion complete.")


# ---------------------------------------------------------------------------
# Seed: audit log entries
# ---------------------------------------------------------------------------

def seed_audit_log(db: Session, project: Project, teams: dict[str, Team], users: dict[str, User]):
    print("\n[*] Seeding Audit Log entries (backdated)...")
    added = 0
    for actor_email, action, resource_type, target_email, details_fn, when in AUDIT_LOG_ENTRIES:
        actor = users.get(actor_email)
        if not actor:
            continue
        target_user = users.get(target_email) if target_email else None
        resource_id = target_user.user_id if target_user else actor.user_id
        details = details_fn(project, teams)

        existing_rows = db.execute(
            select(AuditLog).where(
                AuditLog.user_id == actor.user_id,
                AuditLog.action == action,
                AuditLog.resource_id == resource_id,
            )
        ).scalars().all()
        # Idempotency is keyed on "a row with the CORRECT scope already
        # exists" rather than "any row with this (user, action, resource)
        # exists" -- audit_log is append-only (no UPDATE/DELETE, verified:
        # even a DELETE is rejected by the DB trigger, not just an UPDATE),
        # so a malformed row from an earlier, broken version of this script
        # (team/project NAME in details instead of a real team_id/
        # project_id -- see the docstring note above) can never be removed
        # or corrected in place. It's left as harmless orphaned history
        # (org_admin already sees every row regardless of scope; only a
        # non-org-admin viewer's team/project-scoped filter needed the real
        # id, which the malformed row never had) and a fresh, correctly-
        # scoped row is inserted alongside it instead.
        if any(
            (action == "ASSIGN_ROLE" and "team_id" in (r.details or {}))
            or (action == "GRANT_PROJECT_ADMIN" and "project_id" in (r.details or {}))
            or (action not in ("ASSIGN_ROLE", "GRANT_PROJECT_ADMIN"))
            for r in existing_rows
        ):
            continue

        # audit_log is append-only (a DB trigger rejects any UPDATE/DELETE
        # of a row's CONTENT once inserted, see reset_demo_data's own note
        # on this) -- created_at MUST be set on construction, as part of the
        # single INSERT. Setting it after an add()+flush() (as this used to)
        # issues an UPDATE, which the trigger rejects outright.
        dt = datetime.fromisoformat(when.replace("Z", "+00:00"))
        log = AuditLog(
            user_id=actor.user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            created_at=dt,
        )
        db.add(log)
        db.flush()
        added += 1

    db.commit()
    print(f"  [+] {added} audit log entries added (already-present entries skipped).")


# ---------------------------------------------------------------------------
# Trigger audit run (graph sync + R001 evaluation)
# ---------------------------------------------------------------------------

def trigger_project_audit(client: TestClient, project, users: dict[str, User]):
    print("\n[*] Triggering project audit (graph sync + requirement evaluation)...")
    admin = users["blabhardwaj@gmail.com"]
    auth_headers = {"Authorization": f"Bearer {create_session_token(str(admin.user_id))}"}
    resp = client.post(f"/projects/{project.project_id}/intelligence/audit", headers=auth_headers, json={})
    if resp.status_code != 200:
        print(f"  [!] Audit trigger returned {resp.status_code}: {resp.text}")
        return
    data = resp.json()
    print(f"  [+] Audit run complete: readiness_status={data.get('readiness_status')}, run_id={data.get('run_id')}")


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="DocFlow AI Lendora Financial Demo Seed Script")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()

    db = SessionLocal()
    db.info["tenant_id"] = LENDORA_TENANT_ID
    try:
        if args.verify:
            print("Verification suite not implemented for this script yet -- inspect the app directly.")
            sys.exit(0)

        if args.reset:
            reset_demo_data(db)

        docs_dir = find_docs_dir()
        tenant, project, teams, stages, users = seed_structure_and_users(db)

        client = TestClient(app)
        seed_requirements(db, project, stages, users)
        seed_documents(client, db, docs_dir, teams, stages, users)
        seed_audit_log(db, project, teams, users)
        trigger_project_audit(client, project, users)

        print("\n[+] Lendora Financial demo seed complete.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
