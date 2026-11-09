"""#357 — the judge must name the tool before judging its result.

``agent/loop_guard._looks_like_failure`` became payload-aware in #350: for a
content-returning tool (``read_file``, ``search_files``, …) the result IS the
payload, so quoting "error:" inside a document says nothing about whether the
call worked.  ``agent_judge`` kept calling it with the content alone, so the
payload exemption never reached the judge's failure count.

Each case below fails on the unpatched module: the lenient marker scan counted
a successful ``read_file`` of an error-quoting document as a failed call.
"""

from agent.agent_judge import (
    replay_trace_steps,
    score_replayed_trace,
    summarize_trace,
)

# A document body — a successful read_file whose CONTENT mentions failures.
DOC = (
    "Incident review\n\n"
    "At 03:14 the job exited with error: connection reset. A second run timed\n"
    "out after 60s. Permission denied was logged once by the crawler.\n"
)


def _trace(result_msg, tool="read_file"):
    return [
        {"role": "user", "content": "read the review"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "function": {"name": tool}}],
        },
        result_msg,
        {"role": "assistant", "content": "The review is summarised above."},
    ]


class TestJudgeReadsToolName:
    def test_payload_quoting_errors_is_not_a_failure(self):
        # Name carried on the result message (tool_dispatch_helpers shape).
        summary = summarize_trace(
            _trace({"role": "tool", "name": "read_file", "content": DOC})
        )
        assert summary.tool_results == 1
        assert summary.tool_failures == 0

    def test_tool_name_resolved_from_tool_call_id(self):
        # The codex projector / MCP sampling shapes carry only the call id.
        summary = summarize_trace(
            _trace({"role": "tool", "tool_call_id": "c1", "content": DOC})
        )
        assert summary.tool_results == 1
        assert summary.tool_failures == 0

    def test_real_error_still_detected_for_payload_tool(self):
        # A genuine payload-tool failure: bare prose that OPENS with the error.
        summary = summarize_trace(
            _trace({
                "role": "tool",
                "name": "read_file",
                "content": "Error: no such file or directory: notes/x.md",
            })
        )
        assert summary.tool_failures == 1

    def test_unknown_tool_keeps_lenient_scan(self):
        # No recoverable name → old behaviour, so nothing is silently lost.
        summary = summarize_trace(_trace({"role": "tool", "content": DOC}))
        assert summary.tool_failures == 1

    def test_shell_result_still_judged_by_markers(self):
        summary = summarize_trace(
            _trace(
                {
                    "role": "tool",
                    "name": "terminal",
                    "content": "pytest\n...\nerror: 3 failed",
                },
                tool="terminal",
            )
        )
        assert summary.tool_failures == 1


class TestReplayScoresWithToolName:
    def test_payload_step_scores_one(self):
        steps = score_replayed_trace(
            "s-357", _trace({"role": "tool", "name": "read_file", "content": DOC})
        )["steps"]
        assert steps[0]["tool_failure_count"] == 0
        assert steps[0]["score"] == 1.0

    def test_replay_resolves_name_from_call_id(self):
        steps = list(
            replay_trace_steps(
                _trace({"role": "tool", "tool_call_id": "c1", "content": DOC})
            )
        )
        assert steps[0]["tool_calls"][0]["id"] == "c1"
        result = score_replayed_trace(
            "s-357", _trace({"role": "tool", "tool_call_id": "c1", "content": DOC})
        )
        assert result["steps"][0]["tool_failure_count"] == 0
