"""#350 — the loop guard must not read a file's contents as a tool failure.

``_looks_like_failure`` scanned the tool result text for failure words and never
saw the tool's name. For a content-returning tool the result *is* the payload, so
one ``read_file`` of a file that merely quotes the gate phrase scored
``safety_gate_blocked`` — a category that bypasses every threshold and is
appended as a **user-role** "do not retry or bypass" directive, stopping the run.
210 such directives are recorded in this install's state.db.

The contracts pinned here:

* a successful ``read_file`` whose body contains every failure marker is not a
  failure, and produces no nudge on a one-call history (the issue's own
  reproduction);
* a genuine read failure still is one, and still carries its class;
* a real gate refusal still produces the directive — the fix narrows the
  evidence, it does not switch the safety branch off; and
* a *command* result that merely quotes the gate phrase is not a refusal either.
"""

import json

import pytest

from agent import loop_guard

GATE_PHRASE = "Tool execution blocked by safety gate: destructive command detected"

# Every marker the guard scans for, plus the gate phrase, in one file body.
POISONED_BODY = "\n".join(
    [
        "1|Notes on the guard's own vocabulary — nothing here failed.",
        "2|command not found",
        "3|no such file",
        "4|permission denied",
        "5|access denied",
        "6|refusing to write",
        "7|forbidden",
        "8|timed out",
        "9|timeout",
        "10|traceback (most recent call last)",
        "11|closedresourceerror",
        "12|unreachable",
        "13|externally-managed-environment",
        "14|error:",
        "15|failed",
        "16|exit status 1",
        "17|is not recognized",
        "18|could not be found",
        "19|no results found",
        f"20|{GATE_PHRASE}",
    ]
)


def _one_call_history(tool_name, result_content):
    """The issue's reproduction: one assistant tool call and its result."""
    return [
        {"role": "user", "content": "look at the file"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": tool_name, "arguments": json.dumps({"path": "x.py"})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": result_content},
    ]


class TestContentPayloadIsNotAFailure:
    def test_marker_scan_is_skipped_for_a_content_tool(self):
        assert loop_guard._looks_like_failure(POISONED_BODY, "read_file") is False

    @pytest.mark.parametrize("tool_name", sorted(loop_guard._CONTENT_PAYLOAD_TOOLS))
    def test_every_content_tool_is_covered(self, tool_name):
        assert loop_guard._looks_like_failure(POISONED_BODY, tool_name) is False

    def test_command_output_with_markers_is_still_scanned(self):
        """The tightening is scoped: command output keeps the marker scan."""
        assert loop_guard._looks_like_failure("bash: foo: command not found", "terminal") is True

    def test_no_nudge_on_a_single_successful_read_of_a_poisoned_file(self):
        """The issue's reproduction, end to end through the real maybe_nudge()."""
        messages = _one_call_history("read_file", POISONED_BODY)

        assert loop_guard.maybe_nudge(messages) is None


class TestGenuineFailuresStillSeen:
    def test_a_real_read_failure_is_a_failure_with_its_class(self):
        envelope = json.dumps({
            "error": "File not found: /nope.txt",
            "error_class": "not_found",
            "recovery": "File does not exist.",
        })

        assert loop_guard._looks_like_failure(envelope, "read_file") is True
        assert loop_guard._failure_category(envelope, "read_file") == "not_found"

    def test_a_real_gate_refusal_still_yields_the_directive(self):
        """A refusal IS positive evidence — the safety branch must still fire."""
        envelope = json.dumps({"error": GATE_PHRASE, "error_class": "safety_gate_blocked"})
        messages = _one_call_history("read_file", envelope)

        nudge = loop_guard.maybe_nudge(messages)

        assert nudge is not None
        assert "Do not retry or bypass" in nudge

    def test_a_bare_refusal_at_the_head_is_positive_evidence(self):
        """A refusal with no envelope, opening the result, still counts.

        (It is not, by itself, a *failure* — the guard's marker scan never
        recognised the gate phrase as one — but the category it produces must
        not be discarded when the evidence shape is this one.)
        """
        assert loop_guard._safety_refusal_is_positive(GATE_PHRASE) is True
        assert loop_guard._safety_refusal_is_positive(POISONED_BODY) is False


class TestSafetyCategoryNeedsPositiveEvidence:
    def test_the_category_is_not_inferred_from_a_command_body(self):
        """grep output that quotes the gate is not a refusal by the gate."""
        quoted = f"scripts/loop_guard.py:1011: {GATE_PHRASE}\nother: no such file"

        assert loop_guard._failure_category(quoted, "terminal") is None

    def test_gate_phrase_does_not_trip_the_safety_branch_from_a_body(self):
        quoted = "\n".join([
            "grep -rn 'safety gate' agent/loop_guard.py",
            f"agent/loop_guard.py:1011:    return f\"[loop-guard] {GATE_PHRASE}\"",
            "error: no such file",
        ])
        messages = _one_call_history("terminal", quoted)

        nudge = loop_guard.maybe_nudge(messages)

        # A failure may still be nudged on generic thresholds — but never with the
        # safety directive that tells the model to stop and report.
        assert nudge is None or "Do not retry or bypass" not in nudge


class TestEnvelopeErrorText:
    def test_it_reads_only_the_tools_own_fields(self):
        envelope = json.dumps({"content": POISONED_BODY, "path": "x.py"})

        assert loop_guard._envelope_error_text(envelope) == ""

    def test_it_returns_none_for_non_envelope_text(self):
        assert loop_guard._envelope_error_text("plain output") is None
