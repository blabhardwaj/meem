"""
LLM-augmented relationship/claim extraction (Master Plan v2, item 9).

Additive on top of relationship_extractor.py's regex pass and
claims_analyzer.py's fixed CLAIM_PATTERNS — never replaces either. The regex
pass is cheap, deterministic, and catches the obvious cases (a literal
filename/UUID mention, a claim phrased exactly like one of the fixed
patterns); this module catches what regex structurally cannot: a document
that satisfies a requirement or references another document without ever
using its exact name or filename, and claims phrased in ways no fixed
pattern anticipated.

Confidence gating (stored in Edge.properties / Claim.source_locator, no
schema change needed):
  >= 0.7  -> feeds audit rules directly, same as a regex-found edge/claim.
  0.5-0.7 -> stored with low_confidence=True, EXCLUDED from the audit-rule
             queries (R001 evidence, R008 contradictions) — visible in the
             graph for a future manual-review surface, never silently
             treated as fact.
  < 0.5   -> discarded, not stored at all.
"""
import json
import logging
import re
from typing import Any

from groq import Groq

from app.config import GROQ_API_KEY, GROQ_MODEL

logger = logging.getLogger(__name__)

_client = Groq(api_key=GROQ_API_KEY)

LOW_CONFIDENCE_FLOOR = 0.5
DIRECT_FACT_CONFIDENCE_FLOOR = 0.7

# Content is truncated before hitting the LLM — this is a background,
# additive pass; keeping it fast/cheap matters more than covering every
# character of a very long document (the regex pass already scanned all of it).
MAX_CONTENT_CHARS = 12000

REFERENCE_RELATIONSHIP_TYPES = {
    "REFERENCES", "DEPENDS_ON", "EVIDENCES", "IMPLEMENTS", "VALIDATES", "ESTABLISHES",
}


class LLMExtractionError(Exception):
    """Raised when Groq's response can't be parsed into valid extraction output."""


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    match = re.search(r"\{.*\}|\[.*\]", text, re.DOTALL)
    if match:
        return match.group(0)
    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return fence_match.group(1) if fence_match else text


def _call_groq_json(system_prompt: str, user_content: str) -> Any:
    """Groq call + defensive JSON parse, one retry on malformed output. Mirrors scan_score.py's pattern."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]
    create_kwargs = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 2000,
    }
    if "qwen" in GROQ_MODEL.lower():
        create_kwargs["reasoning_effort"] = "none"
    else:
        create_kwargs["reasoning_effort"] = "low"

    last_error = None
    for _ in range(2):
        response = _client.chat.completions.create(**create_kwargs)
        raw_text = response.choices[0].message.content
        try:
            return json.loads(_strip_markdown_fences(raw_text))
        except json.JSONDecodeError as exc:
            last_error = exc
            continue

    raise LLMExtractionError(f"Failed to get valid JSON after 2 attempts. Last error: {last_error}")


# --- Reference extraction ---------------------------------------------------

_REFERENCE_SYSTEM_PROMPT = """You are analyzing a project document to find what it references or satisfies.

You are told the filename of the document you are analyzing ("this document"), given its content, a
list of OTHER documents in the same project (id + filename), and a list of requirement checklist
items for the project (id + name + description).

Find every place THIS document (the one whose filename and content you were given, NOT one of the
other candidates):
- REFERENCES or DEPENDS_ON another document from the list (by describing it, not necessarily naming
  the exact filename — e.g. "see the checkout API spec" could mean a document about checkout APIs)
- EVIDENCES / IMPLEMENTS / VALIDATES / ESTABLISHES a requirement from the list, by actually
  satisfying what that requirement describes in its content

A common failure mode to avoid: two documents can each discuss the other's topic at length (e.g. a
"Test Plan" narrating that a later "Validation Results" document will confirm its results, and that
"Validation Results" document in turn summarizing the Test Plan's outcomes). When this happens, judge
strictly by what THIS document's OWN TYPE AND PURPOSE is — its title and role, not which requirement
name appears most often in its prose — never attribute evidence for a requirement to this document
merely because this document TALKS ABOUT that requirement's topic or mentions another document by
name. A document only EVIDENCES/IMPLEMENTS/VALIDATES/ESTABLISHES a requirement if satisfying that
requirement is this document's own reason for existing.

Only reference items from the EXACT candidate lists given to you. Never invent a document or
requirement that isn't in the lists. If nothing matches, return an empty list.

