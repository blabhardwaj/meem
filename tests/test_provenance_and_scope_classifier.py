import uuid
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.models.graph import Claim, Node
from app.models.document import Document, DocumentVersion
from app.models.stage import Stage
from app.models.user import User
from app.services.graph.claims_analyzer import (
    classify_claim_scope,
    detect_project_contradictions,
)
from app.services.query_context import set_query_context, reset_query_context
from app.tools.query_tools import get_document_info, get_version_history


def test_real_doc09_and_doc11_metadata_classification():
    """
    Operational requirement: Test real Doc 09 and Doc 11 documents and formatting.
    Both contain '## Sign-off' sections with 'Prepared by:', 'Reviewed by:', and 'Date:'.
    Verify they are strictly classified as 'document_metadata' and NEVER 'project'.
    """
    docs_dir = Path("d:/Ra/DocFlowAI/docs for demo3")
    assert docs_dir.exists(), "docs for demo3 directory must exist"

    doc09_text = (docs_dir / "09_fraud_and_transaction_monitoring_framework.md").read_text(encoding="utf-8")
    doc11_text = (docs_dir / "11_master_lending_agreement.md").read_text(encoding="utf-8")

    # Verify real content contains the exact problematic lines
    assert "Prepared by: Rohan Kapoor, Risk Lead" in doc09_text
    assert "Date: 2026-07-22" in doc09_text
    assert "Prepared by: Priya Nair, Legal Counsel" in doc11_text
    assert "Date: 2026-07-28" in doc11_text

    # Test claims derived from Doc 09
    doc09_prep_date_claim = Claim(
        subject="document preparation date",
        predicate="date",
        object="2026-07-22",
        source_locator={"snippet": "## 7. Sign-off\nPrepared by: Rohan Kapoor, Risk Lead\nDate: 2026-07-22", "section": "Sign-off"}
    )
    doc09_author_claim = Claim(
        subject="prepared by",
        predicate="author",
        object="Rohan Kapoor, Risk Lead",
        source_locator={"snippet": "Prepared by: Rohan Kapoor, Risk Lead", "section": "Sign-off"}
    )

    # Test claims derived from Doc 11
    doc11_prep_date_claim = Claim(
        subject="document preparation date",
        predicate="date",
        object="2026-07-28",
        source_locator={"snippet": "## 11. Sign-off\nPrepared by: Priya Nair, Legal Counsel\nDate: 2026-07-28", "section": "Sign-off"}
    )
    doc11_author_claim = Claim(
        subject="prepared by",
        predicate="author",
        object="Priya Nair, Legal Counsel",
        source_locator={"snippet": "Prepared by: Priya Nair, Legal Counsel", "section": "Sign-off"}
    )

    assert classify_claim_scope(doc09_prep_date_claim) == "document_metadata"
    assert classify_claim_scope(doc09_author_claim) == "document_metadata"
    assert classify_claim_scope(doc11_prep_date_claim) == "document_metadata"
    assert classify_claim_scope(doc11_author_claim) == "document_metadata"


def test_draft_agent_output_classification():
    """
    Verify actual Draft Agent output formatting ('Prepared By:', 'Generated:', timestamps)
    is classified as 'document_metadata' and excluded from R007.
    """
    draft_agent_claim_1 = Claim(
        subject="prepared by",
        predicate="author",
        object="Nishant Bhardwaj",
        source_locator={"snippet": "Prepared by: Nishant Bhardwaj\nGenerated: 2026-09-20 01:32 UTC"}
    )
    draft_agent_claim_2 = Claim(
        subject="document generation timestamp",
        predicate="date",
        object="2026-09-20 01:47 UTC",
        source_locator={"snippet": "Generated at: 2026-09-20 01:47 UTC\nPrepared by: System Draft Agent"}
    )

    assert classify_claim_scope(draft_agent_claim_1) == "document_metadata"
    assert classify_claim_scope(draft_agent_claim_2) == "document_metadata"


