"""
Direct-Postgres resolution of free-form references to documents, stages, and
teams within a project. NOT a vector search — plain lookups over the handful
of rows a project has, forgiving about phrasing.

Extracted from app/tools/rag_tools.py so the Query Agent's tools
(app/tools/query_tools.py) can resolve a "document reference" the same way
summarize_document does, without importing the RAG retrieval pipeline (and
its embedding-model stack) just to reuse a string matcher.
"""

import re
import uuid

from sqlalchemy import func

from app.models.document import Document
from app.models.stage import Stage
from app.models.team import Team


# ---------------------------------------------------------------------------
# document reference -> Document rows
# ---------------------------------------------------------------------------

_REF_STOPWORDS = {
    "the", "a", "an", "document", "doc", "file", "of", "for", "about", "please",
    "whole", "entire", "this", "that", "our", "my", "summary", "overview",
}
_DOC_EXT_RE = re.compile(r"(\.|\s+)(md|pdf|docx|doc|txt)$", re.IGNORECASE)


def normalize_ref(s: str) -> str:
    s = s.lower()
    for ch in "-_./\\'\"":
        s = s.replace(ch, " ")
    return " ".join(s.split())


def match_documents(db, project_id: uuid.UUID, ref: str, stage_id: uuid.UUID | None = None) -> list[Document]:
    """
    Resolve a free-form reference to document(s) by `original_filename` or UUID within
    the project. Matching is done in Python (few docs per project) so it can be
    forgiving:
      - direct UUID match if ref is a UUID;
      - scoped to stage_id if provided;
      - auto-detects stage names mentioned in ref (e.g. 'doc in Sign Off stage');
      - '-' / '_' / '.' / slashes are treated as spaces, case-insensitive;
      - a filename extension on either side is ignored;
      - the reference contains the filename stem, OR the filename contains the
        reference, OR every significant word of the filename appears in the
        reference.

    Returns matches (exact-ish first); the caller treats 0 as not-found and
    >1 as ambiguous.
    """
    ref_raw = (ref or "").strip()
    if not ref_raw:
        return []

    # 1. Direct UUID lookup
    try:
        as_uuid = uuid.UUID(ref_raw)
        q = db.query(Document).filter(Document.project_id == project_id, Document.document_id == as_uuid)
        if stage_id is not None:
            q = q.filter(Document.stage_id == stage_id)
        found = q.all()
        if found:
            return found
    except (ValueError, AttributeError):
        pass

    # 2. Stage detection in ref if stage_id not explicitly given
    detected_stage_id = stage_id
    cleaned_ref = ref_raw
    if detected_stage_id is None:
        project_stages = (
            db.query(Stage)
            .filter(Stage.project_id == project_id, Stage.deleted_at.is_(None))
            .all()
        )
        ref_norm = normalize_ref(ref_raw)
        for stg in project_stages:
            stg_norm = normalize_ref(stg.name)
            if not stg_norm:
                continue
            # Look for "in <stage>", "under <stage>", or directly the stage name in the ref
            patterns = [
                rf"\b(in|under|for|within|at)(\s+the)?\s+{re.escape(stg_norm)}(\s+stage|\s+phase)?\b",
                rf"\b{re.escape(stg_norm)}(\s+stage|\s+phase)\b",
            ]
            matched = False
            for pat in patterns:
                if re.search(pat, ref_norm):
                    matched = True
                    break
            if matched:
                detected_stage_id = stg.stage_id
                # Strip the stage mention from ref for filename matching
                # remove words matching stage
                stg_words = set(stg_norm.split()) | {"in", "under", "within", "stage", "phase"}
                words_left = [w for w in ref_norm.split() if w not in stg_words]
                if words_left:
                    cleaned_ref = " ".join(words_left)
                break

    ref_n = normalize_ref(cleaned_ref)
    ref_stem = _DOC_EXT_RE.sub("", ref_n).strip()
    if not ref_stem:
        ref_n = normalize_ref(ref_raw)
        ref_stem = _DOC_EXT_RE.sub("", ref_n).strip()
    if not ref_stem:
        return []
    ref_tokens = {t for t in ref_stem.split() if t not in _REF_STOPWORDS and len(t) > 2}

    query = (
        db.query(Document)
        .filter(Document.project_id == project_id)
    )
    if detected_stage_id is not None:
        query = query.filter(Document.stage_id == detected_stage_id)

    docs = query.order_by(Document.created_at.desc()).all()

    # If stage-filtered search yielded no docs, but detected_stage_id was inferred, fallback to all
    if not docs and detected_stage_id is not None and stage_id is None:
        docs = (
            db.query(Document)
            .filter(Document.project_id == project_id)
            .order_by(Document.created_at.desc())
            .all()
        )

    strong: list[Document] = []
    weak: list[Document] = []
    for d in docs:
        fn = normalize_ref(d.original_filename)
        stem = _DOC_EXT_RE.sub("", fn).strip()
        if not stem:
            continue
        if ref_stem == stem or ref_stem in fn or stem in ref_n:
            strong.append(d)
            continue
        words = [w for w in stem.split() if w]
        acronym = "".join(w[0] for w in words if w not in _REF_STOPWORDS)
        acronym_all = "".join(w[0] for w in words)
        if ref_stem in (acronym, acronym_all) or (acronym and acronym in ref_tokens) or (acronym_all and acronym_all in ref_tokens):
            strong.append(d)
            continue
        fn_tokens = {t for t in stem.split() if t not in _REF_STOPWORDS and len(t) > 2}
        if fn_tokens and fn_tokens <= ref_tokens:
            weak.append(d)

    chosen = strong or weak
    seen, unique = set(), []
    for d in chosen:
        if d.document_id not in seen:
            seen.add(d.document_id)
            unique.append(d)
    return unique


