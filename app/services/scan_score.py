"""
takes a document's parsed md (from document_parser.py)
turn it into a score by asking groqs llm to judge it against the 5 criterion rubric
"""

"""
Structure Scanner — scoring function (Phase 3).

Calls Groq with the rubric + few-shot prompt from scan_prompts.py, parses
the JSON response defensively, and re-validates/re-computes overall_score
from the individual criteria rather than trusting the LLM's arithmetic.
"""

import json
import os
import re
from groq import Groq
from app.services.scan_prompts import build_scoring_messages, RUBRIC_CRITERIA
from app.config import GROQ_API_KEY, GROQ_MODEL


_client = Groq(api_key=GROQ_API_KEY)

_EXPECTED_CRITERION_NAMES = {c["name"] for c in RUBRIC_CRITERIA}
_MAX_PER_CRITERION = 20


class ScoringError(Exception):
    """Raised when Groq's response can't be parsed into a valid score."""
    pass


def _strip_markdown_fences(text: str) -> str:
    """
    Defensive cleanup: strip think blocks, markdown fences, and isolate the JSON object.
    """
    text = text.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        return match.group(0)
    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return fence_match.group(1) if fence_match else text


def _validate_and_normalize(parsed: dict) -> dict:
    """
    Validates the parsed JSON has the expected shape, clamps scores into
    range, and RECOMPUTES overall_score as the sum of criteria scores
    rather than trusting whatever number the LLM put there.
    """
    if "criteria" not in parsed or not isinstance(parsed["criteria"], list):
        raise ScoringError("Response missing 'criteria' list")

    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise ScoringError("Response missing non-empty 'summary' string")

    seen_names = set()
    normalized_criteria = []

    for item in parsed["criteria"]:
        name = item.get("name")
        note = item.get("note", "")
        score = item.get("score")

        if name not in _EXPECTED_CRITERION_NAMES:
            raise ScoringError(f"Unexpected criterion name: {name}")
        if not isinstance(score, (int, float)):
            raise ScoringError(f"Non-numeric score for {name}: {score}")

        # Clamp defensively — a model returning 25/20 shouldn't blow up scoring,
        # just cap it rather than reject the whole scan.
        score = max(0, min(_MAX_PER_CRITERION, int(round(score))))

        normalized_criteria.append({"name": name, "score": score, "note": note})
        seen_names.add(name)

    missing = _EXPECTED_CRITERION_NAMES - seen_names
    if missing:
        raise ScoringError(f"Response missing criteria: {missing}")

    overall_score = sum(c["score"] for c in normalized_criteria)

    return {
        "overall_score": overall_score,
        "criteria": normalized_criteria,
        "summary": summary.strip(),
    }


def score_document(document_markdown: str) -> dict:
    """
    Scores a document's Markdown against the structure rubric via Groq.

    Returns:
        {
            "overall_score": int,
            "criteria": [{"name", "score", "note"}, ...],
            "summary": str
        }

    Raises:
        ScoringError: if Groq's response can't be parsed/validated,
                      even after one retry.
    """
    messages = build_scoring_messages(document_markdown)

    create_kwargs = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0,  # deterministic scoring, not creative
        "max_tokens": 1000,
    }
    if "qwen" in GROQ_MODEL.lower():
        create_kwargs["reasoning_effort"] = "none"
    else:
        create_kwargs["reasoning_effort"] = "low"

    last_error = None
    for attempt in range(2):  # one retry on malformed output
        response = _client.chat.completions.create(**create_kwargs)
        raw_text = response.choices[0].message.content

        try:
            cleaned = _strip_markdown_fences(raw_text)
            parsed = json.loads(cleaned)
            return _validate_and_normalize(parsed)
        except (json.JSONDecodeError, ScoringError) as e:
            last_error = e
            continue

    raise ScoringError(
        f"Failed to get valid score after 2 attempts. Last error: {last_error}"
    )