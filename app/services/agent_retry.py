"""
Resilience wrapper for agno Agent.run() calls against Groq's tool-calling API.

Real failure observed live: Groq occasionally emits genuinely malformed JSON
for a tool call's arguments (e.g. a zero-argument tool like
list_pending_approvals gets called with garbage like '{1"1":1"1":"..."}').
Groq's API rejects this itself with a 400 "tool_use_failed" / "Failed to
parse tool call arguments as JSON" error, which agno wraps as a
ModelProviderError and — correctly, as a GENERAL policy — does NOT retry,
because agno's retry logic treats every 400 as a non-retryable client error
(see agno.models.base.Model._is_retryable_error). That general policy is
wrong for this SPECIFIC error: the "bad request" here is a one-off glitch in
the model's own generation (sampling noise), not a genuine problem with our
request, so asking again with the exact same input often just works.

Without this wrapper, that single bad generation crashed the entire turn
with a raw, technical error surfaced straight to the user (a JSON fragment
in a chat bubble). This retries specifically that error signature a couple
of times before giving up and re-raising, so it looks the same as any other
transient hiccup to callers — no code changes needed at call sites beyond
swapping `agent.run(...)` for `run_agent_resilient(agent, ...)`.
"""

import logging
import time

from agno.exceptions import ModelProviderError

logger = logging.getLogger(__name__)

_MALFORMED_TOOL_CALL_SIGNATURES = (
    "tool_use_failed",
    "failed to parse tool call arguments",
)

_MAX_ATTEMPTS = 3
_RETRY_DELAY_SECONDS = 0.5


def _is_malformed_tool_call_error(exc: Exception) -> bool:
    if not isinstance(exc, ModelProviderError):
        return False
    message = str(exc).lower()
    return any(sig in message for sig in _MALFORMED_TOOL_CALL_SIGNATURES)


def run_agent_resilient(agent, *args, **kwargs):
    """
    Agent.run(*args, **kwargs), retried up to _MAX_ATTEMPTS times if Groq
    reports a malformed tool-call-arguments error. Any other exception (or
    exhausting all attempts) propagates exactly as agent.run() would have
    raised it directly.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            return agent.run(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — re-raised below if not our target
            if not _is_malformed_tool_call_error(exc):
                raise
            last_exc = exc
            logger.warning(
                "Groq returned a malformed tool-call arguments error (attempt %d/%d) for agent %r — retrying: %s",
                attempt, _MAX_ATTEMPTS, getattr(agent, "name", agent), exc,
            )
            if attempt < _MAX_ATTEMPTS:
                time.sleep(_RETRY_DELAY_SECONDS)
    raise last_exc
