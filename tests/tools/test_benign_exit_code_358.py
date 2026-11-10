"""Regression tests for #358 — a benign exit code is not a persistent error.

``grep``/``rg`` exit 1 ("no matches") and ``diff``/``cmp`` exit 1 ("differences
found") are successes with an empty answer. They used to be stamped
``⚠️ Non-retryable: persistent_error`` on the model's tool result, which told the
agent to abandon a search that had just worked, and made the runtime's failure
census disagree with ``scripts/introspection_extract.py`` about the same
payload. The matrix below pins both directions: the benign codes stop being
failures, and every genuinely informative verdict (rc=2, 126, 127, 130, 141,
signal deaths, error payloads) keeps its old classification and its stamp.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.display import _detect_tool_failure
from agent.tool_result_classification import is_benign_terminal_result
from agent.tool_guardrails import ToolCallGuardrailController
from tools import tool_failure_classifier as tfc
from tools.terminal_failure_classifier import (
    FailureCategory,
    classify_terminal_failure,
    is_benign_exit_code,
)
from tools.terminal_tool_result import _interpret_exit_code


def _payload(command: str, exit_code: int, output: str = "", error=None) -> str:
    """A terminal payload shaped exactly like the tool's own JSON result."""
    return json.dumps({
        "output": output,
        "exit_code": exit_code,
        "error": error,
        "exit_code_meaning": _interpret_exit_code(command, exit_code),
    })


# ---------------------------------------------------------------------------
# the predicate itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "grep -n FOO notes.txt",
        "egrep -n FOO notes.txt",
        "fgrep FOO notes.txt",
        "rg -n FOO notes.txt",
        "ag FOO",
        "ack FOO",
        "/usr/bin/grep -n FOO notes.txt",
        "diff a.txt b.txt",
        "colordiff a.txt b.txt",
        "cmp a.bin b.bin",
        "git grep FOO",
        "git -C /repo grep FOO",
        "git --no-pager grep FOO",
        "cat notes.txt | grep FOO",
        "FOO=1 grep FOO notes.txt",
    ],
)
def test_documented_benign_code_is_benign(command):
    assert is_benign_exit_code(command, 1) is True


@pytest.mark.parametrize(
    "command, exit_code",
    [
        ("grep foo missing.txt", 2),  # grep's own "generic error"
        ("grep foo x", 126),  # found but not executable
        ("grep foo x", 127),  # shell never found the command
        ("grep foo x", 130),  # SIGINT
        ("grep foo x", 141),  # SIGPIPE
        ("grep foo x", -9),  # subprocess signal encoding
        ("grep foo x", 0),  # success is not "benign"
        ("false", 1),  # a real failure, not an empty search
        ("ls /nonexistent", 2),
        ("git diff", 1),  # broad git rc=1 stays a failure
        ("git status", 1),
        ("git", 1),
        ("find . -name x", 1),  # partial results, not "nothing found"
        ("test -f missing", 1),
        ("", 1),
    ],
)
def test_non_benign_codes_are_not_benign(command, exit_code):
    assert is_benign_exit_code(command, exit_code) is False


# ---------------------------------------------------------------------------
# terminal classifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "grep -n FOO notes.txt",
        "rg -n FOO notes.txt",
        "diff a.txt b.txt",
        "cmp a.bin b.bin",
        "git grep FOO",
    ],
)
def test_benign_exit_is_its_own_category(command):
    result = classify_terminal_failure(command, 1, "", "")
    assert result.category is FailureCategory.benign_exit_code
    assert result.should_retry is False
    assert "not an error" in result.hint or "nothing to report" in result.hint


@pytest.mark.parametrize(
    "command, exit_code, expected",
    [
        # unchanged classifications — the point of the fix is that these still fire
        ("grep foo missing.txt", 2, FailureCategory.unknown),
        ("grep foo x", 126, FailureCategory.permission_denied),
        ("grep foo x", 127, FailureCategory.missing_command),
        ("grep foo x", 130, FailureCategory.persistent_error),
        ("grep foo x", 141, FailureCategory.persistent_error),
        ("false", 1, FailureCategory.persistent_error),
        ("git diff", 1, FailureCategory.unknown),
        ("ls /nonexistent", 2, FailureCategory.persistent_error),
    ],
)
def test_real_failures_keep_their_category(command, exit_code, expected):
    result = classify_terminal_failure(command, exit_code, "", "")
    assert result.category is expected
    assert result.should_retry is False


def test_error_text_outranks_the_benign_code():
    """A benign code plus an error payload is still the error it reports."""
    denied = classify_terminal_failure(
        "grep foo /root/x", 1, "", "grep: /root/x: Permission denied"
    )
    assert denied.category is FailureCategory.permission_denied

    missing = classify_terminal_failure(
        "grep foo x", 1, "", "bash: grep: command not found"
    )
    assert missing.category is FailureCategory.missing_command


