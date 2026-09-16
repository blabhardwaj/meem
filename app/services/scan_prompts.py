"""
Structure Scanner — rubric definition and scoring prompt (Phase 3).

The rubric is UNIVERSAL — it does not check a document against any specific
required_documents checklist item (that link is used only for gap-detection/
coverage in Phase 5, not for scan quality). Every document, regardless of
type, is scored against the same 3 structural-quality criteria.

Threshold is PERCENTAGE-BASED (65% of max possible score, raised from 60% in
Master Plan v2 item 6 now that scan_score.py's PER_CRITERION_MINIMUM floor
exists to catch single-axis failure) so it auto-scales if the criteria list
changes again — no manual recalculation needed.
"""

RUBRIC_CRITERIA = [
    {
        "name": "structural_clarity",
        "max_score": 20,
        "description": (
            "Does the document have clear headings/sections and a logical "
            "flow, or is it a wall of undifferentiated text with no "
            "navigable structure?"
        ),
    },
    {
        "name": "completeness",
        "max_score": 20,
        "description": (
            "Are there obvious blank fields, placeholder text (e.g. 'TBD', "
            "'Lorem ipsum', empty brackets), or pasted screenshots/images "
            "standing in for typed content that should be present?"
        ),
    },
    {
        "name": "labeling_accuracy",
        "max_score": 20,
        "description": (
            "Does the content under each heading actually match what that "
            "heading claims? (e.g. a 'Test Cases' section that is actually "
            "just a paragraph of unrelated prose is a labeling failure.)"
        ),
    },
]

MAX_POSSIBLE_SCORE = sum(c["max_score"] for c in RUBRIC_CRITERIA)  # 60
REFORMATION_THRESHOLD_PERCENTAGE = 0.65
REFORMATION_THRESHOLD = round(MAX_POSSIBLE_SCORE * REFORMATION_THRESHOLD_PERCENTAGE)  # 39

SYSTEM_PROMPT = f"""You are a document structure quality scanner for a project management system. Your job is to evaluate ONE uploaded document against 3 universal structural-quality criteria and return a numeric score for each.

You are NOT checking whether the document is the "right" document for its project stage, and you are NOT checking factual accuracy. You are ONLY checking structural/formatting quality: is this document well-organized, complete, and accurately labeled.

RUBRIC (score each 0-20, integers only):

1. structural_clarity — clear headings/sections, logical flow vs. undifferentiated text
2. completeness — no blank fields, placeholders, or screenshot-dumps standing in for real content
3. labeling_accuracy — section content actually matches its heading

For each criterion, give a brief one-sentence note explaining the score — cite something specific from the document, not a generic statement.

Also provide a "summary" field: ONE short sentence (max ~25 words) identifying which criterion/criteria pulled the overall score down the most and why, in plain human language a reviewer can read at a glance. If the document scored well overall (no criterion below 15), the summary should say so briefly instead (e.g. "No significant issues; document is well-structured throughout.").

Respond with ONLY valid JSON, no preamble, no markdown code fences, in exactly this shape:

{{
  "criteria": [
    {{"name": "structural_clarity", "score": <int 0-20>, "note": "<one sentence>"}},
    {{"name": "completeness", "score": <int 0-20>, "note": "<one sentence>"}},
    {{"name": "labeling_accuracy", "score": <int 0-20>, "note": "<one sentence>"}}
  ],
  "overall_score": <int, sum of the 3 scores above, max {MAX_POSSIBLE_SCORE}>,
  "summary": "<one short sentence, plain language, what pulled the score down or that it's fine>"
}}"""

# --- Few-shot examples: calibrate the model on what "good/bad/borderline" means ---

FEW_SHOT_EXAMPLES = [
    {
        "role": "user",
        "content": """Document to score:

# Requirements Specification - Order Management Module

## 1. Overview
This document defines functional and non-functional requirements for the Order Management module, covering order creation, status tracking, and cancellation workflows.

## 2. Functional Requirements
### 2.1 Order Creation
The system shall allow authenticated users to create new orders by selecting items from the catalog, specifying quantities, and confirming a shipping address. Upon submission, the system shall generate a unique order ID and set status to "Pending".

### 2.2 Status Tracking
Orders shall progress through the following statuses: Pending -> Confirmed -> Shipped -> Delivered. Each transition shall be timestamped and logged.

### 2.3 Cancellation
Users may cancel an order while status is "Pending" or "Confirmed". Cancellation after "Shipped" requires a return request instead.

## 3. Non-Functional Requirements
- Order creation must complete within 2 seconds under normal load.
- All order data must be encrypted at rest.

## 4. Stakeholder Sign-off
Approved by: Priya Sharma, Product Lead — 2026-03-12""",
    },
    {
        "role": "assistant",
        "content": """{
  "criteria": [
    {"name": "structural_clarity", "score": 20, "note": "Numbered sections with clear headings and logical progression from overview to sign-off."},
    {"name": "completeness", "score": 19, "note": "All expected sections are filled in with real content; sign-off is present and dated."},
    {"name": "labeling_accuracy", "score": 20, "note": "Each section's content matches its heading precisely, e.g. Functional Requirements contains only functional requirements."}
  ],
  "overall_score": 59,
  "summary": "No significant issues; document is well-structured and complete throughout."
}""",
    },
    {
        "role": "user",
        "content": """Document to score:

Test Plan

TBD

[screenshot placeholder]

test cases: see attached

Owner: ___________
Date: ___________""",
    },
    {
        "role": "assistant",
        "content": """{
  "criteria": [
    {"name": "structural_clarity", "score": 3, "note": "No real sections or headings beyond a bare title; no logical flow."},
    {"name": "completeness", "score": 2, "note": "Scope is literally 'TBD', a screenshot placeholder replaces real content, and Owner/Date fields are blank."},
    {"name": "labeling_accuracy", "score": 5, "note": "The one labeled item, 'test cases', just says 'see attached' with no attachment content present."}
  ],
  "overall_score": 10,
  "summary": "Score pulled down almost entirely by Completeness — the document is essentially empty placeholders."
}""",
    },
    {
        "role": "user",
        "content": """Document to score:

# Design Doc - Notification Service

Covers push, email, and SMS notification delivery.

## Architecture
Uses a queue-based fanout: events land in a central queue, a dispatcher reads them and routes to the correct channel handler.

## Channels
Push: uses FCM.
Email: uses SES.
SMS: TBD, vendor not yet selected.

## Open Questions
- Retry policy for failed SMS not decided.
- Rate limiting thresholds not specified.""",
    },
    {
        "role": "assistant",
        "content": """{
  "criteria": [
    {"name": "structural_clarity", "score": 18, "note": "Clear headings and logical flow from architecture through open questions."},
    {"name": "completeness", "score": 10, "note": "SMS channel and two open questions are explicitly left unresolved with 'TBD' and 'not decided/specified'."},
    {"name": "labeling_accuracy", "score": 18, "note": "Content under each heading matches what it claims, including honestly labeling gaps under Open Questions."}
  ],
  "overall_score": 46,
  "summary": "Mainly held back by Completeness — the SMS channel and two open questions are left explicitly unresolved."
}""",
    },
]


def build_scoring_messages(document_markdown: str) -> list[dict]:
    """
    Builds the full message list for the Groq scoring call:
    system prompt + few-shot examples + the actual document to score.
    """
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(FEW_SHOT_EXAMPLES)
    messages.append({
        "role": "user",
        "content": f"Document to score:\n\n{document_markdown}"
    })
    return messages