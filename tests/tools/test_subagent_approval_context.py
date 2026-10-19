"""Regression tests for issue #229 — a delegated subagent must be denied, not crashed.

``tools/approval._Unattended.mode()`` resolves its config getter *by constructed
name* (``_get_{name}_approval_mode``). Two gaps turned a missing name into an
``AttributeError``/``KeyError`` at dispatch, before any approval decision was
made, so nothing in the subagent ran at all:

1. ``_get_subagent_approval_mode`` was absent from ``approval_context``
   (regression of #196).
2. ``_run_approval_gate``'s ``deny_messages`` dict is indexed once per
   unattended context name but only carries ``single_query``/``cron``/
   ``unattended`` — the registered ``subagent`` context raised ``KeyError``.

The invariants below are what stops a third occurrence: every registered context
resolves a mode, a missing getter fails closed, and an unmapped deny message
falls through to the context's own block text.
"""

from __future__ import annotations

import pytest

from tools import approval, approval_context

# Names registered as ``_Unattended`` contexts in tools/approval.py.
REGISTERED_CONTEXTS = ("single_query", "cron", "unattended", "subagent")


@pytest.mark.parametrize("name", REGISTERED_CONTEXTS)
def test_every_registered_context_has_a_mode_getter(name):
    """The name is load-bearing: ``mode()`` looks the getter up by construction."""
    getter = getattr(approval_context, f"_get_{name}_approval_mode", None)
    assert callable(getter), (
        f"tools/approval_context.py is missing _get_{name}_approval_mode; "
        "every gated call from this context would die at dispatch"
    )


def test_mode_fails_closed_when_the_getter_is_missing(monkeypatch):
    """A missing getter must deny, never raise. Deny is the only safe default."""
    ctx = approval._Unattended(
        "hypothetical", "hypothetical_mode", "nobody is present", "in tests", "trusted"
    )
    assert ctx.mode() == "deny"


def test_subagent_context_denies_without_crashing(monkeypatch):
    """The unfixed path raised KeyError: 'subagent' here (deny_messages lacked the key)."""
    monkeypatch.setattr(approval, "_is_single_query_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_subagent_context", lambda: True)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_unattended_platform_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_presence", lambda *a, **k: (None, False, False, False))
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(approval, "is_approved", lambda *a, **k: False)
    monkeypatch.setattr(approval, "get_current_session_key", lambda: "test-session")
    monkeypatch.setattr(approval.approval_context, "_get_approval_mode", lambda: "default")
    monkeypatch.setattr(approval.approval_context, "_get_subagent_approval_mode", lambda: "deny")

    result = approval._run_approval_gate(
        pattern_key="rm -rf /",
        description="destructive command",
        display_target="terminal",
        autoapprove_log_prefix="Auto",
    )

    assert result["approved"] is False
    message = result["message"]
    # The subagent has no caller-specific wording, so it must fall through to
    # the context's own block_message — which names the setting to change.
    assert "approvals.subagent_mode: approve" in message


def test_subagent_context_approves_when_configured(monkeypatch):
    """Configuring the mode must still work — the guard must not over-deny."""
    monkeypatch.setattr(approval, "_is_single_query_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_subagent_context", lambda: True)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_unattended_platform_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_presence", lambda *a, **k: (None, False, False, False))
    monkeypatch.setattr(approval, "_yolo_active", lambda: False)
    monkeypatch.setattr(approval, "is_approved", lambda *a, **k: False)
    monkeypatch.setattr(approval, "get_current_session_key", lambda: "test-session")
    monkeypatch.setattr(approval.approval_context, "_get_approval_mode", lambda: "default")
    monkeypatch.setattr(approval.approval_context, "_get_subagent_approval_mode", lambda: "approve")

    result = approval._run_approval_gate(
        pattern_key="rm -rf /",
        description="destructive command",
        display_target="terminal",
        autoapprove_log_prefix="Auto",
    )

    assert result["approved"] is True
