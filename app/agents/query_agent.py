from agno.agent import Agent
from agno.models.groq import Groq
from agno.db.postgres import PostgresDb

from app.tools.query_tools import (
    check_my_access,
    get_document_info,
    get_project_structure,
    get_stage_document_status,
    get_stage_requirements,
    get_version_history,
    list_accessible_documents,
    list_pending_approvals,
    who_can_approve,
)
from app.config import GROQ_MODEL, DATABASE_URL

db = PostgresDb(db_url=DATABASE_URL, session_table="agent_sessions")

# The Query Agent is deliberately the SMALLEST of the four agents: metadata
# lookups only, all read-only, all direct Postgres. It has no citation rules,
# no untrusted-content framing, and no retrieval concepts to explain — none of
# that applies here — so the system prompt is kept minimal on purpose. The
# tools themselves carry the access control; the agent only routes and relays.
query_agent = Agent(
    name="Query Agent",
    role=(
        "Answers read-only metadata questions about a project's documents and "
        "structure by calling its lookup tools and relaying their actual "
        "return values — it never guesses and never takes any action."
    ),
    debug_mode=False,
    model=Groq(
        id=GROQ_MODEL,
        max_tokens=800,
        request_params={"reasoning_effort": "none" if "qwen" in GROQ_MODEL.lower() else "low"},
    ),
    tools=[
        get_document_info,
        get_version_history,
        who_can_approve,
        list_pending_approvals,
        check_my_access,
        get_project_structure,
        get_stage_requirements,
        get_stage_document_status,
        list_accessible_documents,
    ],
    db=db,
    add_history_to_context=True,
    num_history_runs=4,
    retries=1,
    exponential_backoff=True,
    instructions="""
You answer metadata questions about documents and projects using the tools
below. Who the user is and which project they are in is already known — never
ask for it, never pass it to a tool.

- Every factual answer must come from a tool result you just received. Never
  guess, never answer document/project facts from memory.
- Never take an action or claim you took one. You cannot approve, reject,
  upload, submit, grant access, or change anything — you only answer
  questions. If asked to DO something, say plainly that you can only answer
  questions about documents and the project, and do not call any tool.
- If a tool reports that something isn't found or isn't visible to the user,
  tell the user you can't find it. Do not explain why, and do not speculate
  about whether a hidden document exists.
- Pick the one tool that fits the question:
    · who uploaded / status / sensitivity / team / stage / upload date of a
      named document -> get_document_info (pass stage_reference if a stage is mentioned)
    · how many versions / version dates / which is current -> get_version_history (pass stage_reference if a stage is mentioned)
    · who can approve / sign off for a team or stage -> who_can_approve
    · what's waiting for MY approval / review -> list_pending_approvals
    · am I allowed to see or upload to a team/stage -> check_my_access
    · what stages/teams exist, which stages need approval -> get_project_structure
    · what documents are required / mandatory checklist for a stage -> get_stage_requirements
    · which required docs are missing / checklist completion / coverage percentage -> get_stage_document_status
- Genuinely ambiguous question, or a tool returned "ambiguous" -> ask one
  short clarifying question. Small talk -> a brief reply, no tool.
- Call at most ONE tool per user message.

Be concise and factual. Lead with the answer.
""",
    markdown=True,
)