def test_shared_classifier_sees_the_command():
    """``classify_tool_failure`` must forward the command, not classify blind."""
    payload = _payload("grep -n FOO notes.txt", 1)
    result = tfc.classify_tool_failure(
        "terminal", payload, exit_code=1, command="grep -n FOO notes.txt"
    )
    assert result.category is tfc.ToolFailureCategory.benign_exit_code
    assert result.category is not tfc.ToolFailureCategory.persistent_error
    assert result.category is not tfc.ToolFailureCategory.unknown
    assert result.should_retry is False


def test_benign_category_is_not_in_the_persistent_bucket():
    shared = tfc._TERMINAL_CATEGORY_MAP[FailureCategory.benign_exit_code]
    assert shared is tfc.ToolFailureCategory.benign_exit_code
    assert tfc._CATEGORY_RETRYABLE[shared] is False
    assert shared not in {
        tfc.ToolFailureCategory.persistent_error,
        tfc.ToolFailureCategory.unknown,
    }


# ---------------------------------------------------------------------------
# display mirror + guardrail mirror
# ---------------------------------------------------------------------------


def test_display_does_not_call_a_no_match_a_failure():
    assert _detect_tool_failure("terminal", _payload("grep -n FOO notes.txt", 1)) == (
        False,
        "",
    )
    assert _detect_tool_failure("terminal", _payload("rg FOO notes.txt", 1)) == (
        False,
        "",
    )
    assert _detect_tool_failure("terminal", _payload("diff a b", 1)) == (False, "")
    assert _detect_tool_failure("terminal", _payload("cmp a b", 1)) == (False, "")


@pytest.mark.parametrize(
    "payload",
    [
        _payload("grep FOO x", 1),
        _payload("diff a b", 1),
        _payload("grep foo missing.txt", 2),
        _payload("grep foo x", 127),
        _payload("grep foo x", 126),
        _payload("grep foo x", 130),
        _payload("grep foo x", 141),
        _payload("false", 1),
        _payload("grep FOO x", 1, error="grep: boom"),
        json.dumps({"output": "hello", "exit_code": 0}),
        "not json at all",
    ],
)
def test_display_and_guardrail_mirror_agree(payload):
    """``agent.tool_guardrails`` must mirror ``agent.display``'s failure verdict.

    Only the boolean is compared: the two differ in the cosmetic suffix they
    return for a payload with an ``error`` string (display shows the message,
    the guardrail shows ``[exit N]``) — pre-existing and unrelated to #358.
    """
    assert (
        _detect_tool_failure("terminal", payload)[0]
        is tfc_guardrail_failure(payload)[0]
    )


def tfc_guardrail_failure(payload):
    from agent.tool_guardrails import classify_tool_failure as guardrail_classifier

    return guardrail_classifier("terminal", payload)


def test_error_payload_keeps_its_failure_verdict():
    """A payload carrying a real ``error`` is never benign, whatever the note says."""
    payload = _payload("grep FOO x", 1, output="", error="grep: invalid option -- 'z'")
    assert is_benign_terminal_result(payload) is False


# ---------------------------------------------------------------------------
# the stamp itself (run_agent path)
# ---------------------------------------------------------------------------


def _observer():
    return SimpleNamespace(
        _tool_guardrails=ToolCallGuardrailController(),
        _stall_guards_enabled=lambda: False,
        _failure_recovery_enabled=False,
    )


@pytest.mark.parametrize(
    "command",
    [
        "grep -n FOO notes.txt",
        "rg -n FOO notes.txt",
        "diff a.txt b.txt",
        "git grep FOO",
    ],
)
def test_no_non_retryable_stamp_for_a_benign_exit(command):
    from run_agent import AIAgent

    payload = _payload(command, 1)
    observed = AIAgent._append_guardrail_observation(
        _observer(),
        "terminal",
        {"command": command},
        payload,
        failed=True,
    )
    assert "Non-retryable" not in observed
    assert json.loads(observed)["exit_code"] == 1


@pytest.mark.parametrize(
    "command, exit_code, expected",
    [
        ("false", 1, "persistent_error"),
        ("grep foo missing.txt", 2, "unknown"),
        ("grep foo x", 127, "tool_unavailable"),
        ("grep foo x", 126, "permission_denied"),
        ("grep foo x", 130, "persistent_error"),
        ("grep foo x", 141, "persistent_error"),
    ],
)
def test_real_failure_still_gets_the_stamp(command, exit_code, expected):
    from run_agent import AIAgent

    payload = _payload(command, exit_code)
    observed = AIAgent._append_guardrail_observation(
        _observer(),
        "terminal",
        {"command": command},
        payload,
        failed=True,
    )
    assert f"Non-retryable: {expected}" in observed


# ---------------------------------------------------------------------------
# the note and the verdict cannot drift apart
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    ["grep -n FOO notes.txt", "rg FOO x", "diff a b", "cmp a b", "git grep FOO"],
)
def test_note_and_verdict_agree(command):
    payload = _payload(command, 1)
    meaning = json.loads(payload)["exit_code_meaning"]
    assert meaning and "not an error" in meaning
    assert is_benign_terminal_result(payload) is True
