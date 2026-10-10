"""Durable guard for the unattended-context approval getters (#196).

``approval._Unattended.mode()`` does not call a fixed function — it resolves
``getattr(approval_context, f"_get_{self.name}_approval_mode")`` at call time, so that
tests can patch the getters. The cost of that indirection is that adding a context to
``_unattended_contexts()`` without also adding its getter to ``tools.approval_context``
turns a policy denial into an ``AttributeError`` raised at dispatch: the agent sees an
opaque tool error and cannot tell "refused" from "broken gate".

That is exactly what happened with the ``subagent`` context, wired into ``_SUBAGENT_CTX``
and ``_unattended_contexts()`` while ``_get_subagent_approval_mode`` was never added.

These tests drive ``_unattended_contexts()`` through every branch it has and assert the
invariant for each context name it produces, so the next context added cannot repeat
the bug.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import patch

import pytest

from tools import approval, approval_context
from tools.approval_context import _get_subagent_approval_mode

# ``_unattended_contexts()`` reaches the platform "unattended" context only when cron is
# NOT active (it is an ``elif``), so the two live branches need separate cases:
#   * all cron-family predicates true -> single_query + subagent + cron
#   * cron false, platform unattended true -> single_query + subagent + unattended
# Together the cases cover every name the function can produce.
_CASES = {
    "cron_active": {
        "_is_single_query_approval_context": True,
        "_is_subagent_context": True,
        "_is_cron_approval_context": True,
        "_is_unattended_platform_approval_context": False,
    },
    "unattended_platform": {
        "_is_single_query_approval_context": True,
        "_is_subagent_context": True,
        "_is_cron_approval_context": False,
        "_is_unattended_platform_approval_context": True,
    },
}

_EXPECTED_NAMES = {
    "cron_active": {"single_query", "subagent", "cron"},
    "unattended_platform": {"single_query", "subagent", "unattended"},
}

# Every context name either case can produce; the union must stay complete.
_ALL_NAMES = set().union(*_EXPECTED_NAMES.values())


@pytest.fixture(params=sorted(_CASES))
def contexts(request):
    """The unattended-context list for one predicate configuration."""
    case = request.param
    with ExitStack() as stack:
        for predicate, value in _CASES[case].items():
            stack.enter_context(
                patch(f"tools.approval.{predicate}", return_value=value)
            )
        produced = approval._unattended_contexts()
        assert produced, "no unattended contexts produced — the guard would be vacuous"
        yield case, produced


def test_setup_reaches_the_expected_contexts(contexts):
    """Assert the setup before trusting any result: a guard that silently exercises one
    context is worse than no guard."""
    case, produced = contexts
    assert {ctx.name for ctx in produced} == _EXPECTED_NAMES[case]


def test_cases_cover_every_known_context_name():
    """If a context is ever dropped from the cases, coverage shrinks silently."""
    assert _ALL_NAMES == {"single_query", "subagent", "cron", "unattended"}


def test_every_unattended_context_resolves_its_getter(contexts):
    """THE regression guard (#196): the dynamic lookup must resolve for every name."""
    _, produced = contexts
    for ctx in produced:
        getter_name = f"_get_{ctx.name}_approval_mode"
        getter = getattr(approval_context, getter_name, None)
        assert getter is not None, (
            f"unattended context {ctx.name!r} (config approvals.{ctx.cfg_key}) has no "
            f"approval_context.{getter_name}() — a gated call in this context would "
            "raise AttributeError at dispatch instead of denying cleanly"
        )
        assert callable(getter)


def test_every_unattended_context_mode_is_binary(contexts):
    """``mode()`` must return a usable verdict for every context, never raise."""
    _, produced = contexts
    for ctx in produced:
        assert ctx.mode() in {"deny", "approve"}


def test_subagent_getter_reads_subagent_mode_key():
    """The getter must read ``approvals.subagent_mode`` — the key ``_SUBAGENT_CTX``
    advertises in the message it shows the user."""
    assert approval._SUBAGENT_CTX.cfg_key == "subagent_mode"
    with patch(
        "tools.approval_context._binary_approval_mode", return_value="approve"
    ) as binary:
        assert _get_subagent_approval_mode() == "approve"
    assert binary.call_args.args == ("subagent_mode",)


def test_subagent_getter_passes_through_deny_default():
    """Deny-by-default: an unconfigured subagent context must not silently approve.
    ``_binary_approval_mode`` owns the default; the getter must not override it."""
    with patch(
        "tools.approval_context._binary_approval_mode", return_value="deny"
    ) as binary:
        assert _get_subagent_approval_mode() == "deny"
    assert binary.call_args.args == ("subagent_mode",)
