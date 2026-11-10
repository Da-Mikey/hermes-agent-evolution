"""Shared helpers for classifying tool result payloads."""

from __future__ import annotations

import json
from typing import Any


FILE_MUTATING_TOOL_NAMES = frozenset({"write_file", "patch"})


# Tools whose interrupted/dangling execution is safe to discard because they
# cannot mutate either external state or Hermes session state. Unknown/plugin/
# MCP tools stay effect-capable by default.
NO_EFFECT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "session_search", "skill_view", "skills_list",
    "web_extract", "web_search", "vision_analyze", "browser_snapshot",
    "browser_get_images", "browser_console", "read_terminal",
})


def tool_may_have_side_effect(tool_name: str) -> bool:
    return tool_name not in NO_EFFECT_TOOL_NAMES


# Set by a tool that REFUSED a call the harness judged redundant (repeated identical
# read/search). The body still carries ``"error"`` so the model reads it as a stop
# signal, but nothing failed: failure classifiers must not count it, or the cheap
# refusal feeds the streak that fires ``repeated_exact_failure_block``.
GUARDRAIL_REFUSAL_KEY = "guardrail_refusal"


def is_guardrail_refusal(result: Any) -> bool:
    """Return True when ``result`` (JSON string or parsed dict) is a harness refusal."""
    data = result
    if isinstance(result, str):
        try:
            data = json.loads(result.strip())
        except Exception:
            return False
    return isinstance(data, dict) and data.get(GUARDRAIL_REFUSAL_KEY) is True


# The terminal tool's own benign verdict, as published in ``exit_code_meaning``
# by ``tools.terminal_tool_result._interpret_exit_code``: grep/rg rc=1 "no
# matches", diff/cmp rc=1 "files differ", `git grep` rc=1, and `test` rc=1
# "condition false" all carry this phrase. It is computed from the command and
# the code by ``tools.terminal_failure_classifier.is_benign_exit_code`` (#358).
# Matched as a phrase (not the parenthesised form) because the wording varies:
# "No matches found (not an error)" vs "Files differ (expected, not an error)".
BENIGN_EXIT_MEANING_MARKER = "not an error"


def is_benign_terminal_result(result: Any) -> bool:
    """Whether a ``terminal`` payload reports a documented benign exit code.

    A no-match grep / no-difference diff is a successful empty answer — not a
    failure to retry, report, or abandon the tool over. Keyed on the terminal
    tool's own ``exit_code_meaning`` verdict (the command-aware decision), so it
    can never claim success for a code the tool itself treats as an error, and
    a payload carrying a real ``error`` keeps its failure verdict (#358).
    """
    data = result
    if isinstance(result, str):
        try:
            data = json.loads(result.strip())
        except Exception:
            return False
    if not isinstance(data, dict):
        return False
    exit_code = data.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int) or exit_code == 0:
        return False
    meaning = data.get("exit_code_meaning")
    if not isinstance(meaning, str) or BENIGN_EXIT_MEANING_MARKER not in meaning:
        return False
    return not data.get("error")


def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """Return True when a file mutation result proves the write landed."""
    if tool_name not in FILE_MUTATING_TOOL_NAMES or not isinstance(result, str):
        return False
    try:
        data = json.loads(result.strip())
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    if tool_name == "write_file":
        return "bytes_written" in data
    if tool_name == "patch":
        return data.get("success") is True
    return False
