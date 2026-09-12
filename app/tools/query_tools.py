"""
Tools for the Query Agent — read-only metadata Q&A about documents and the
project. Every tool is a direct Postgres lookup; NONE of them touch vector
search, generation, or any write/action path.

WHO is asking and WHICH project is NOT a tool argument — it comes from
app/services/query_context (set per-turn by run_query_turn), so a chat
message can never make a lookup act as another user or in another project.
The only thing the LLM supplies is a free-form document / stage / team
reference.

Access control is REUSED, never reimplemented:
  * can_view_document() / classify_document_visibility() — per-document ABAC
  * has_permission() / has_stage_access() — role + stage-access checks
  * access_requests_service.pending_requests_for_reviewer() — the exact
    /access-requests/pending query
  * pending_approvals.documents_awaiting_approval() / reviews_project() — the
    exact Admin → Pending Approvals tab queries

Each tool returns a dict with a "status" field. A document the caller cannot
see is reported as "not_found" with no hint that it exists.
"""

import os
import re
import uuid

from sqlalchemy import select

from app.database import SessionLocal
from app.models.document import Document, DocumentVersion
from app.models.project import Project
from app.models.required_document import RequiredDocument
from app.models.stage import Stage, TeamStageAccess
from app.models.team import Team, TeamRole, UserTeamMembership
from app.models.user import User
from app.models.workflow import WorkflowState
from app.services.access_control import (
    DocumentVisibility,
    _get_team_membership,
    _is_org_admin,
    _is_project_admin,
    build_access_filter,
    can_view_document,
    classify_documents_visibility,
    has_permission,
    has_stage_access,
)
from app.services.access_requests_service import pending_requests_for_reviewer
from app.services.authorization_context import AuthorizationContext, build_authorization_context
from app.services.document_lookup import match_documents, normalize_ref, resolve_stage, resolve_team
from app.services.pending_approvals import documents_awaiting_approval, reviews_project
from app.services.query_context import get_query_context

from agno.tools import tool

_TEAM_ROLE_RANK = {TeamRole.viewer: 0, TeamRole.contributor: 1, TeamRole.team_lead: 2}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _email(db, user_id: uuid.UUID | None) -> str | None:
    if user_id is None:
        return None
    u = db.get(User, user_id)
    return u.email if u else None


def _team_name(db, team_id: uuid.UUID | None) -> str | None:
    if team_id is None:
        return None
    t = db.get(Team, team_id)
    return t.name if t else None


def _stage_name(db, stage_id: uuid.UUID | None) -> str | None:
    if stage_id is None:
        return None
    s = db.get(Stage, stage_id)
    return s.name if s else None


def _team_leads(db, team_id: uuid.UUID) -> list[str]:
    rows = db.execute(
        select(UserTeamMembership).where(
            UserTeamMembership.team_id == team_id,
            UserTeamMembership.role == TeamRole.team_lead,
        )
    ).scalars().all()
    return sorted(e for e in (_email(db, r.user_id) for r in rows) if e)


def _resolve_visible_document(db, project_id, user_id, ref: str, stage_ref: str | None = None):
    """
    (doc_or_None, error_dict_or_None). A document the caller cannot see comes
    back as ("not_found") with no existence hint — same as summarize_document.
    """
    stage_id = None
    if stage_ref:
        stage_id = resolve_stage(db, project_id, stage_ref)

    candidates = match_documents(db, project_id, ref, stage_id=stage_id)
    visible = [d for d in candidates if can_view_document(db, user_id, d)]
    if not visible:
        return None, {
            "status": "not_found",
            "message": f"I can't find a document matching '{ref}'.",
        }
    if len(visible) > 1:
        return None, {
            "status": "ambiguous",
            "matches": [
                f"{d.original_filename} (stage: '{_stage_name(db, d.stage_id)}')"
                for d in visible
            ],
            "message": "Multiple documents match that reference across different stages — ask the user which stage they mean.",
        }
    return visible[0], None


