"""
Master Plan v2, item 14: built-in standard section layouts for chat-drafting.

Each layout is a fixed list of {name, purpose} sections, authored once here —
no database storage, no per-user management. A layout (built-in or extracted
from an uploaded reference document via outline_extraction.py) is folded into
the drafting agent's first-turn prompt so the generated document follows a
recognizable, client-expected shape instead of only reflecting whatever
structure the user's own free-form request happened to imply.
"""

BUILTIN_LAYOUTS: dict[str, dict] = {
    "prd": {
        "label": "PRD",
        "document_type": "Product Requirements Document",
        "sections": [
            {"name": "Overview", "purpose": "What this product/feature is and why it's being built"},
            {"name": "Problem Statement", "purpose": "The user or business problem this addresses"},
            {"name": "Goals & Success Metrics", "purpose": "What success looks like, measurably"},
            {"name": "Requirements", "purpose": "Functional requirements, organized by area"},
            {"name": "Non-Goals", "purpose": "What is explicitly out of scope"},
            {"name": "Open Questions", "purpose": "Unresolved decisions or dependencies"},
        ],
    },
    "ard": {
        "label": "ARD",
        "document_type": "Architecture Requirements Document",
        "sections": [
            {"name": "Overview", "purpose": "System/component being designed and its context"},
            {"name": "Goals & Constraints", "purpose": "Design goals and hard constraints (technical, business, compliance)"},
            {"name": "Proposed Architecture", "purpose": "The design itself — components, data flow, boundaries"},
            {"name": "Alternatives Considered", "purpose": "Other approaches evaluated and why they were rejected"},
            {"name": "Risks & Mitigations", "purpose": "Known risks in the proposed design and how they're addressed"},
            {"name": "Open Questions", "purpose": "Unresolved decisions or dependencies"},
        ],
    },
    "test-plan": {
        "label": "Test Plan",
        "document_type": "Test Plan",
        "sections": [
            {"name": "Scope", "purpose": "What is and isn't covered by this test plan"},
            {"name": "Test Approach", "purpose": "Strategy — manual/automated, environments, tooling"},
            {"name": "Test Cases", "purpose": "Scenarios to be tested, organized by area"},
            {"name": "Entry & Exit Criteria", "purpose": "When testing starts and what defines done"},
            {"name": "Risks", "purpose": "Known testing risks or gaps in coverage"},
        ],
    },
    "brd": {
        "label": "BRD",
        "document_type": "Business Requirements Document",
        "sections": [
            {"name": "Executive Summary", "purpose": "One-paragraph summary of the business need and proposal"},
            {"name": "Business Objectives", "purpose": "What the business is trying to achieve"},
            {"name": "Scope", "purpose": "What is and isn't included"},
            {"name": "Business Requirements", "purpose": "Requirements from a business (not technical) perspective"},
            {"name": "Stakeholders", "purpose": "Who is involved or affected"},
            {"name": "Assumptions & Constraints", "purpose": "What is assumed true, and any hard limits"},
        ],
    },
}


def get_builtin_layout(layout_id: str) -> dict | None:
    return BUILTIN_LAYOUTS.get(layout_id)


def list_builtin_layouts() -> list[dict]:
    """[{id, label, document_type, sections}] for the frontend's template picker."""
    return [{"id": lid, **layout} for lid, layout in BUILTIN_LAYOUTS.items()]
