"""
Master Plan v2, item 12: applies one described change to a document's FULL
current content, preserving everything else — the same whole-document
regeneration convention draft_generator.draft_document uses for chat-drafting
revisions, just framed around "revise this specific document" instead of
"draft a new one from a type + description."
"""

import re

from groq import Groq

from app.config import GROQ_API_KEY, GROQ_MODEL
from app.services.revision_prompts import build_revision_messages

_client = Groq(api_key=GROQ_API_KEY)


class RevisionGenerationError(Exception):
    pass


def revise_document(current_content: str, requested_change: str) -> str:
    """
    Returns the full revised document. Raises RevisionGenerationError on an
    empty change description or an empty/unusable Groq response.
    """
    if not requested_change or not requested_change.strip():
        raise RevisionGenerationError("requested_change cannot be empty")

    messages = build_revision_messages(current_content, requested_change)

    create_kwargs = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 3000,
    }
    if "qwen" in GROQ_MODEL.lower():
        create_kwargs["reasoning_effort"] = "none"
    else:
        create_kwargs["reasoning_effort"] = "low"

    response = _client.chat.completions.create(**create_kwargs)
    content = response.choices[0].message.content

    if not content or not content.strip():
        raise RevisionGenerationError("Groq returned an empty response")

    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    if "</think>" in content:
        content = content.split("</think>", 1)[1]
    content = content.strip()

    lines = content.split("\n")
    if lines and not lines[0].lstrip().startswith("#"):
        for i, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                content = "\n".join(lines[i:])
                break

    return content
