"""Idempotency guard for side-effecting MCP tools (#1924).

When a side-effecting tool call (sending an email, creating a GitHub issue,
posting a tweet) fails AFTER the effect was dispatched — e.g. a timeout
after the request was sent — the agent's default is to retry.  But the effect
may have already landed, producing a **duplicate**: a second email, a second
issue, a second tweet.

This module provides a verify-before-retry hint layer.  When the dispatch
layer detects a post-dispatch failure on a side-effecting tool, it appends an
``[idempotency]`` directive to the tool result telling the model to verify the
effect before retrying.  The directive is advisory — it does not block the
retry mechanically — but it turns a silent duplicate into a visible decision
point.

The module is pure + lazy-import (the MCP tools may not be available in all
environments).  It never executes the verification call itself — it only
returns the tool name + args the dispatch layer *could* use to verify, so the
hint can reference the correct tool.

Usage (in the dispatch layer's error path)::

    from agent.idempotency import apply_retry_advisory

    result = apply_retry_advisory(function_name, function_args, result)

``check_before_retry`` is the underlying decision function; call it directly
only when you need the structured verdict rather than the augmented result.

Intentionally lightweight: the registry is a plain dict and the whole module
is standalone (no agent state required).
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── Side-effect registry ────────────────────────────────────────────────
# Maps a side-effecting MCP tool to metadata about how to verify whether the
# effect landed.  Entries are keyed by the ``service__tool`` TAIL of the tool
# name, never by the full name: the server prefix in front of the tail is a
# deployment detail that changes with how the MCP server is mounted.  The same
# logical tool is exposed as ``mcp__murable__agentmail__send_message`` (the
# murable-bundled server) and ``mcp__agentmail__send_message`` (the standalone
# server) — matching on the tail keeps the registry correct for both instead
# of going silently stale when the prefix changes.  That staleness was the bug
# this registry shipped with: a tool name that matched nothing in this
# environment (#173).
_SIDE_EFFECT_REGISTRY: Dict[str, Dict[str, Any]] = {
    "agentmail__send_message": {
        "verify_tool": "mcp__agentmail__list_messages",
        "effect_type": "email_send",
    },
    "agentmail__create_draft": {
        "verify_tool": "mcp__agentmail__list_drafts",
        "effect_type": "email_draft",
    },
    # Kept for installs that mount a GitHub MCP server.  This install files
    # issues through the `gh` CLI, where the advisory is still the right
    # instruction (list before re-creating), just with a different mechanism.
    "github__create_issue": {
        "verify_tool": "mcp__github__list_issues",
        "effect_type": "github_issue",
    },
    "x_twitter__create_tweet": {
        "verify_tool": "mcp__x_twitter__get_user_tweets",
        "effect_type": "tweet",
    },
}

# Error signatures that suggest the call may have dispatched before failing.
# These are the dangerous cases: the server received and processed the request,
# but the response was lost (timeout, connection drop, cancellation).
_DISPATCH_ERROR_MARKERS = (
    "timed out",
    "timeout",
    "deadline exceeded",
    "connection reset",
    "connection refused",
    "connection closed",
    "closedresourceerror",
    "unreachable",
    "cancelled",
    "remote disconnected",
    "broken pipe",
)


@dataclass
class RetryVerdict:
    """Result of an idempotency check on a failed side-effecting tool call.

    Attributes:
        feedback: The advisory message to append to the tool result.
        verify_tool: The read-only tool name the agent can use to verify.
        effect_type: Category of side effect (email_send, github_issue, etc.).
    """

    feedback: str
    verify_tool: str = ""
    effect_type: str = ""


def _registry_key(tool_name: str) -> str:
    """Normalise a tool name to its ``service__tool`` tail.

    MCP tool names carry a server prefix: ``mcp__<server>__<service>__<tool>``
    (or ``mcp__<service>__<tool>`` when the server mounts under its own name).
    Only the last two ``__``-separated segments identify the logical tool, so
    that is what the registry matches on.
    """
    parts = [p for p in tool_name.split("__") if p]
    if len(parts) < 2:
        return tool_name
    return "__".join(parts[-2:])


def is_side_effecting_tool(tool_name: str) -> bool:
    """True when *tool_name* is in the side-effect registry."""
    return _registry_key(tool_name) in _SIDE_EFFECT_REGISTRY


def _looks_like_post_dispatch_error(result: Any) -> bool:
    """Heuristic: does this failure look like it happened AFTER dispatch?

    The dangerous case: the tool call was sent to the server, the server
    processed it, but the response didn't come back (timeout, connection drop).
    In these cases the side effect may have landed despite the error.
    """
    if not isinstance(result, str):
        return False
    low = result.lower()
    return any(marker in low for marker in _DISPATCH_ERROR_MARKERS)


def check_before_retry(
    tool_name: str,
    args: Dict[str, Any],
    result: Any,
    session_nonce: str = "",
) -> Optional[RetryVerdict]:
    """Decide whether a failed side-effecting tool call is safe to retry.

    Returns ``None`` when:
      - the tool is not side-effecting (not in registry) — caller decides
      - the failure does NOT look like a post-dispatch error (normal errors
        like invalid args are retryable and not an atomicity concern)

    Returns a ``RetryVerdict`` when the failure looks like it may have
    dispatched before failing (timeout, connection drop on a side-effecting
    tool).  The verdict's ``feedback`` tells the model to verify the effect
    before retrying, and that the conservative default is to NOT retry.

    The cost of a duplicate side effect (double email to a real person) is
    higher than the cost of a missed retry (the user re-sends manually), so
    when in doubt, the feedback directs the agent to assume the effect landed.
    """
    entry = _SIDE_EFFECT_REGISTRY.get(_registry_key(tool_name))
    if entry is None:
        return None  # Not a side-effecting tool

    if not _looks_like_post_dispatch_error(result):
        return None  # Normal error — not an atomicity concern

    effect_type = entry.get("effect_type", "side_effect")
    verify_tool = entry.get("verify_tool", "")

    feedback = (
        f"[idempotency] The {effect_type} call failed after dispatch (likely "
        f"timeout). The side effect MAY have already occurred — do NOT retry "
        f"blindly. To verify, call `{verify_tool}` and check whether the "
        f"effect landed before retrying. If you cannot verify, assume it "
        f"succeeded and report the situation to the user."
    )

    return RetryVerdict(
        feedback=feedback,
        verify_tool=verify_tool,
        effect_type=effect_type,
    )


def _looks_like_error_result(result: Any) -> bool:
    """True when *result* is a tool-failure string, not a successful result.

    Guards the advisory against false positives: a successful tool result can
    legitimately contain the word "timeout" (reading a log, echoing config),
    and appending a retry advisory to it would be noise.  The dispatch layer
    builds failure strings as ``Error executing tool '<name>': <cause>``.
    """
    if not isinstance(result, str):
        return False
    stripped = result.lstrip()
    return stripped.lower().startswith(("error", "[errno", "exception"))


def apply_retry_advisory(
    tool_name: str,
    args: Optional[Dict[str, Any]],
    result: Any,
) -> Any:
    """Return *result* with the ``[idempotency]`` advisory appended, or unchanged.

    This is the call-site helper for the dispatch layer: hand it every tool
    result on an error path and it decides whether the failure looks like a
    post-dispatch one on a side-effecting tool.  Anything else — a successful
    result, a non-side-effecting tool, a plain argument error — passes through
    byte-identical.

    Never raises: the advisory must not be able to break the tool result path.
    """
    if not _looks_like_error_result(result):
        return result
    try:
        verdict = check_before_retry(tool_name, args or {}, result)
    except Exception:  # pragma: no cover - defensive
        logger.debug("idempotency check failed for %s", tool_name, exc_info=True)
        return result
    if verdict is None:
        return result
    return f"{result}\n\n{verdict.feedback}"


def idempotency_key(
    tool_name: str, args: Dict[str, Any], session_nonce: str = ""
) -> str:
    """Generate a deterministic key from tool + args + session.

    Excludes transport metadata (clientId, sendAt) that may differ across
    retries of the same logical action.  The key is stable for the same
    (tool, args, session) triple so the dispatch layer can detect a retry.
    """
    key_fields = {k: v for k, v in args.items() if k not in ("clientId", "sendAt")}
    payload = json.dumps(
        {"tool": tool_name, "args": key_fields, "nonce": session_nonce},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:32]