Return ONLY a JSON array, no prose:
[
  {
    "target_type": "document" | "requirement",
    "target_id": "<exact id from the candidate list>",
    "relationship": "REFERENCES" | "DEPENDS_ON" | "EVIDENCES" | "IMPLEMENTS" | "VALIDATES" | "ESTABLISHES",
    "confidence": 0.0-1.0,
    "reason": "short justification, one sentence"
  }
]
"""


def extract_references_llm(
    content: str,
    candidate_documents: list[dict],
    candidate_requirements: list[dict],
    this_document_filename: str | None = None,
) -> list[dict]:
    """
    candidate_documents: [{"id": str, "filename": str}]
    candidate_requirements: [{"id": str, "name": str, "description": str | None}]
    this_document_filename: the filename of the document being analyzed —
    without it, the LLM has no explicit anchor for "which document is this"
    and can confuse a document with one of the OTHER candidates it heavily
    cross-references (e.g. a Test Plan and its own Validation Results
    summarizing each other, each getting attributed the other's requirement).

    Returns validated results only — any item naming a target_id outside the
    given candidates, or an invalid relationship/confidence, is dropped
    rather than raising (a single malformed item shouldn't lose the rest).
    """
    if not candidate_documents and not candidate_requirements:
        return []

    doc_ids = {d["id"] for d in candidate_documents}
    req_ids = {r["id"] for r in candidate_requirements}

    manifest = {
        "other_documents": candidate_documents,
        "requirements": candidate_requirements,
    }
    this_doc_line = (
        f"This document's own filename: {this_document_filename}\n\n"
        if this_document_filename else ""
    )
    user_content = (
        f"{this_doc_line}"
        f"Candidate documents and requirements (JSON):\n{json.dumps(manifest)}\n\n"
        f"Document content to analyze:\n\n{content[:MAX_CONTENT_CHARS]}"
    )

    try:
        raw = _call_groq_json(_REFERENCE_SYSTEM_PROMPT, user_content)
    except LLMExtractionError:
        logger.exception("LLM reference extraction failed, skipping this document")
        return []
    except Exception:
        logger.exception("LLM reference extraction: unexpected error, skipping this document")
        return []

    if not isinstance(raw, list):
        return []

    results = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        target_type = item.get("target_type")
        target_id = item.get("target_id")
        relationship = item.get("relationship")
        confidence = item.get("confidence")

        if target_type not in ("document", "requirement"):
            continue
        if relationship not in REFERENCE_RELATIONSHIP_TYPES:
            continue
        if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
            continue
        if target_type == "document" and target_id not in doc_ids:
            continue
        if target_type == "requirement" and target_id not in req_ids:
            continue
        if confidence < LOW_CONFIDENCE_FLOOR:
            continue

        results.append({
            "target_type": target_type,
            "target_id": target_id,
            "relationship": relationship,
            "confidence": float(confidence),
            "reason": str(item.get("reason", ""))[:500],
        })

    return results


# --- Claim extraction --------------------------------------------------------

_CLAIM_SYSTEM_PROMPT = """You are extracting structured factual claims from a project document.

A claim is a concrete, checkable statement the document makes — a timeline, a budget figure, a
technology choice, a security/compliance requirement, a performance target, or any other specific
commitment. Do NOT extract vague statements, opinions, or anything without a concrete value.

Return ONLY a JSON array, no prose:
[
  {
    "subject": "short noun phrase naming what the claim is about, e.g. 'launch date', 'p95 latency'",
    "predicate": "short relation, e.g. 'scheduled_for', 'latency_threshold', 'technology_choice'",
    "object": "the concrete value, e.g. 'March 2026', '200ms', 'PostgreSQL'",
    "polarity": true or false (false for a negated/prohibited/optional statement),
    "snippet": "the exact sentence or phrase from the document this claim comes from",
    "confidence": 0.0-1.0
  }
]

If the document makes no concrete claims, return an empty array.
"""


def extract_claims_llm(content: str) -> list[dict]:
    """
    Free-form subject/predicate (unlike claims_analyzer.py's fixed
    CLAIM_PATTERNS list) — this is what lets contradiction detection catch
    claims phrased differently across documents (see
    claims_analyzer.detect_semantic_contradictions).
    """
    if not content or not content.strip():
        return []

    user_content = f"Document content:\n\n{content[:MAX_CONTENT_CHARS]}"

    try:
        raw = _call_groq_json(_CLAIM_SYSTEM_PROMPT, user_content)
    except LLMExtractionError:
        logger.exception("LLM claim extraction failed, skipping this document")
        return []
    except Exception:
        logger.exception("LLM claim extraction: unexpected error, skipping this document")
        return []

    if not isinstance(raw, list):
        return []

    results = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        subject = item.get("subject")
        predicate = item.get("predicate")
        obj = item.get("object")
        snippet = item.get("snippet")
        confidence = item.get("confidence")
        polarity = item.get("polarity", True)

        if not all(isinstance(v, str) and v.strip() for v in (subject, predicate, obj, snippet)):
            continue
        if not isinstance(polarity, bool):
            polarity = True
        if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
            continue
        if confidence < LOW_CONFIDENCE_FLOOR:
            continue

        results.append({
            "subject": subject.strip(),
            "predicate": predicate.strip(),
            "object": obj.strip(),
            "polarity": polarity,
            "snippet": snippet.strip(),
            "confidence": float(confidence),
        })

    return results


# --- Contradiction adjudication ----------------------------------------------

_CONTRADICTION_SYSTEM_PROMPT = """You are checking whether two factual statements from different
project documents actually contradict each other, or whether they're just phrased differently
while agreeing (or are about related-but-different things).

Return ONLY a JSON object, no prose:
{
  "conflict": true or false,
  "confidence": 0.0-1.0,
  "reason": "one short sentence"
}
"""


def adjudicate_contradiction(claim_a: dict, claim_b: dict) -> dict | None:
    """
    claim_a/claim_b: {"subject": str, "object": str, "snippet": str}

    Master Plan v2, item 9: this is the LLM half of "embed claim text,
    cluster similar claims first, then let the LLM adjudicate whether claims
    within a cluster actually conflict" — the embedding/clustering half lives
    in claims_analyzer.py since it needs claim rows from the DB. This
    function is only ever called on a PRE-FILTERED candidate pair (already
    passed an embedding-similarity threshold) to keep LLM call volume bounded.

    Returns None on any failure (caller should treat as "not adjudicated,
    skip" rather than assuming a conflict).
    """
    user_content = (
        f"Statement A (from one document): \"{claim_a['subject']}: {claim_a['object']}\" "
        f"(source text: \"{claim_a['snippet']}\")\n\n"
        f"Statement B (from a different document): \"{claim_b['subject']}: {claim_b['object']}\" "
        f"(source text: \"{claim_b['snippet']}\")"
    )
    try:
        raw = _call_groq_json(_CONTRADICTION_SYSTEM_PROMPT, user_content)
    except LLMExtractionError:
        logger.exception("Contradiction adjudication failed, skipping this pair")
        return None
    except Exception:
        logger.exception("Contradiction adjudication: unexpected error, skipping this pair")
        return None

    if not isinstance(raw, dict):
        return None
    conflict = raw.get("conflict")
    confidence = raw.get("confidence")
    if not isinstance(conflict, bool):
        return None
    if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
        return None

    return {
        "conflict": conflict,
        "confidence": float(confidence),
        "reason": str(raw.get("reason", ""))[:500],
    }


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    """Plain cosine similarity — no assumption that embed_dense's output is pre-normalized."""
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = sum(a * a for a in vec_a) ** 0.5
    norm_b = sum(b * b for b in vec_b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# --- Document-level coherence assessment (item 10) --------------------------

COHERENCE_ISSUE_TYPES = {"contradiction", "duplicate", "unmet_requirement"}

_COHERENCE_SYSTEM_PROMPT = """You are reviewing a newly finalized project document against related
context already in the project — other documents' content and the project's requirement
checklist — to catch problems a structure/quality scanner cannot see.

You are given the new document's content and a set of RELATED CONTEXT snippets (from other
documents and requirements in the same project, already retrieved as relevant).

Find only CONCRETE, checkable issues:
- "contradiction": the new document states something that conflicts with a related context snippet
  (a different date, number, decision, or requirement than what's already established).
- "duplicate": the new document substantially repeats content already covered by a related
  context snippet, adding no new information (not just covering a similar topic — actually
  restating the same specifics).
- "unmet_requirement": a related context snippet is a requirement description, and the new
  document claims to satisfy it (by title or content) but its actual content does not.

Do NOT flag stylistic differences, unrelated topics, or vague overlaps. If there are no concrete
issues, return an empty list.

Return ONLY a JSON array, no prose:
[
  {
    "type": "contradiction" | "duplicate" | "unmet_requirement",
    "description": "one or two sentences describing the specific issue",
    "confidence": 0.0-1.0,
    "related_context": "which related context snippet this refers to, verbatim or near-verbatim"
  }
]
"""


def assess_document_coherence(content: str, related_context: list[dict]) -> list[dict]:
    """
    related_context: [{"label": str, "text": str}] — already-retrieved snippets
    (from Qdrant similarity + graph neighbors, assembled by the caller in
    coherence.py) describing what this document should be checked against.
    Not the LLM's job to go find context itself — keeps this call grounded
    and bounded, same principle as extract_references_llm's candidate list.

    Returns validated issues only (confidence must be 0.5+ same floor as the
    rest of item 9/10's LLM outputs; type must be one of COHERENCE_ISSUE_TYPES).
    """
    if not related_context:
        return []

    context_text = "\n\n".join(f"[{c['label']}]\n{c['text']}" for c in related_context)
    user_content = (
        f"Related context:\n\n{context_text}\n\n"
        f"New document content:\n\n{content[:MAX_CONTENT_CHARS]}"
    )

    try:
        raw = _call_groq_json(_COHERENCE_SYSTEM_PROMPT, user_content)
    except LLMExtractionError:
        logger.exception("Document coherence assessment failed")
        return []
    except Exception:
        logger.exception("Document coherence assessment: unexpected error")
        return []

    if not isinstance(raw, list):
        return []

    results = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        issue_type = item.get("type")
        description = item.get("description")
        confidence = item.get("confidence")

        if issue_type not in COHERENCE_ISSUE_TYPES:
            continue
        if not isinstance(description, str) or not description.strip():
            continue
        if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
            continue
        if confidence < LOW_CONFIDENCE_FLOOR:
            continue

        results.append({
            "type": issue_type,
            "description": description.strip()[:1000],
            "confidence": float(confidence),
            "related_context": str(item.get("related_context", ""))[:1000],
        })

    return results
