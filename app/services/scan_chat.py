"""
One turn of the /scan conversation, shared by the CLI (cli.py) and the HTTP
endpoint (app/routers/agents.py).

This is the standalone Structure Scanner chat — DEFERRED_ITEMS.md #4's
"paste/select content and score it independently" capability. It is a thin
wrapper over the existing Scanner Agent (app/agents/scanner_agent.py) and its
three tools (score_document / reform_document / scan_for_injection): no new
scanning logic, no persistence, no ABAC.

Fully decoupled from persistence, exactly like draft_chat:
  - no DB, no chat_sessions/chat_messages, no stage/team/project,
  - multi-turn context ("now reform it") works only within a process, via the
    Agno agent's own in-context history keyed by the namespaced session id.

The agno session id is `scan-<caller uuid>` — namespaced so it can never
collide with a drafting/rag session that happens to reuse the same uuid.
"""

from agno.run.base import RunStatus

from app.agents.scanner_agent import scanner_agent
from app.services.agent_retry import run_agent_resilient

_TOOL_NAMES = ("score_document", "reform_document", "scan_for_injection")


class ScanTurnError(Exception):
    """The agent run itself failed (e.g. the model provider errored)."""


def _tools_called(response) -> list[str]:
    tools = getattr(response, "tools", None) or []
    return [t.tool_name for t in tools if getattr(t, "tool_name", None) in _TOOL_NAMES]


def run_scan_turn(session_id: str, message: str) -> dict:
    """
    Run one standalone-scan turn for `session_id`.

    Returns:
        {
          "reply": str,               # the agent's text (Markdown)
          "tools_called": list[str],  # which scanner tools ran this turn
        }

    Raises:
        ScanTurnError — the model/agent run failed.
    """
    response = run_agent_resilient(scanner_agent, message or "continue", session_id=f"scan-{session_id}")

    if getattr(response, "status", None) == RunStatus.error:
        raise ScanTurnError(getattr(response, "content", "") or "agent run failed")

    return {
        "reply": getattr(response, "content", "") or "",
        "tools_called": _tools_called(response),
    }
