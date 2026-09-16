"""
Master Plan v2, item 14: extract a reusable section OUTLINE from a user-
uploaded reference document, for the "upload template" flow in chat-drafting.

Deliberately a dedicated extraction step, not folded into drafting directly:
a user uploading a "template" will in practice upload a real, filled-out
document (an old PRD, say) and expect only its STRUCTURE to be reused — the
actual content (names, dates, numbers, specifics) must never leak into the
new draft. This module's one job is to strip content and return shape only,
same {name, purpose} format as a built-in layout (draft_layouts.py) so both
feed the same downstream prompt-injection path in draft_chat.py.

Nothing here is persisted — the extracted outline lives only in the HTTP
response; the caller (the frontend, then the draft-chat request) is
responsible for holding onto it for the session.
"""
import json
import re
from typing import Any

from groq import Groq

from app.config import GROQ_API_KEY, GROQ_MODEL

_client = Groq(api_key=GROQ_API_KEY)

# Same reasoning as llm_extraction.py's MAX_CONTENT_CHARS: this is a single
# interactive request a user is waiting on, not a background pass — keep it
# fast/cheap rather than covering a very long reference document in full.
MAX_CONTENT_CHARS = 12000

_SYSTEM_PROMPT = """You extract the SECTION STRUCTURE of a document — never its content.

You will be given the text of a real document. Return ONLY its outline: the sequence of
section headings it uses, and a one-line description of what PURPOSE each section serves
(not what it currently says).

STRICT RULES:
1. NEVER include actual content from the document — no names, dates, numbers, product
   details, company names, or any other specific fact. The "purpose" line describes the
   ROLE of the section in general terms (e.g. "States the problem being solved"), not what
   this particular document wrote there.
2. Use the document's own heading structure and order.
3. If a heading's purpose can't be determined from context, infer a reasonable generic one.
4. Output ONLY a JSON array, no other text: [{"name": "<heading text>", "purpose": "<one-line generic purpose>"}, ...]
5. If the document has no discernible section structure (e.g. it's just prose with no
   headings), return an empty array [].

Example:
Input document mentions "## Q3 Revenue Targets\\nWe aim to hit $4.2M by..."
Output entry: {"name": "Q3 Revenue Targets", "purpose": "States the revenue goal for the period"}
NOT {"name": "Q3 Revenue Targets", "purpose": "Aims to hit $4.2M"} — that leaks content.
"""


class OutlineExtractionError(Exception):
    pass


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        return match.group(0)
    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    return fence_match.group(1) if fence_match else text


def extract_outline_from_reference(content: str) -> list[dict]:
    """
    Returns [{"name": str, "purpose": str}, ...] — content-stripped section
    outline. Empty list if the document has no discernible structure.

    Raises OutlineExtractionError if Groq's response can't be parsed as a
    valid outline after retrying once.
    """
    if not content or not content.strip():
        raise OutlineExtractionError("content cannot be empty")

    truncated = content[:MAX_CONTENT_CHARS]
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": truncated},
    ]
    create_kwargs: dict[str, Any] = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0,
        "max_tokens": 1500,
    }
    if "qwen" in GROQ_MODEL.lower():
        create_kwargs["reasoning_effort"] = "none"
    else:
        create_kwargs["reasoning_effort"] = "low"

    last_error: Exception | None = None
    for _ in range(2):
        response = _client.chat.completions.create(**create_kwargs)
        raw_text = response.choices[0].message.content
        try:
            parsed = json.loads(_strip_markdown_fences(raw_text))
        except json.JSONDecodeError as exc:
            last_error = exc
            continue

        if not isinstance(parsed, list):
            last_error = ValueError("Expected a JSON array")
            continue

        sections = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            purpose = item.get("purpose")
            if isinstance(name, str) and name.strip() and isinstance(purpose, str):
                sections.append({"name": name.strip(), "purpose": purpose.strip()})
        return sections

    raise OutlineExtractionError(f"Failed to extract a valid outline after 2 attempts. Last error: {last_error}")
