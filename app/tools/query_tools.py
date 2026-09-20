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

from sqlalchemy import select, text

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
    get_accessible_stages_for_user,
    has_permission,
    has_stage_access,
)
from app.services.access_requests_service import pending_requests_for_reviewer
from app.services.authorization_context import AuthorizationContext, build_authorization_context
from app.services.document_lookup import match_documents, normalize_ref, resolve_stage, resolve_team
from app.services.graph.audit_engine import execute_project_audit
from app.services.indexing import resolve_grounding_version
from app.services.pending_approvals import documents_awaiting_approval, reviews_project
from app.services.query_context import get_query_context

from agno.tools import tool

_TEAM_ROLE_RANK = {TeamRole.viewer: 0, TeamRole.contributor: 1, TeamRole.team_lead: 2}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _scoped_session(user_id: uuid.UUID):
    """
    Opens a fresh SessionLocal() and puts it in the same tenant scope
    app/tools/graph_tools.py's _resolve_caller_auth uses: `documents`,
    `projects`, `stages`, `teams`, etc. all FORCE ROW LEVEL SECURITY, so a
    session with no `app.current_tenant_id` GUC set sees zero rows in them
    (never an error — RLS silently filters everything out), which every one
    of this module's tools would otherwise misreport as "not_found"/"no
    access" instead of actually running the query. `users` is RLS-enabled
    but NOT forced, so the table owner (this app's DB role) can still read a
    user by id before any tenant is set — that's what bootstraps this.
    Raises PermissionError if user_id doesn't resolve to a real user.
    """
    db = SessionLocal()
    user = db.query(User).filter(User.user_id == user_id).first()
    if not user:
        db.close()
        raise PermissionError(f"Access denied: user {user_id} not found")
    db.info["tenant_id"] = str(user.tenant_id)
    db.execute(text("SET LOCAL app.current_tenant_id = :tid"), {"tid": str(user.tenant_id)})
    return db


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