def test_legitimate_project_claims_classification():
    """
    Verify project-level specifications (NPA targets, SLAs, policy ownership)
    remain classified as 'project' scope.
    """
    npa_target_claim = Claim(
        subject="gross NPA target",
        predicate="threshold",
        object="4.5%",
        source_locator={"snippet": "Policy target: gross NPA <= 4.5% across all cohorts", "section": "Risk Appetite"}
    )
    sla_claim = Claim(
        subject="transaction triage SLA",
        predicate="duration",
        object="24 hours",
        source_locator={"snippet": "Triage: Risk team, 24-hour SLA for initial triage", "section": "Roles and Escalation"}
    )
    policy_owner_claim = Claim(
        subject="risk policy owner",
        predicate="assignee",
        object="Rohan Kapoor",
        source_locator={"snippet": "Policy owner: Rohan Kapoor, Risk Lead", "section": "Governance"}
    )

    assert classify_claim_scope(npa_target_claim) == "project"
    assert classify_claim_scope(sla_claim) == "project"
    assert classify_claim_scope(policy_owner_claim) == "project"


def test_r007_contradiction_engine_tri_state_invariants():
    """
    Test contradiction detection invariant:
    - Metadata claims (Doc 09 vs Doc 11 preparation dates) must produce 0 R007 contradictions.
    - Project claims (gross NPA target 4.5% vs 5.5%) must produce R007 contradictions.
    """
    tenant_id = uuid.uuid4()
    project_id = uuid.uuid4()
    doc09_id = uuid.uuid4()
    doc11_id = uuid.uuid4()

    doc_a = MagicMock(spec=Document)
    doc_a.document_id = doc09_id
    doc_a.original_filename = "09_fraud_and_transaction_monitoring_framework.md"
    from datetime import datetime, timezone
    doc_a.stage_id = uuid.uuid4()
    doc_a.created_at = datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)

    doc_b = MagicMock(spec=Document)
    doc_b.document_id = doc11_id
    doc_b.original_filename = "11_master_lending_agreement.md"
    doc_b.stage_id = uuid.uuid4()
    doc_b.created_at = datetime(2026, 7, 28, 10, 0, tzinfo=timezone.utc)

    # 1. Metadata claims: preparation dates
    c_meta_1 = Claim(
        tenant_id=tenant_id,
        project_id=project_id,
        document_id=doc09_id,
        subject="document preparation date",
        predicate="date",
        object="2026-07-22",
        source_locator={"scope": "document_metadata", "snippet": "Date: 2026-07-22"}
    )
    c_meta_2 = Claim(
        tenant_id=tenant_id,
        project_id=project_id,
        document_id=doc11_id,
        subject="document preparation date",
        predicate="date",
        object="2026-07-28",
        source_locator={"scope": "document_metadata", "snippet": "Date: 2026-07-28"}
    )

    mock_db_meta = MagicMock()
    mock_db_meta.query.return_value.join.return_value.filter.return_value.order_by.return_value.all.return_value = [
        (c_meta_1, doc_a),
        (c_meta_2, doc_b),
    ]

    meta_contradictions = detect_project_contradictions(mock_db_meta, tenant_id, project_id)
    assert len(meta_contradictions) == 0, f"Expected 0 contradictions for metadata claims, got {len(meta_contradictions)}"

    # 2. Legitimate project claims: NPA target contradiction
    c_proj_1 = Claim(
        tenant_id=tenant_id,
        project_id=project_id,
        document_id=doc09_id,
        subject="gross NPA target",
        predicate="target",
        object="4.5%",
        source_locator={"scope": "project", "snippet": "gross NPA <= 4.5%"}
    )
    c_proj_2 = Claim(
        tenant_id=tenant_id,
        project_id=project_id,
        document_id=doc11_id,
        subject="gross NPA target",
        predicate="target",
        object="5.5%",
        source_locator={"scope": "project", "snippet": "gross NPA <= 5.5%"}
    )

    node_a = MagicMock(spec=Node)
    node_a.source_id = doc09_id
    node_a.node_id = uuid.uuid4()

    node_b = MagicMock(spec=Node)
    node_b.source_id = doc11_id
    node_b.node_id = uuid.uuid4()

    from app.models.graph import Edge

    def mock_query_dispatch(model_or_first, *rest):
        q = MagicMock()
        if model_or_first == Claim:
            q.join.return_value.filter.return_value.order_by.return_value.all.return_value = [
                (c_proj_1, doc_a),
                (c_proj_2, doc_b),
            ]
            return q
        if model_or_first == Node:
            q.filter.return_value.all.return_value = [node_a, node_b]
            return q
        if model_or_first == Edge:
            q.filter.return_value.first.return_value = None
            return q
        return q

    mock_db_proj = MagicMock()
    mock_db_proj.query.side_effect = mock_query_dispatch

    proj_contradictions = detect_project_contradictions(mock_db_proj, tenant_id, project_id)
    assert len(proj_contradictions) == 1, f"Expected 1 contradiction for project claims, got {len(proj_contradictions)}"
    assert "gross NPA target" in proj_contradictions[0].description


