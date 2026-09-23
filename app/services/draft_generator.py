import os
import re
from groq import Groq
from app.services.draft_prompts import build_draft_messages, build_revision_messages
from app.config import GROQ_API_KEY, GROQ_MODEL
_client = Groq(api_key=GROQ_API_KEY)


class DraftGenerationError(Exception):
    """Raised when Groq fails to produce a usable draft."""
    pass


def _run_groq_draft_call(messages: list[dict], max_tokens: int) -> str:
    """Shared Groq call + response cleanup for both a fresh draft and a
    revision — the two differ only in which messages they send in."""
    create_kwargs = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": max_tokens,
    }
    if "qwen" in GROQ_MODEL.lower():
        create_kwargs["reasoning_effort"] = "none"
    else:
        create_kwargs["reasoning_effort"] = "low"

    response = _client.chat.completions.create(**create_kwargs)

    content = response.choices[0].message.content

    if not content or not content.strip():
        raise DraftGenerationError("Groq returned an empty response")

    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    if "</think>" in content:
        content = content.split("</think>", 1)[1]
    content = content.strip()

    # Light sanity check — trim any stray preamble before the first heading,
    # in case the model ignores the "output only the document" rule.
    lines = content.split("\n")
    if lines and not lines[0].lstrip().startswith("#"):
        for i, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                content = "\n".join(lines[i:])
                break

    return content


def draft_document(document_type: str, user_input: str) -> str:
    """
    Generates a full Markdown document from a document type + the user's
    free-form description of what they want it to contain.

    Args:
        document_type: e.g. "Test Plan", "Design Doc", "Requirements Spec"
        user_input: free-form text — bullets, paragraphs, or a mix — describing
                    everything the document should cover

    Returns:
        Markdown string reflecting only what the user described.

    Raises:
        DraftGenerationError: if user_input is empty, or Groq returns
                               an empty/unusable response.
    """
    if not user_input or not user_input.strip():
        raise DraftGenerationError("user_input cannot be empty")

    messages = build_draft_messages(document_type, user_input)
    return _run_groq_draft_call(messages, max_tokens=2500)


def revise_document(document_type: str, current_content: str, change_request: str) -> str:
    """
    Applies a requested change to an existing document and returns the full
    updated document. Unlike draft_document, the caller (draft_document tool)
    never has to route the existing content through a token-constrained
    tool-routing model's own output — it's passed straight into this Groq
    call's input, which has its own separate, larger token budget.

    Args:
        document_type: e.g. "Test Plan", "Design Doc", "Requirements Spec"
        current_content: the full current document, verbatim
        change_request: free-form text describing only what should change

    Returns:
        Markdown string: the full document with the change applied.

    Raises:
        DraftGenerationError: if current_content or change_request is empty,
                               or Groq returns an empty/unusable response.
    """
    if not current_content or not current_content.strip():
        raise DraftGenerationError("current_content cannot be empty")
    if not change_request or not change_request.strip():
        raise DraftGenerationError("change_request cannot be empty")

    messages = build_revision_messages(document_type, current_content, change_request)
    # A revision echoes back the whole (possibly already-long) document, so it
    # needs materially more headroom than a from-scratch draft.
    return _run_groq_draft_call(messages, max_tokens=4000)