import os
from groq import Groq
from app.services.draft_prompts import build_draft_messages
from app.config import GROQ_API_KEY, GROQ_MODEL
_client = Groq(api_key=GROQ_API_KEY)


class DraftGenerationError(Exception):
    """Raised when Groq fails to produce a usable draft."""
    pass


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

    create_kwargs = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": 2500,
    }
    if "qwen" in GROQ_MODEL.lower():
        create_kwargs["reasoning_effort"] = "none"
    else:
        create_kwargs["reasoning_effort"] = "low"

    response = _client.chat.completions.create(**create_kwargs)

    content = response.choices[0].message.content

    if not content or not content.strip():
        raise DraftGenerationError("Groq returned an empty response")

    import re
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    if "</think>" in content:
        content = content.split("</think>", 1)[1]
    content = content.strip()

    # Light sanity check — trim any stray preamble before the first heading,
    # in case the model ignores rule 5 despite instructions.
    lines = content.split("\n")
    if lines and not lines[0].lstrip().startswith("#"):
        for i, line in enumerate(lines):
            if line.lstrip().startswith("#"):
                content = "\n".join(lines[i:])
                break

    return content