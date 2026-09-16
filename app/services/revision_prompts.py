SYSTEM_PROMPT = """You are revising an existing document based on a user's requested change.

You will receive the document's FULL current content and a description of one requested
change. Return the FULL document with that change applied — every other section, sentence, and
fact preserved exactly as given. Never regenerate the document from scratch, never summarize it,
never drop content the user did not ask to remove.

RULES:
1. Apply ONLY the requested change. Do not "improve," reformat, or reorganize anything else.
2. Preserve all headings, sections, and facts not related to the requested change, verbatim.
3. NEVER invent specific facts not present in the original content or the requested change — no
   fabricated names, dates, metrics, or numbers.
4. Output ONLY the full revised document in Markdown. No preamble, no meta-commentary.

INPUT FORMAT you will receive:
Current Document:
<full current content>

Requested Change:
<free-form text describing what to change>
"""


def build_revision_messages(current_content: str, requested_change: str) -> list[dict]:
    user_content = (
        f"Current Document:\n{current_content}\n\nRequested Change:\n{requested_change}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