def _teams_for_stage(db, stage_id: uuid.UUID) -> list[Team]:
    team_ids = db.execute(
        select(TeamStageAccess.team_id).where(TeamStageAccess.stage_id == stage_id)
    ).scalars().all()
    return [t for t in (db.get(Team, tid) for tid in set(team_ids)) if t is not None]


# ---------------------------------------------------------------------------
# 1. get_document_info
# ---------------------------------------------------------------------------

@tool
def get_document_info(document_reference: str, stage_reference: str | None = None) -> dict:
    """Metadata for ONE named document: uploader, approval status, sensitivity,
    team, stage, upload date. `document_reference` is the title/filename/ID as the
    user said it. `stage_reference` is an optional stage name or stage UUID to
    disambiguate when identical filenames exist in different stages. Status "not_found"
    also covers a document the user may not see — relay it as "can't find it", don't speculate.
    "ambiguous" -> ask which.
    """
    ctx = get_query_context()
    db = SessionLocal()
    try:
        doc, err = _resolve_visible_document(db, ctx.project_id, ctx.user_id, document_reference, stage_reference)
        if err:
            return err

        wf = db.execute(
            select(WorkflowState).where(WorkflowState.document_id == doc.document_id)
        ).scalar_one_or_none()
        if wf is not None:
            approval_status = wf.state.value
        else:
            stage = db.get(Stage, doc.stage_id)
            approval_status = (
                "no approval required (this stage does not require sign-off)"
                if stage is None or not stage.requires_approval
                else "not yet submitted for approval"
            )

        current_version = None
        if doc.current_version_id is not None:
            v = db.get(DocumentVersion, doc.current_version_id)
            if v is not None:
                current_version = v.version_number

        return {
            "status": "found",
            "document": doc.original_filename,
            "uploaded_by": _email(db, doc.uploaded_by),
            "approval_status": approval_status,
            "sensitivity": doc.sensitivity_level.name,
            "team": _team_name(db, doc.uploaded_as_team_id),
            "stage": _stage_name(db, doc.stage_id),
            "uploaded_at": doc.created_at.isoformat() if doc.created_at else None,
            "current_version": current_version,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. get_version_history
# ---------------------------------------------------------------------------

@tool
def get_version_history(document_reference: str, stage_reference: str | None = None) -> dict:
    """Version history of ONE named document: how many versions, each one's
    created_at and status, and which is current. `document_reference` is the
    title/filename. `stage_reference` is an optional stage name or stage UUID.
    "not_found" / "ambiguous" behave as in get_document_info.
    """
    ctx = get_query_context()
    db = SessionLocal()
    try:
        doc, err = _resolve_visible_document(db, ctx.project_id, ctx.user_id, document_reference, stage_reference)
        if err:
            return err

        versions = db.execute(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == doc.document_id)
            .order_by(DocumentVersion.version_number.asc())
        ).scalars().all()

        current_version = None
        if doc.current_version_id is not None:
            cv = db.get(DocumentVersion, doc.current_version_id)
            if cv is not None:
                current_version = cv.version_number

        return {
            "status": "found",
            "document": doc.original_filename,
            "version_count": len(versions),
            "current_version": current_version,
            "versions": [
                {
                    "version": v.version_number,
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                    "status": v.status.value,
                    "is_current": v.version_id == doc.current_version_id,
                }
                for v in versions
            ],
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. who_can_approve
# ---------------------------------------------------------------------------

@tool
def who_can_approve(stage_or_team_reference: str) -> dict:
    """Who can approve work for a team or stage — the team lead(s), by email.
    `stage_or_team_reference` is a team or stage name. If it's a stage that
    doesn't require approval, status is "stage_no_approval" — say so plainly
    instead of naming anyone. "not_found" if it matches no team or stage.
    """
    ctx = get_query_context()
    db = SessionLocal()
    try:
        ref = (stage_or_team_reference or "").strip()

        team_id = resolve_team(db, ctx.project_id, ref)
        if team_id is not None:
            return {
                "status": "team_leads",
                "scope": "team",
                "team": _team_name(db, team_id),
                "approvers": _team_leads(db, team_id),
            }

        stage_id = resolve_stage(db, ctx.project_id, ref)
        if stage_id is not None:
            stage = db.get(Stage, stage_id)
            if not stage.requires_approval:
                return {"status": "stage_no_approval", "stage": stage.name}
            teams = _teams_for_stage(db, stage_id)
            return {
                "status": "stage_teams",
                "stage": stage.name,
                "teams": [
                    {"team": t.name, "approvers": _team_leads(db, t.team_id)}
                    for t in sorted(teams, key=lambda t: t.name)
                ],
            }

        return {
            "status": "not_found",
            "message": f"No team or stage matching '{ref}' exists in this project.",
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. list_pending_approvals
# ---------------------------------------------------------------------------

@tool
def list_pending_approvals() -> dict:
    """What is awaiting THIS user's review in this project right now — the
    contents of their Pending Approvals tab: documents at 'pending_review' plus
    confidential-access requests they can decide. No arguments. Status
    "nothing_to_review" means no review role here, or nothing is pending.
    """
    ctx = get_query_context()
    db = SessionLocal()
    try:
        user = db.get(User, ctx.user_id)
        tenant_id = user.tenant_id if user else None

        if not reviews_project(db, ctx.user_id, ctx.project_id):
            return {
                "status": "nothing_to_review",
                "message": "You don't have a review role in this project, so nothing is awaiting your approval here.",
            }

        docs = documents_awaiting_approval(db, ctx.user_id, ctx.project_id)
        reqs = pending_requests_for_reviewer(
            db, reviewer_id=ctx.user_id, tenant_id=tenant_id, project_id=ctx.project_id
        )

        if not docs and not reqs:
            return {
                "status": "nothing_to_review",
                "message": "Nothing is awaiting your approval in this project right now.",
            }

        return {
            "status": "ok",
            "document_count": len(docs),
            "access_request_count": len(reqs),
            "documents": [
                {
                    "document": d.original_filename,
                    "stage": _stage_name(db, d.stage_id),
                    "team": _team_name(db, d.uploaded_as_team_id),
                    "sensitivity": d.sensitivity_level.name,
                }
                for d in docs
            ],
            "access_requests": [
                {
                    "requester": _email(db, r.user_id),
                    "team": _team_name(db, r.team_id),
                    "requested_at": r.requested_at.isoformat() if r.requested_at else None,
                }
                for r in reqs
            ],
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 5. check_my_access
# ---------------------------------------------------------------------------

@tool
def check_my_access(stage_or_team_reference: str) -> dict:
    """"Am I allowed to see / upload to X" for a team or stage, from a direct
    permission check (never inferred). `stage_or_team_reference` is a team or
    stage name. Returns can_view / can_upload booleans (for a stage, also the
    team that grants it). "not_found" if it matches no team or stage.
    """
    ctx = get_query_context()
    db = SessionLocal()
    try:
        ref = (stage_or_team_reference or "").strip()

        team_id = resolve_team(db, ctx.project_id, ref)
        if team_id is not None:
            return {
                "status": "team_access",
                "team": _team_name(db, team_id),
                "can_view": has_permission(db, ctx.user_id, "view", team_id, ctx.project_id),
                "can_upload": has_permission(db, ctx.user_id, "upload", team_id, ctx.project_id),
            }

        stage_id = resolve_stage(db, ctx.project_id, ref)
        if stage_id is not None:
            stage = db.get(Stage, stage_id)
            if _is_org_admin(db, ctx.user_id) or _is_project_admin(db, ctx.user_id, ctx.project_id):
                return {
                    "status": "stage_access",
                    "stage": stage.name,
                    "can_view": True,
                    "can_upload": True,
                    "via_team": None,
                }
            view_team = None
            upload_team = None
            memberships = db.execute(
                select(UserTeamMembership).where(
                    UserTeamMembership.user_id == ctx.user_id,
                    UserTeamMembership.project_id == ctx.project_id,
                )
            ).scalars().all()
            for m in memberships:
                if has_stage_access(db, ctx.user_id, m.team_id, stage_id, ctx.project_id):
                    if view_team is None:
                        view_team = m.team_id
                    if _TEAM_ROLE_RANK[m.role] >= _TEAM_ROLE_RANK[TeamRole.contributor]:
                        upload_team = m.team_id
                        break
            grant_team = upload_team or view_team
            return {
                "status": "stage_access",
                "stage": stage.name,
                "can_view": view_team is not None,
                "can_upload": upload_team is not None,
                "via_team": _team_name(db, grant_team),
            }

        return {
            "status": "not_found",
            "message": f"No team or stage matching '{ref}' exists in this project.",
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 6. get_project_structure
# ---------------------------------------------------------------------------

@tool
def get_project_structure() -> dict:
    """Structural overview of this project: its stages in order (each with
    whether it requires approval) and the teams in it. No arguments.
    """
    ctx = get_query_context()
    db = SessionLocal()
    try:
        project = db.get(Project, ctx.project_id)
        stages = db.execute(
            select(Stage)
            .where(Stage.project_id == ctx.project_id, Stage.deleted_at.is_(None))
            .order_by(Stage.order_index.asc())
        ).scalars().all()
        teams = db.execute(
            select(Team).where(Team.project_id == ctx.project_id).order_by(Team.name.asc())
        ).scalars().all()
        return {
            "status": "ok",
            "project": project.name if project else None,
            "stages": [
                {"name": s.name, "requires_approval": s.requires_approval} for s in stages
            ],
            "teams": [t.name for t in teams],
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 7. get_stage_requirements
# ---------------------------------------------------------------------------

@tool
def get_stage_requirements(stage_reference: str) -> dict:
    """Checklist requirements for a specific stage from the required_documents
    table: document names, descriptions, and which are mandatory.
    `stage_reference` is a stage name or UUID.
    Status "not_found" if no matching stage exists in this project.
    """
    ctx = get_query_context()
    db = SessionLocal()
    try:
        ref = (stage_reference or "").strip()
        stage_id = resolve_stage(db, ctx.project_id, ref)
        if stage_id is None:
            return {
                "status": "not_found",
                "message": f"No stage matching '{ref}' exists in this project.",
            }

        stage = db.get(Stage, stage_id)
        reqs = db.execute(
            select(RequiredDocument)
            .where(RequiredDocument.stage_id == stage_id)
            .order_by(RequiredDocument.created_at.asc())
        ).scalars().all()

        return {
            "status": "ok",
            "stage": stage.name if stage else ref,
            "requirements": [
                {
                    "name": r.name,
                    "description": r.description or "",
                    "mandatory": r.is_mandatory,
                }
                for r in reqs
            ],
            "mandatory_count": sum(1 for r in reqs if r.is_mandatory),
            "total_count": len(reqs),
        }
    finally:
        db.close()


_NEGATIVE_QUALIFIERS = {"notes", "meeting", "minutes", "summary", "template", "draft", "scratch", "review"}


def _clean_stem(filename: str) -> str:
    stem, _ = os.path.splitext(filename)
    return stem.strip()


def _tokenize(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-zA-Z0-9]+", text.lower()) if t]


def match_document_to_requirement(req_name: str, filename: str) -> tuple[bool, str | None]:
    """
    Deterministic requirement satisfaction matcher.
    Hierarchy:
      1. Exact normalized stem match (e.g. 'Software Requirements Specification.pdf' == 'Software Requirements Specification')
      2. Exact stem ignoring trailing version / status tags (e.g. 'Software Requirements Specification v1.0.md')
      3. Contiguous whole phrase containment (requirement tokens appear in contiguous sequence in doc tokens),
         with negative qualifier guard: reject matches if filename introduces negative qualifiers ('notes', 'meeting', 'minutes', etc.)
         not present in the requirement definition.
      4. Acronym token match (for requirements with 3+ words, e.g. 'Software Requirements Specification' -> 'srs')
         provided negative qualifiers are not introduced.
      5. Otherwise unmatched.

    Never allows doc tokens in requirement tokens (a short generic filename cannot satisfy a specific requirement).
    """
    req_tokens = _tokenize(req_name)
    if not req_tokens:
        return False, None

    doc_stem = _clean_stem(filename)
    doc_tokens = _tokenize(doc_stem)
    if not doc_tokens:
        return False, None

    # 1. Exact normalized stem
    if req_tokens == doc_tokens:
        return True, "exact_stem"

    # 2. Exact stem ignoring trailing version / status tags
    cleaned_doc_tokens = [
        t for t in doc_tokens
        if not re.match(r"^v?\d+(\.\d+)*$", t) and t not in {"final", "approved"}
    ]
    if cleaned_doc_tokens == req_tokens:
        return True, "exact_stem_version_ignored"

    # Check for negative qualifiers introduced by the document
    req_token_set = set(req_tokens)
    doc_token_set = set(doc_tokens)
    extra_tokens = doc_token_set - req_token_set
    introduced_negative = _NEGATIVE_QUALIFIERS.intersection(extra_tokens)
    if introduced_negative:
        # Document is notes, meeting minutes, summary, etc. when requirement isn't
        return False, None

    # 3. Contiguous phrase containment
    req_len = len(req_tokens)
    doc_len = len(doc_tokens)
    if doc_len >= req_len:
        for i in range(doc_len - req_len + 1):
            if doc_tokens[i : i + req_len] == req_tokens:
                return True, "contained_phrase"

    # 4. Standard acronym match (for 3+ word requirements, e.g. SRS)
    if req_len >= 3:
        acronym = "".join(t[0] for t in req_tokens)
        if len(acronym) >= 3 and acronym in doc_tokens:
            return True, "acronym_match"

    return False, None


# ---------------------------------------------------------------------------
# 8. get_stage_document_status
# ---------------------------------------------------------------------------

@tool
def get_stage_document_status(stage_reference: str) -> dict:
    """Document completeness status for a stage: which required documents
    exist, which are missing, coverage percentage, and satisfied mandatory items.
    `stage_reference` is a stage name or UUID.
    Status "not_found" if no matching stage exists in this project.
    """
    ctx = get_query_context()
    db = SessionLocal()
    try:
        ref = (stage_reference or "").strip()
        stage_id = resolve_stage(db, ctx.project_id, ref)
        if stage_id is None:
            return {
                "status": "not_found",
                "message": f"No stage matching '{ref}' exists in this project.",
            }

        stage = db.get(Stage, stage_id)
        reqs = db.execute(
            select(RequiredDocument)
            .where(RequiredDocument.stage_id == stage_id)
            .order_by(RequiredDocument.created_at.asc())
        ).scalars().all()

        # Batch-load visible documents using AuthorizationContext
        auth_ctx = build_authorization_context(db, ctx.user_id, ctx.project_id)
        stage_docs = db.execute(
            select(Document).where(
                Document.project_id == ctx.project_id,
                Document.stage_id == stage_id,
                Document.tenant_id == auth_ctx.tenant_id,
            )
        ).scalars().all()

        doc_ids = [d.document_id for d in stage_docs]
        vis_map = classify_documents_visibility(db, auth_ctx, doc_ids)
        visible_docs = [
            d for d in stage_docs
            if vis_map.get(d.document_id) == DocumentVisibility.fully_allowed
        ]
        doc_names = [d.original_filename for d in visible_docs]

        satisfied_requirements = []
        missing_requirements = []
        missing_mandatory = []
        satisfied_mandatory_count = 0
        total_mandatory_count = sum(1 for r in reqs if r.is_mandatory)
        requirements_details = []

        for r in reqs:
            matched_doc = None
            matched_rule = None
            for d in visible_docs:
                is_match, rule = match_document_to_requirement(r.name, d.original_filename)
                if is_match:
                    matched_doc = d.original_filename
                    matched_rule = rule
                    break

            if matched_doc:
                satisfied_requirements.append(r.name)
                if r.is_mandatory:
                    satisfied_mandatory_count += 1
                requirements_details.append({
                    "requirement": r.name,
                    "mandatory": r.is_mandatory,
                    "status": "satisfied",
                    "satisfied_by": matched_doc,
                    "match_rule": matched_rule,
                })
            else:
                missing_requirements.append(r.name)
                if r.is_mandatory:
                    missing_mandatory.append(r.name)
                requirements_details.append({
                    "requirement": r.name,
                    "mandatory": r.is_mandatory,
                    "status": "missing",
                    "satisfied_by": None,
                    "match_rule": None,
                })

        if total_mandatory_count > 0:
            coverage = round((satisfied_mandatory_count / total_mandatory_count) * 100.0, 1)
        elif len(reqs) > 0:
            coverage = round((len(satisfied_requirements) / len(reqs)) * 100.0, 1)
        else:
            coverage = 100.0

        return {
            "status": "ok",
            "stage": stage.name if stage else ref,
            "coverage_percentage": coverage,
            "total_requirements": len(reqs),
            "mandatory_requirements_count": total_mandatory_count,
            "satisfied_mandatory_count": satisfied_mandatory_count,
            "missing_mandatory": missing_mandatory,
            "satisfied_documents": satisfied_requirements,
            "missing_documents": missing_requirements,
            "uploaded_documents_in_stage": doc_names,
            "requirements_details": requirements_details,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 9. list_accessible_documents
# ---------------------------------------------------------------------------

@tool
def list_accessible_documents(stage_reference: str | None = None) -> dict:
    """Lists all documents in this project that the user is authorized to view
    under their current team membership, clearance level, and stage access.
    Optionally filters by `stage_reference` (stage name or UUID). Call this whenever
    the user asks 'what documents do I have access to', 'which documents can I see',
    or asks for a list of project documents.
    """
    ctx = get_query_context()
    db = SessionLocal()
    try:
        auth_ctx = build_authorization_context(db, ctx.user_id, ctx.project_id)
        query = db.query(Document).filter(
            Document.project_id == ctx.project_id,
            Document.tenant_id == auth_ctx.tenant_id,
        )

        stage_name = None
        if stage_reference:
            stage_id = resolve_stage(db, ctx.project_id, stage_reference)
            if stage_id is not None:
                query = query.filter(Document.stage_id == stage_id)
                stg = db.get(Stage, stage_id)
                stage_name = stg.name if stg else str(stage_id)

        all_docs = query.order_by(Document.created_at.desc()).all()
        doc_ids = [d.document_id for d in all_docs]
        vis_map = classify_documents_visibility(db, auth_ctx, doc_ids)

        visible = [d for d in all_docs if vis_map.get(d.document_id) == DocumentVisibility.fully_allowed]

        return {
            "status": "ok",
            "scope": f"stage '{stage_name}'" if stage_name else "entire project",
            "count": len(visible),
            "documents": [
                {
                    "name": d.original_filename,
                    "stage": _stage_name(db, d.stage_id),
                    "sensitivity": d.sensitivity_level.name,
                    "uploaded_by": _email(db, d.uploaded_by),
                    "uploaded_at": d.created_at.isoformat() if d.created_at else None,
                }
                for d in visible
            ],
        }
    finally:
        db.close()