# ---------------------------------------------------------------------------
# stage / team reference -> id
# ---------------------------------------------------------------------------

def resolve_stage(db, project_id: uuid.UUID, stage_ref: str) -> uuid.UUID | None:
    """A stage UUID string OR a stage name (case-insensitive, forgiving). None if unresolvable."""
    stage_ref = (stage_ref or "").strip()
    if not stage_ref:
        return None
    try:
        as_uuid = uuid.UUID(stage_ref)
        exists = db.query(Stage.stage_id).filter(
            Stage.stage_id == as_uuid, Stage.project_id == project_id, Stage.deleted_at.is_(None)
        ).first()
        return as_uuid if exists else None
    except ValueError:
        pass

    # Exact lowercase match
    row = db.query(Stage).filter(
        Stage.project_id == project_id,
        Stage.deleted_at.is_(None),
        func.lower(Stage.name) == stage_ref.lower(),
    ).first()
    if row:
        return row.stage_id

    # Forgiving match: strip trailing "stage" / "phase" and normalize delimiters
    clean_ref = re.sub(r"\b(stage|phase)\b", "", stage_ref, flags=re.IGNORECASE).strip()
    clean_norm = normalize_ref(clean_ref)
    if clean_norm:
        all_stages = db.query(Stage).filter(
            Stage.project_id == project_id, Stage.deleted_at.is_(None)
        ).all()
        for s in all_stages:
            s_norm = normalize_ref(s.name)
            if s_norm == clean_norm or clean_norm in s_norm or s_norm in clean_norm:
                return s.stage_id
    return None


def resolve_team(db, project_id: uuid.UUID, team_ref: str) -> uuid.UUID | None:
    """A team UUID string OR a team name (case-insensitive, forgiving). None if unresolvable."""
    team_ref = (team_ref or "").strip()
    if not team_ref:
        return None
    try:
        as_uuid = uuid.UUID(team_ref)
        exists = db.query(Team.team_id).filter(
            Team.team_id == as_uuid, Team.project_id == project_id
        ).first()
        return as_uuid if exists else None
    except ValueError:
        pass

    row = db.query(Team).filter(
        Team.project_id == project_id, func.lower(Team.name) == team_ref.lower()
    ).first()
    if row:
        return row.team_id

    clean_norm = normalize_ref(team_ref)
    if clean_norm:
        all_teams = db.query(Team).filter(Team.project_id == project_id).all()
        for t in all_teams:
            t_norm = normalize_ref(t.name)
            if t_norm == clean_norm or clean_norm in t_norm or t_norm in clean_norm:
                return t.team_id
    return None

