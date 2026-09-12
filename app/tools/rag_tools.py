"""
Tools for the RAG Agent — thin @tool wrappers over the retrieval pipeline
(app/services/rag/retrieval.retrieve), the direct generation/summarisation
calls (app/services/rag/generation), the ABAC document check
(app/services/access_control), and the confidential-access request service
(app/services/access_requests_service).

WHO is asking and WHICH project is NOT a tool argument — it comes from
app/services/rag_context (set per-turn by run_rag_turn), so a chat message
can never make a tool act as another user or in another project. The only
things the LLM supplies are the query text, an optional stage name, a
document reference, and a team reference — never an identity, never a
document_id, never a raw access decision.

Each tool returns a dict with a "status" field. The agent relays these
verbatim in spirit — it must not invent an answer, a summary, a citation, or
a confirmation that a tool did not return.
"""

import time
import uuid

from app.database import SessionLocal
from app.models.document import Document, DocumentTeamVisibility, DocumentVersion
from app.models.stage import Stage
from app.models.team import Team, UserTeamMembership
from app.models.user import User
from app.services.access_control import (
    DocumentVisibility,
    _get_team_membership,
    classify_document_visibility,
)
from app.services.access_requests_service import (
    AccessRequestError,
    request_confidential_access as _request_confidential_access,
)
from app.services.document_lookup import (
    match_documents as _match_documents,
    resolve_stage as _resolve_stage,
    resolve_team as _resolve_team,
)
from app.services.rag.generation import GenerationError, generate_answer, summarize_full_document
from app.services.rag.retrieval import NoProjectAccessError, retrieve
from app.services.rag_context import get_rag_context