def test_query_tools_abac_and_zero_metadata_leakage():
    """
    Test get_document_info and get_version_history under authorized and unauthorized contexts.
    Ensures zero metadata leakage when user lacks access.
    """
    tenant_id = uuid.uuid4()
    project_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    ver_id = uuid.uuid4()
    stage_id = uuid.uuid4()
    caller_user_id = uuid.uuid4()
    uploader_id = uuid.uuid4()
    approver_id = uuid.uuid4()

    mock_db = MagicMock()

    # 1. Unauthorized caller context: _resolve_visible_document returns None
    token = set_query_context(user_id=caller_user_id, project_id=project_id)
    try:
        with patch("app.tools.query_tools._scoped_session", return_value=mock_db):
            with patch("app.tools.query_tools._resolve_visible_document", return_value=(None, {"status": "not_found", "message": "Document not found or inaccessible."})):
                info_func = getattr(get_document_info, "entrypoint", get_document_info)
                info_result = info_func(document_reference="09_fraud_and_transaction_monitoring_framework.md")
                assert info_result.get("status") == "not_found"
                assert "uploader" not in info_result
                assert "approver" not in info_result

                history_func = getattr(get_version_history, "entrypoint", get_version_history)
                history_result = history_func(document_reference="09_fraud_and_transaction_monitoring_framework.md")
                assert history_result.get("status") == "not_found"
                assert "versions" not in history_result
    finally:
        reset_query_context(token)

    # 2. Authorized caller context: _resolve_visible_document returns document
    mock_doc = MagicMock(spec=Document)
    mock_doc.document_id = doc_id
    mock_doc.original_filename = "09_fraud_and_transaction_monitoring_framework.md"
    mock_doc.current_version_id = ver_id
    mock_doc.stage_id = stage_id
    mock_doc.uploaded_by = uploader_id
    mock_doc.uploaded_as_team_id = uuid.uuid4()
    mock_doc.project_id = project_id
    mock_doc.sensitivity_level = MagicMock(name="standard")
    mock_doc.sensitivity_level.name = "standard"
    mock_doc.created_at = None

    mock_ver = MagicMock(spec=DocumentVersion)
    mock_ver.version_id = ver_id
    mock_ver.version_number = 1
    mock_ver.uploaded_by = uploader_id
    mock_ver.approved_by = approver_id
    mock_ver.created_at = None
    mock_ver.approved_at = None
    mock_ver.provenance_event_id = uuid.uuid4()
    mock_ver.status = MagicMock(value="indexed")
    mock_ver.approval_outcome = MagicMock(value="approved")
    mock_ver.file_size_bytes = 4539

    mock_stage = MagicMock(spec=Stage)
    mock_stage.stage_id = stage_id
    mock_stage.name = "Risk & Compliance"
    mock_stage.requires_approval = True

    def mock_get(model_cls, entity_id):
        if model_cls == DocumentVersion:
            return mock_ver
        if model_cls == Stage:
            return mock_stage
        return None

    mock_db.get.side_effect = mock_get
    mock_db.execute.return_value.scalar_one_or_none.return_value = None

    token = set_query_context(user_id=caller_user_id, project_id=project_id)
    try:
        with patch("app.tools.query_tools._scoped_session", return_value=mock_db):
            with patch("app.tools.query_tools._resolve_visible_document", return_value=(mock_doc, None)):
                with patch("app.tools.query_tools._user_profile") as mock_user_prof:
                    mock_user_prof.side_effect = [
                        {"name": "Rohan Kapoor", "email": "rohan.kapoor@lendorafinancial.com", "role": "Risk Lead", "timestamp": "2026-07-22T10:00:00Z"},
                        {"name": "Neha Sharma", "email": "neha.sharma@lendorafinancial.com", "role": "Compliance Officer", "timestamp": "2026-07-22T12:00:00Z"},
                    ]
                    info_func = getattr(get_document_info, "entrypoint", get_document_info)
                    info_result = info_func(document_reference="09_fraud_and_transaction_monitoring_framework.md")
                    assert info_result.get("status") == "found"
                    assert info_result["uploader"]["name"] == "Rohan Kapoor"
                    assert info_result["approver"]["name"] == "Neha Sharma"
                    assert info_result["provenance_event_id"] == str(mock_ver.provenance_event_id)
    finally:
        reset_query_context(token)