def _user_profile(db, user_id: uuid.UUID | None, team_id: uuid.UUID | None = None, project_id: uuid.UUID | None = None) -> dict | None:
    if user_id is None:
        return None
    u = db.get(User, user_id)
    if not u:
        return None
    role_str = "member"
    if team_id is not None:
        m = db.execute(
            select(UserTeamMembership).where(
                UserTeamMembership.user_id == user_id,
                UserTeamMembership.team_id == team_id,
            )
        ).scalar_one_or_none()
        if m:
            role_str = m.role.value if hasattr(m.role, "value") else str(m.role)
    elif u.is_org_admin:
        role_str = "org_admin"
    return {
        "user_id": str(u.user_id),
        "name": u.full_name or u.email,
        "email": u.email,
        "role": role_str,
    }


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
                {
                    "filename": d.original_filename,
                    "stage": _stage_name(db, d.stage_id),
                }
                for d in visible
            ],
            "message": (
                f"Multiple documents match '{ref}'. Which one did you mean? "
                + ", ".join(f"'{d.original_filename}' ({_stage_name(db, d.stage_id)})" for d in visible)
            ),
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
    db = _scoped_session(ctx.user_id)
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

        current_v = None
        current_version = None
        if doc.current_version_id is not None:
            current_v = db.get(DocumentVersion, doc.current_version_id)
            if current_v is not None:
                current_version = current_v.version_number

        uploader = _user_profile(db, current_v.uploaded_by if current_v else doc.uploaded_by, doc.uploaded_as_team_id, doc.project_id)
        if uploader:
            uploader["uploaded_at"] = (current_v.created_at if current_v else doc.created_at).isoformat() if (current_v and current_v.created_at or doc.created_at) else None

        approver = None
        approver_id = (current_v.approved_by if current_v else None) or (wf.approved_by if wf and wf.state == WorkflowStatus.approved else None)
        if approver_id:
            approver = _user_profile(db, approver_id, doc.uploaded_as_team_id, doc.project_id)
            if approver:
                approved_time = (current_v.approved_at if current_v else None) or (wf.approval_timestamp if wf else None)
                approver["approved_at"] = approved_time.isoformat() if approved_time else None

        return {
            "status": "found",
            "document": doc.original_filename,
            "uploader": uploader,
            "uploaded_by": uploader["email"] if uploader else _email(db, doc.uploaded_by),
            "approver": approver,
            "approval_status": approval_status,
            "sensitivity": doc.sensitivity_level.name,
            "team": _team_name(db, doc.uploaded_as_team_id),
            "stage": _stage_name(db, doc.stage_id),
            "uploaded_at": doc.created_at.isoformat() if doc.created_at else None,
            "current_version": current_version,
            "provenance_event_id": str(current_v.provenance_event_id) if current_v and current_v.provenance_event_id else None,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. get_version_history
# ---------------------------------------------------------------------------

@tool
def get_version_history(document_reference: str, stage_reference: str | None = None) -> dict:
    """Version history of ONE named document: how many versions, each one's
    created_at and status. Reports TWO distinct notions, never conflate
    them when relaying this: "latest" is the newest version regardless of
    its state (could be an unapproved draft or a rejected attempt);
    "live" is the version actually in effect — approved and what Search
    Agent answers/audits are grounded in. These can be different versions
    at once (a newer draft can be "latest" while an older approved version
    is still "live"). `document_reference` is the title/filename.
    `stage_reference` is an optional stage name or stage UUID. "not_found" /
    "ambiguous" behave as in get_document_info.
    """
    ctx = get_query_context()
    db = _scoped_session(ctx.user_id)
    try:
        doc, err = _resolve_visible_document(db, ctx.project_id, ctx.user_id, document_reference, stage_reference)
        if err:
            return err

        # Ordered by created_at, not version_number: version_number is now
        # only assigned once a version is APPROVED (None until then), so it
        # no longer reflects true chronological order among unresolved
        # drafts/rejections sitting between approved versions.
        versions = db.execute(
            select(DocumentVersion)
            .where(DocumentVersion.document_id == doc.document_id)
            .order_by(DocumentVersion.created_at.asc())
        ).scalars().all()

        latest_version = None
        if doc.current_version_id is not None:
            cv = db.get(DocumentVersion, doc.current_version_id)
            if cv is not None:
                latest_version = cv.version_number

        # The version actually grounding Search Agent answers and audit
        # rules — can differ from "latest" (document.current_version_id)
        # when a newer draft/rejection sits unresolved on top of the last
        # genuinely approved version. Deliberately reported separately, not
        # collapsed into one "current" concept.
        live = resolve_grounding_version(db, doc.document_id)

        version_items = []
        for v in versions:
            v_uploader = _user_profile(db, v.uploaded_by, doc.uploaded_as_team_id, doc.project_id)
            v_approver = None
            if v.approved_by:
                v_approver = _user_profile(db, v.approved_by, doc.uploaded_as_team_id, doc.project_id)
                if v_approver and v.approved_at:
                    v_approver["approved_at"] = v.approved_at.isoformat()

            version_items.append({
                "version": v.version_number,
                "approval_outcome": v.approval_outcome.value if v.approval_outcome else None,
                "created_at": v.created_at.isoformat() if v.created_at else None,
                "status": v.status.value,
                "uploader": v_uploader,
                "approver": v_approver,
                "provenance_event_id": str(v.provenance_event_id) if v.provenance_event_id else None,
                "is_latest": v.version_id == doc.current_version_id,
                "is_live": live is not None and v.version_id == live.version_id,
            })

        return {
            "status": "found",
            "document": doc.original_filename,
            "version_count": len(versions),
            "latest_version": latest_version,
            "live_version": live.version_number if live is not None else None,
            "versions": version_items,
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
    db = _scoped_session(ctx.user_id)
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
    db = _scoped_session(ctx.user_id)
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
    db = _scoped_session(ctx.user_id)
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
    whether it requires approval) and the teams in it. Takes NO arguments —
    not even a stage. For a stage's document CHECKLIST/requirements, use
    get_stage_requirements instead (its `stage_reference` argument is
    optional: omit it for a project-wide requirements list across every
    stage, or pass one to scope to a single stage).
    """
    ctx = get_query_context()
    db = _scoped_session(ctx.user_id)
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
def get_stage_requirements(stage_reference: str | None = None) -> dict:
    """Checklist requirements for this project from the required_documents
    table: document names, descriptions, and which are mandatory.
    `stage_reference` (a stage name or UUID) is OPTIONAL — omit it (or pass
    an empty string) for a project-wide requirements list grouped by every
    stage; pass it to scope the list to one specific stage.
    Status "not_found" if `stage_reference` is given but matches no stage.
    """
    ctx = get_query_context()
    db = _scoped_session(ctx.user_id)
    try:
        ref = (stage_reference or "").strip()

        if not ref:
            stages = db.execute(
                select(Stage)
                .where(Stage.project_id == ctx.project_id, Stage.deleted_at.is_(None))
                .order_by(Stage.order_index.asc())
            ).scalars().all()
            by_stage = []
            for stage in stages:
                reqs = db.execute(
                    select(RequiredDocument)
                    .where(RequiredDocument.stage_id == stage.stage_id)
                    .order_by(RequiredDocument.created_at.asc())
                ).scalars().all()
                by_stage.append({
                    "stage": stage.name,
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
                })
            return {"status": "ok", "scope": "entire project", "stages": by_stage}

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
    """Document completeness status for ONE SPECIFIC stage: which required
    documents exist, which are missing, coverage percentage, and satisfied
    mandatory items. `stage_reference` (a stage name or UUID) is required —
    for a project-wide readiness picture instead, use get_project_gaps.
    Status "not_found" if no matching stage exists in this project.
    """
    ctx = get_query_context()
    db = _scoped_session(ctx.user_id)
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
    db = _scoped_session(ctx.user_id)
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


# ---------------------------------------------------------------------------
# 10. get_project_readiness
# ---------------------------------------------------------------------------

@tool
def get_project_readiness(stage_reference: str | None = None) -> dict:
    """Whether this project (or one stage of it, if `stage_reference` is given)
    is READY or NOT_READY for launch/gate, its completeness score, and the
    top blocking issues from the Intelligence audit — the exact data behind
    the Intelligence dashboard's "Project Readiness & Gate Status" panel.
    Call this for questions like "why is this project not ready", "is this
    project ready to launch", "what's blocking us", "how complete are we".
    """
    ctx = get_query_context()
    db = _scoped_session(ctx.user_id)
    try:
        stage_id = None
        if stage_reference:
            stage_id = resolve_stage(db, ctx.project_id, stage_reference)
            if stage_id is None:
                return {
                    "status": "not_found",
                    "message": f"No stage matching '{stage_reference}' exists in this project.",
                }

        accessible_stages = set(get_accessible_stages_for_user(db, ctx.user_id, ctx.project_id))
        if stage_id is not None and stage_id not in accessible_stages:
            return {"status": "not_found", "message": f"No stage matching '{stage_reference}' exists in this project."}

        audit_run = execute_project_audit(db, ctx.project_id, target_stage_id=stage_id)
        visible_findings = [
            f for f in audit_run.findings
            if f.target_stage_id is None or f.target_stage_id in accessible_stages
        ]
        visible_blockers = [f for f in visible_findings if f.is_blocker]
        readiness_status = "READY" if len(visible_blockers) == 0 else "NOT_READY"

        return {
            "status": "ok",
            "scope": f"stage '{stage_reference}'" if stage_id else "entire project",
            "readiness_status": readiness_status,
            "completeness_score": f"{audit_run.completeness_score}%",
            "total_blockers": len(visible_blockers),
            "top_blockers": [
                {
                    "rule_code": b.rule_code,
                    "title": b.title,
                    "description": b.description,
                }
                for b in visible_blockers[:5]
            ],
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 11. get_project_gaps
# ---------------------------------------------------------------------------

@tool
def get_project_gaps(stage_reference: str | None = None, document_reference: str | None = None) -> dict:
    """The specific compliance gaps behind "What needs attention" on the
    Intelligence dashboard: missing mandatory requirements, unapproved gate
    documents, broken dependencies, stale references, document
    contradictions, document-coherence issues (duplicate content, unmet
    requirements, and content contradictions found within a single
    document), and Scanner-flagged versions. Optionally scoped to one stage
    via `stage_reference` and/or narrowed to findings about ONE specific
    document via `document_reference` (filename or document id) — use
    `document_reference` whenever the user is asking about a particular
    document's own flagged issue rather than the whole project/stage, so
    the answer only covers that document instead of everything else also
    flagged in the same stage. Call this for questions like "what needs
    attention", "what's missing", "are there any contradictions", "what
    findings does the audit have", or "why is <document> flagged".
    """
    ctx = get_query_context()
    db = _scoped_session(ctx.user_id)
    try:
        stage_id = None
        if stage_reference:
            stage_id = resolve_stage(db, ctx.project_id, stage_reference)
            if stage_id is None:
                return {
                    "status": "not_found",
                    "message": f"No stage matching '{stage_reference}' exists in this project.",
                }

        accessible_stages = set(get_accessible_stages_for_user(db, ctx.user_id, ctx.project_id))
        if stage_id is not None and stage_id not in accessible_stages:
            return {"status": "not_found", "message": f"No stage matching '{stage_reference}' exists in this project."}

        target_document_id = None
        if document_reference:
            matches = match_documents(db, ctx.project_id, document_reference, stage_id=stage_id)
            visible_matches = [d for d in matches if can_view_document(db, ctx.user_id, d)]
            if not visible_matches:
                return {
                    "status": "not_found",
                    "message": f"No document matching '{document_reference}' exists in this project.",
                }
            if len(visible_matches) > 1:
                return {
                    "status": "ambiguous",
                    "message": f"Multiple documents match '{document_reference}': "
                    + ", ".join(d.original_filename for d in visible_matches),
                }
            target_document_id = visible_matches[0].document_id

        audit_run = execute_project_audit(db, ctx.project_id, target_stage_id=stage_id)
        visible_findings = [
            f for f in audit_run.findings
            if f.target_stage_id is None or f.target_stage_id in accessible_stages
        ]
        if target_document_id is not None:
            visible_findings = [f for f in visible_findings if f.affected_entity_id == target_document_id]

        visible_blockers = [f for f in visible_findings if f.is_blocker]
        readiness_status = "READY" if len(visible_blockers) == 0 else "NOT_READY"

        if target_document_id is not None:
            scope = f"document '{document_reference}'" + (f" in stage '{stage_reference}'" if stage_id else "")
        elif stage_id:
            scope = f"stage '{stage_reference}'"
        else:
            scope = "entire project"

        return {
            "status": "ok",
            "scope": scope,
            "readiness_status": readiness_status,
            "missing_mandatory_requirements": [f.description for f in visible_findings if f.rule_code == "R001"],
            "unapproved_gate_documents": [f.description for f in visible_findings if f.rule_code in ("R002", "R008")],
            "broken_dependencies": [f.description for f in visible_findings if f.rule_code in ("R003", "R005")],
            "stale_references": [f.description for f in visible_findings if f.rule_code == "R004"],
            "document_contradictions": [f.description for f in visible_findings if f.rule_code == "R007"],
            "document_coherence_issues": [f.description for f in visible_findings if f.rule_code == "R009"],
            "scanner_flagged_versions": [f.description for f in visible_findings if f.rule_code == "R010"],
        }
    finally:
        db.close()