from agno.tools import tool


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _stage_name_map(db, project_id: uuid.UUID, stage_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not stage_ids:
        return {}
    rows = db.query(Stage).filter(
        Stage.project_id == project_id, Stage.stage_id.in_(stage_ids)
    ).all()
    return {s.stage_id: s.name for s in rows}


def _requestable_teams_for_docs(db, user_id: uuid.UUID, document_ids: list[uuid.UUID]) -> list[dict]:
    """
    For documents the user was blocked from BY SENSITIVITY, the team to
    request access on is the one the document is visible to AND the user
    already belongs to (that's exactly the team classify_document_visibility
    found them a member of). Returns unique [{"team_id", "team_name"}].
    """
    out: dict[uuid.UUID, str] = {}
    for doc_id in document_ids:
        visible_team_ids = [
            r.team_id for r in db.query(DocumentTeamVisibility).filter(
                DocumentTeamVisibility.document_id == doc_id
            )
        ]
        for team_id in visible_team_ids:
            if team_id in out:
                continue
            if _get_team_membership(db, user_id, team_id) is not None:
                team = db.get(Team, team_id)
                if team is not None:
                    out[team_id] = team.name
    return [{"team_id": str(k), "team_name": v} for k, v in out.items()]


# ---------------------------------------------------------------------------
# 1. search_documents — grounded Q&A
# ---------------------------------------------------------------------------

@tool(stop_after_tool_call=True)
def search_documents(query: str, stage_id: str | None = None) -> str:
    """
    Answer a question from the project's indexed documents. This is the tool
    for ANY genuine information question ("what does X say", "how do we do Y",
    "what was decided about Z").

    It retrieves the most relevant approved document chunks the CURRENT USER
    is allowed to see, then generates a cited answer from ONLY those chunks.
    Every claim carries a citation naming the source and its stage. It never
    uses outside knowledge.

    The string this returns is already the final, user-ready answer (grounded,
    cited, or an honest "not found" / an access-request offer). It is shown to
    the user verbatim — you do NOT rewrite, summarise, or add to it.

    Args:
        query: the user's question, in natural language.
        stage_id: OPTIONAL. A stage name (e.g. "Requirements") or stage id to
            restrict the search to that stage and the stages it references.
            Omit to search everything the user can access.
    """
    result = _run_search(query, stage_id)
    status = result["status"]

    if status == "answered":
        answer = result["answer"]
        if result.get("confidential_content_also_present"):
            teams = _team_names(result.get("requestable_teams"))
            if teams:
                answer += (
                    f"\n\n_Some related content is confidential to the {teams} "
                    f"team and above your clearance — I can request access from "
                    f"the team lead if you'd like._"
                )
        return answer

    if status == "blocked_by_sensitivity":
        teams = _team_names(result.get("requestable_teams")) or "the owning"
        return (
            f"There is relevant content on this, but it is classified confidential "
            f"to the {teams} team and above your current clearance. Would you like "
            f"me to request access from the {teams} team lead?"
        )

    if status == "no_project_access":
        return "You don't have access to this project."

    if status == "no_results":
        return (
            "I couldn't find anything about that in the documents you have access to."
        )

    # error
    return result.get("message", "The search could not be completed.")


def _team_names(requestable_teams: list[dict] | None) -> str:
    if not requestable_teams:
        return ""
    return ", ".join(t["team_name"] for t in requestable_teams if t.get("team_name"))


def _run_search(query: str, stage_id: str | None = None) -> dict:
    """
    The retrieval + grounded-generation pipeline. Returns a structured dict
    (status + payload); search_documents renders it to the final user string.
    """
    ctx = get_rag_context()
    db = SessionLocal()
    try:
        resolved_stage: uuid.UUID | None = None
        if stage_id:
            resolved_stage = _resolve_stage(db, ctx.project_id, stage_id)
            if resolved_stage is None:
                return {
                    "status": "error",
                    "message": f"No stage matching '{stage_id}' exists in this project.",
                }

        t_tool0 = time.perf_counter()
        try:
            result = retrieve(db, ctx.user_id, ctx.project_id, query, resolved_stage)
        except NoProjectAccessError:
            return {
                "status": "no_project_access",
                "message": "You don't have access to this project.",
            }

        ctx.telemetry.update({
            "auth_context_ms": result.timing.get("auth_ms", 0.0),
            "qdrant_hybrid_ms": result.timing.get("qdrant_ms", 0.0),
            "batch_abac_ms": result.timing.get("abac_ms", 0.0),
            "flashrank_rerank_ms": result.timing.get("rerank_ms", 0.0),
            "retrieval_total_ms": result.timing.get("retrieval_total_ms", 0.0),
        })

        if result.chunks:
            stage_names = _stage_name_map(
                db, ctx.project_id, {c.stage_id for c in result.chunks}
            )
            doc_names = {
                d.document_id: d.original_filename
                for d in db.query(Document).filter(
                    Document.document_id.in_({c.document_id for c in result.chunks})
                )
            }
            sources = []
            source_meta = []
            for i, ch in enumerate(result.chunks, start=1):
                stage_name = stage_names.get(ch.stage_id, "Unknown")
                label = f"Source {i} — {stage_name} stage"
                sources.append(
                    {"label": label, "section": ch.section_title, "text": ch.chunk_text}
                )
                source_meta.append({
                    "label": label,
                    "stage": stage_name,
                    "document": doc_names.get(ch.document_id, "(unknown document)"),
                    "section": ch.section_title,
                })

            t_gen0 = time.perf_counter()
            try:
                answer = generate_answer(query, sources)
            except GenerationError as exc:
                return {"status": "error", "message": f"Could not generate an answer: {exc}"}
            t_gen = (time.perf_counter() - t_gen0) * 1000
            ctx.telemetry["llm_generation_ms"] = t_gen
            ctx.telemetry["tool_total_ms"] = (time.perf_counter() - t_tool0) * 1000

            payload = {
                "status": "answered",
                "answer": answer,
                "sources": source_meta,
                "confidential_content_also_present": False,
            }
            if result.blocked_by_sensitivity:
                payload["confidential_content_also_present"] = True
                payload["requestable_teams"] = _requestable_teams_for_docs(
                    db, ctx.user_id, result.blocked_document_ids
                )
            return payload

        if result.blocked_by_sensitivity:
            teams = _requestable_teams_for_docs(db, ctx.user_id, result.blocked_document_ids)
            return {
                "status": "blocked_by_sensitivity",
                "message": (
                    "Relevant content exists but it is classified confidential on a "
                    "team you're on, and your current clearance doesn't reach it."
                ),
                "requestable_teams": teams,
            }

        return {
            "status": "no_results",
            "message": "No relevant information was found in the documents you can access.",
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. summarize_document — whole-document summary (NOT retrieval)
# ---------------------------------------------------------------------------

@tool
def summarize_document(document_reference: str, stage_reference: str | None = None) -> dict:
    """
    Summarise ONE whole document, named by title, filename, or document ID. Use this — NOT
    search_documents — whenever the user asks to "summarise", "give me an
    overview of", or "tl;dr" a SPECIFIC named document.

    The document is resolved by a direct title/filename lookup within the
    current project (not a search). Access is checked with the same ABAC rule
    as everywhere else. If allowed, the ENTIRE current approved text is
    summarised — never fragments.

    Args:
        document_reference: the document's title, filename, or UUID, as the user
            said it (e.g. "the onboarding guide", "vacation-policy.md").
        stage_reference: optional stage name or stage UUID (e.g. "Sign Off",
            "Architecture & Design") to disambiguate when multiple stages contain
            a document with the same name, or when the user specifies a stage.

    Returns a dict with "status":
        - "summarized": {"document", "summary"}
        - "not_found": no document in this project matches that reference
          (this also covers documents the user cannot see at all — do not
          speculate about whether such a document exists).
        - "ambiguous": {"matches": [...]} — several documents match; ask the
          user which one (specifying the stage).
        - "blocked_by_sensitivity": the document exists and is on the user's
          team but is confidential above their clearance. Carries
          "requestable_teams" — offer to request access.
        - "unavailable": the document has no finalized content to summarise.
        - "error": summarisation failed.
    """
    ctx = get_rag_context()
    db = SessionLocal()
    try:
        ref = (document_reference or "").strip()
        if not ref:
            return {"status": "not_found", "message": "No document reference was given."}

        stage_id = None
        if stage_reference:
            stage_id = _resolve_stage(db, ctx.project_id, stage_reference)

        candidates = _match_documents(db, ctx.project_id, ref, stage_id=stage_id)
        if not candidates:
            return {
                "status": "not_found",
                "message": f"No document matching '{ref}' was found in this project.",
            }
        if len(candidates) > 1:
            stage_names = _stage_name_map(db, ctx.project_id, {d.stage_id for d in candidates})
            return {
                "status": "ambiguous",
                "matches": [
                    f"{d.original_filename} (stage: '{stage_names.get(d.stage_id, 'Unknown')}')"
                    for d in candidates
                ],
                "message": "Multiple documents match that reference across different stages. Please specify which stage.",
            }

        doc = candidates[0]
        visibility = classify_document_visibility(db, ctx.user_id, doc)

        if visibility == DocumentVisibility.not_visible:
            # Never confirm the document exists to someone with no visibility.
            return {
                "status": "not_found",
                "message": f"No document matching '{ref}' was found in this project.",
            }

        if visibility == DocumentVisibility.blocked_by_sensitivity:
            teams = _requestable_teams_for_docs(db, ctx.user_id, [doc.document_id])
            return {
                "status": "blocked_by_sensitivity",
                "document": doc.original_filename,
                "message": (
                    f"'{doc.original_filename}' is classified confidential and your "
                    f"current clearance doesn't reach it."
                ),
                "requestable_teams": teams,
            }

        # fully_allowed
        if doc.current_version_id is None:
            return {
                "status": "unavailable",
                "document": doc.original_filename,
                "message": "That document has no finalized content yet.",
            }
        version = db.get(DocumentVersion, doc.current_version_id)
        if version is None:
            return {
                "status": "unavailable",
                "document": doc.original_filename,
                "message": "That document's current version is missing.",
            }
        try:
            content = version.file_data.decode("utf-8")
        except UnicodeDecodeError:
            return {
                "status": "unavailable",
                "document": doc.original_filename,
                "message": "That document's content is not readable as text.",
            }

        try:
            summary = summarize_full_document(doc.original_filename, content)
        except GenerationError as exc:
            return {"status": "error", "message": f"Could not summarise the document: {exc}"}

        return {
            "status": "summarized",
            "document": doc.original_filename,
            "summary": summary,
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. request_confidential_access — conversational access request
# ---------------------------------------------------------------------------

@tool
def request_confidential_access(team_id: str) -> dict:
    """
    Submit a confidential-access request for the CURRENT USER on a team, to be
    reviewed by that team's lead. Call this ONLY after search_documents or
    summarize_document returned "blocked_by_sensitivity", you offered to
    request access, and the user explicitly said yes.

    Args:
        team_id: the team id (or team name) from the "requestable_teams" field
            of the blocked tool result. Do not invent one.

    Returns a dict with "status":
        - "requested": {"request_id", "team"} — a real pending request now
          exists; the team's lead(s) will review it.
        - "error": {"message"} — e.g. the user already has access or an open
          request, or is not on that team. Relay the message as-is.
    """
    ctx = get_rag_context()
    db = SessionLocal()
    try:
        team_uuid = _resolve_team(db, ctx.project_id, team_id)
        if team_uuid is None:
            return {
                "status": "error",
                "message": f"No team matching '{team_id}' exists in this project.",
            }
        user = db.get(User, ctx.user_id)
        try:
            req = _request_confidential_access(
                db,
                user_id=ctx.user_id,
                team_id=team_uuid,
                expected_tenant_id=user.tenant_id if user else None,
            )
        except AccessRequestError as exc:
            return {"status": "error", "message": exc.message}

        team = db.get(Team, team_uuid)
        return {
            "status": "requested",
            "request_id": str(req.request_id),
            "team": team.name if team else str(team_uuid),
            "message": (
                f"A confidential-access request for the {team.name if team else 'team'} "
                f"team has been submitted and is pending review by the team lead."
            ),
        }
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. list_accessible_documents
# ---------------------------------------------------------------------------

@tool
def list_accessible_documents(stage_reference: str | None = None) -> dict:
    """Lists all documents in this project that the user is authorized to view
    under their current team membership, clearance level, and stage access.
    Optionally filters by `stage_reference` (stage name or UUID). Call this whenever
    the user asks 'what documents do I have access to', 'which documents can I see',
    or asks for a list of project documents.
    """
    ctx = get_rag_context()
    db = SessionLocal()
    try:
        from app.services.authorization_context import build_authorization_context
        from app.services.access_control import classify_documents_visibility, DocumentVisibility
        auth_ctx = build_authorization_context(db, ctx.user_id, ctx.project_id)
        query = db.query(Document).filter(
            Document.project_id == ctx.project_id,
            Document.tenant_id == auth_ctx.tenant_id,
        )

        stage_name = None
        if stage_reference:
            stage_id = _resolve_stage(db, ctx.project_id, stage_reference)
            if stage_id is not None:
                query = query.filter(Document.stage_id == stage_id)
                stg = db.get(Stage, stage_id)
                stage_name = stg.name if stg else str(stage_id)

        all_docs = query.order_by(Document.created_at.desc()).all()
        doc_ids = [d.document_id for d in all_docs]
        vis_map = classify_documents_visibility(db, auth_ctx, doc_ids)

        visible = [d for d in all_docs if vis_map.get(d.document_id) == DocumentVisibility.fully_allowed]
        stage_names = _stage_name_map(db, ctx.project_id, {d.stage_id for d in visible})

        return {
            "status": "ok",
            "scope": f"stage '{stage_name}'" if stage_name else "entire project",
            "count": len(visible),
            "documents": [
                {
                    "name": d.original_filename,
                    "stage": stage_names.get(d.stage_id, "Unknown"),
                    "sensitivity": d.sensitivity_level.name,
                    "uploaded_at": d.created_at.isoformat() if d.created_at else None,
                }
                for d in visible
            ],
        }
    finally:
        db.close()

