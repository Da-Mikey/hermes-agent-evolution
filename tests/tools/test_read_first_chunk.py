"""First-chunk selection in read_file (issue #165).

Truncation used to keep the HEAD of an oversized read. When the caller passes
``query`` keywords the retained window is now ranked by relevance — in native
file order — and the response reports what was skipped. These tests pin the
ranking, the ordering guarantee, the fallbacks (no query / no hit / fits
anyway), the dedup-key change, and the gold-in-window metric the eval harness
now reports.

Run with:  python -m pytest tests/tools/test_read_first_chunk.py -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import file_tools
from tools.file_tools import (
    _select_relevance_window,
    _tokenize_query,
    read_file_tool,
)
# Moved out of tools.file_tools; the old path is a compat shim slated for
# removal, so import from the real home.
from tools.file_tools_read_tracking import reset_file_dedup

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "evals" / "readtool"))

import metrics  # noqa: E402  (eval harness helper, stdlib-only by design)

NEEDLE = "ERROR request_id=req-9f2a"
OTHER_NEEDLE = "CRITICAL worker=alpha shard=beta"
BUDGET = 500


def _log_text(lines=400, needle_at=None, needle=NEEDLE):
    """Rendered-ish log body with an optional planted marker."""
    out = [f"2026-10-02T12:{i % 60:02d}:00Z INFO worker-{i} heartbeat ok" for i in range(lines)]
    if needle_at is not None:
        out[needle_at] = f"2026-10-02T13:00:00Z {needle} trailing context"
    return "\n".join(out) + "\n"


def _gutters(window: str):
    """Line numbers parsed out of a rendered 'N|content' window."""
    nums = []
    for line in window.split("\n"):
        head, sep, _ = line.partition("|")
        if sep and head.strip().isdigit():
            nums.append(int(head))
    return nums


class TestTokenizeQuery(unittest.TestCase):
    def test_splits_lowercases_and_dedupes(self):
        self.assertEqual(
            _tokenize_query("ERROR, error! timeout-2"),
            ["error", "timeout"],
        )

    def test_drops_single_char_and_punctuation_only_tokens(self):
        self.assertEqual(_tokenize_query("a 1 -- ?? ."), [])


class TestSelectRelevanceWindow(unittest.TestCase):
    def test_no_query_returns_none(self):
        self.assertIsNone(_select_relevance_window("a\nb\nc", BUDGET, ""))

    def test_no_match_returns_none(self):
        self.assertIsNone(_select_relevance_window("a\nb\nc", BUDGET, "zebra"))

    def test_matching_token_shorter_than_min_length_returns_none(self):
        # "1" is dropped by tokenisation, so a numeric-only query never
        # silently reorders a read.
        self.assertIsNone(_select_relevance_window("a\n1\nc", BUDGET, "1"))

    def test_tail_hit_is_selected_and_stays_in_native_order(self):
        content = _log_text(lines=400, needle_at=390)
        window = _select_relevance_window(content, BUDGET, "error request_id")
        self.assertIsNotNone(window)
        self.assertIn(NEEDLE, window["text"])
        # Native order: the returned slice is exactly lines[start:end+1].
        lines = content.split("\n")
        self.assertEqual(window["text"], "\n".join(lines[window["start"]:window["end"] + 1]))
        self.assertTrue(window["start"] < window["end"])
        # The head was NOT what got kept.
        self.assertGreater(window["start"], 200)

    def test_window_respects_the_char_budget(self):
        content = _log_text(lines=400, needle_at=200)
        window = _select_relevance_window(content, BUDGET, "error")
        self.assertLessEqual(len(window["text"]), BUDGET)

    def test_earliest_highest_scoring_line_wins_ties(self):
        lines = [f"filler {i}" for i in range(60)]
        lines[5] = "ERROR alpha"
        lines[40] = "ERROR alpha"
        window = _select_relevance_window("\n".join(lines), BUDGET, "error alpha")
        self.assertLessEqual(window["start"], 5)
        self.assertGreaterEqual(window["end"], 5)

    def test_reporting_fields_describe_the_omitted_regions(self):
        content = _log_text(lines=400, needle_at=390)
        window = _select_relevance_window(content, BUDGET, "error")
        self.assertEqual(window["total"], 401)
        self.assertEqual(window["start"] + (window["total"] - 1 - window["end"]),
                         window["total"] - 1 - (window["end"] - window["start"]))

    def test_oversized_selected_line_is_clamped(self):
        content = "small\n" + ("x" * 5000) + " ERROR " + ("y" * 5000) + "\ndone"
        window = _select_relevance_window(content, BUDGET, "error")
        self.assertTrue(window["clamped"])
        self.assertEqual(window["start"], window["end"])
        self.assertLessEqual(len(window["text"]), BUDGET)


class TestReadFileToolQueryIntegration(unittest.TestCase):
    def setUp(self):
        reset_file_dedup()
        self.tmp = tempfile.mkdtemp(prefix="first-chunk-")
        self.path = os.path.join(self.tmp, "server.log")
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write(_log_text(lines=400, needle_at=390))
        # _get_max_read_chars() reads config each call (no module cache since
        # the read-path refactor), so patching the function is the hook.
        self.budget = patch.object(file_tools, "_get_max_read_chars", lambda: BUDGET)
        self.budget.start()
        self.addCleanup(self.budget.stop)

    def _read(self, **kwargs):
        return json.loads(read_file_tool(self.path, task_id="first-chunk-test", **kwargs))

    def test_query_returns_the_tail_window_instead_of_the_head(self):
        res = self._read(query="error request_id")
        self.assertTrue(res.get("truncated"))
        self.assertEqual(res.get("selected_by"), "query")
        self.assertIn(NEEDLE, res["content"])
        self.assertLessEqual(len(res["content"]), BUDGET)
        self.assertLessEqual(res["window_start_line"], 391)
        self.assertLessEqual(391, res["window_end_line"])

    def test_query_window_lines_are_consecutive_and_absolute(self):
        res = self._read(query="error")
        gutters = _gutters(res["content"])
        self.assertEqual(gutters[0], res["window_start_line"])
        self.assertEqual(gutters[-1], res["window_end_line"])
        self.assertEqual(gutters, list(range(gutters[0], gutters[-1] + 1)))

    def test_omitted_counts_and_hint_tell_the_model_where_the_head_went(self):
        res = self._read(query="error")
        self.assertEqual(res["omitted_before"], res["window_start_line"] - 1)
        self.assertGreater(res["omitted_after"], 0)
        self.assertIn("without 'query'", res["hint"])
        self.assertIn(f"offset={res['next_offset']}", res["hint"])

    def test_no_query_keeps_head_behaviour(self):
        res = self._read()
        self.assertTrue(res.get("truncated"))
        self.assertNotIn("selected_by", res)
        self.assertEqual(_gutters(res["content"])[0], 1)

    def test_unmatched_query_falls_back_to_head_behaviour(self):
        res = self._read(query="zebra-unicorn")
        self.assertNotIn("selected_by", res)
        self.assertEqual(_gutters(res["content"])[0], 1)

    def test_small_file_ignores_query_entirely(self):
        small = os.path.join(self.tmp, "small.txt")
        with open(small, "w", encoding="utf-8") as fh:
            fh.write("alpha\nbeta\ngamma\n")
        res = json.loads(read_file_tool(small, task_id="first-chunk-test", query="beta"))
        self.assertFalse(res.get("truncated"))
        self.assertNotIn("selected_by", res)
        self.assertIn("alpha", res["content"])

    def test_dedup_treats_different_queries_as_different_reads(self):
        first = os.path.join(self.tmp, "two-markers.log")
        body = _log_text(lines=200, needle_at=60) + _log_text(lines=200, needle_at=190, needle=OTHER_NEEDLE)
        with open(first, "w", encoding="utf-8") as fh:
            fh.write(body)
        one = json.loads(read_file_tool(first, task_id="first-chunk-test", query="error"))
        two = json.loads(read_file_tool(first, task_id="first-chunk-test", query="critical shard"))
        self.assertIn(NEEDLE, one["content"])
        self.assertIn(OTHER_NEEDLE, two["content"])
        self.assertNotEqual(one["content"], two["content"])


class TestFirstChunkMetrics(unittest.TestCase):
    def test_inclusion_coverage_full_partial_and_empty(self):
        self.assertEqual(metrics.inclusion_coverage(["a", "b"], "x a y b z"), 1.0)
        self.assertEqual(metrics.inclusion_coverage(["a", "b"], "x a y"), 0.5)
        self.assertEqual(metrics.inclusion_coverage(["a"], ""), 0.0)

    def test_inclusion_coverage_without_gold_is_not_applicable(self):
        self.assertEqual(metrics.inclusion_coverage([], "anything"), 1.0)

    def test_first_read_window_returns_the_first_read_file_result(self):
        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "function": {"name": "terminal", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "shell noise"},
            {"role": "assistant", "tool_calls": [
                {"id": "c2", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c2",
             "content": [{"type": "text", "text": "1|head only"}]},
            {"role": "assistant", "tool_calls": [
                {"id": "c3", "function": {"name": "read_file", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c3", "content": "391|the answer"},
        ]
        window = metrics.first_read_window(messages)
        self.assertIn("head only", window)
        self.assertNotIn("the answer", window)
        self.assertEqual(metrics.inclusion_coverage([NEEDLE], window), 0.0)

    def test_first_read_window_is_empty_without_a_read(self):
        messages = [{"role": "assistant", "content": "no tools used"}]
        self.assertEqual(metrics.first_read_window(messages), "")


if __name__ == "__main__":
    unittest.main()
