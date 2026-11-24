# -*- coding: utf-8 -*-
"""Auditability of scoreboard run records (issue #319).

The defect: ``scripts/agent_scoreboard.py`` recorded only a 400-character
``answer_head`` and a bare ``{"label": ..., "ok": ...}`` per check.  When a
``live-state`` case asked "How many of my cron jobs are currently paused?" the
record kept ``answer_head`` = "**14 of your 118 cron jobs are paused.**" next to
``ground truth '11' present -> ok: true``.  Two readings were possible -- '11'
matched as a substring of '118', or '11' stood alone further down the
(unrecorded) answer -- and the record as written could not distinguish them.
The verdict was therefore unfalsifiable after the model call was gone.

These tests pin the definition of done: *given a run record for a live-state
probe, a reader can state which substring satisfied (or failed) the
ground-truth check without re-running the model.*

They drive the REAL emitter -- ``agent_scoreboard.main()``, the function that
opens ``runs/<stamp>.json`` and dumps the record -- with the agent call stubbed.
Nothing here calls a helper the runner does not call.
"""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import agent_scoreboard as sb  # noqa: E402

LIVE_CASE = {
    "id": "live-paused-jobs",
    "domain": "live-state",
    "prompt": "How many of my cron jobs are currently paused?",
    "weight": 1,
    "ground_truth": {
        "cmd": "printf '11\\n'",
        "extract": r"^(\d+)$",
    },
    "checks": [
        {
            "type": "must_contain",
            "pattern": r"paused",
            "why": "must answer the question",
        },
    ],
}

#: The answer from the issue's evidence: states 14 while the live value is 11.
WRONG_COUNT_ANSWER = "**14 of your 118 cron jobs are paused.**"


def _emit(tmp_path, monkeypatch, case, answer, extra_args=("--no-history",)):
    """Run the real emitter over one case and return its written run record."""
    cases_path = tmp_path / "cases.json"
    runs_dir = tmp_path / "runs"
    history = tmp_path / "history.jsonl"
    cases_path.write_text(json.dumps({"cases": [case]}), encoding="utf-8")

    monkeypatch.setattr(sb, "CASES", str(cases_path))
    monkeypatch.setattr(sb, "RUNS_DIR", str(runs_dir))
    monkeypatch.setattr(sb, "HISTORY", str(history))
    monkeypatch.setattr(sb, "run_case", lambda c, t: (answer, {"seconds": 0.1}))
    monkeypatch.setattr(sb, "USAGE_TMP", str(tmp_path / "usage.json"))
    monkeypatch.setattr(sys, "argv", ["agent_scoreboard.py", *extra_args])

    assert sb.main() == 0
    written = sorted(runs_dir.glob("*.json"))
    assert len(written) == 1, written
    return json.loads(written[0].read_text(encoding="utf-8"))


def _result(record, case_id="live-paused-jobs"):
    return next(r for r in record["results"] if r["id"] == case_id)


def _gt_check(result):
    return next(c for c in result["checks"] if c["label"].startswith("ground truth"))


class TestRecordCarriesTheAnswer:
    """The answer the checks actually ran on, not just a 400-char head."""

    def test_full_answer_is_recorded(self, tmp_path, monkeypatch):
        answer = (
            WRONG_COUNT_ANSWER
            + "\n"
            + "\n".join(f"{i}. job-{i} — paused" for i in range(1, 15))
        )
        result = _result(_emit(tmp_path, monkeypatch, LIVE_CASE, answer))

        assert result["answer"] == answer
        assert result["answer_chars"] == len(answer)
        assert result["answer_truncated"] is False
        # Backwards compatibility: the compact field existing readers use is
        # still there, still 400 characters.
        assert result["answer_head"] == answer[:400]

    def test_answer_is_bounded_and_says_so(self, tmp_path, monkeypatch):
        answer = "paused " + ("x" * (sb.ANSWER_MAX_CHARS + 1000))
        result = _result(_emit(tmp_path, monkeypatch, LIVE_CASE, answer))

        assert len(result["answer"]) == sb.ANSWER_MAX_CHARS
        assert result["answer_truncated"] is True
        assert result["answer_chars"] == len(answer)
        # The bound must be visible: a reader has to be able to tell a clipped
        # record from a short answer.
        assert result["answer_chars"] > len(result["answer"])


class TestGroundTruthVerdictIsDecidable:
    """Both directions, from the record alone."""

    def test_pass_records_the_pattern_and_the_matched_text(self, tmp_path, monkeypatch):
        answer = "You have 11 paused cron jobs out of 118."
        record = _emit(tmp_path, monkeypatch, LIVE_CASE, answer)
        result = _result(record)
        check = _gt_check(result)

        assert result["pass"] is True
        assert check["ok"] is True
        assert check["matched_pattern"] == r"\b11\b"
        assert check["matched_text"] == "11"

    def test_substring_inside_a_longer_number_does_not_pass(
        self, tmp_path, monkeypatch
    ):
        """The issue's second reading: '11' only exists inside '118'."""
        record = _emit(tmp_path, monkeypatch, LIVE_CASE, WRONG_COUNT_ANSWER)
        result = _result(record)
        check = _gt_check(result)

        assert result["pass"] is False
        assert check["ok"] is False
        assert check["matched_pattern"] is None
        assert check["matched_text"] is None
        # A failing verdict must also show what was tried, or "not present"
        # cannot be told apart from a bad expectation.
        assert r"\b11\b" in check["patterns_tried"]
        assert check["token"] == "11"

    def test_control_substring_search_would_mislabel_the_second_case(self):
        """CONTROL: reimplement the pre-#319 substring behaviour.

        The old check was an unanchored search for the token, which finds '11'
        inside '118'.  This control exists so the boundary logic cannot regress
        silently: if it ever starts agreeing with the real check on this answer,
        the real check has lost its word boundaries.
        """
        haystack = WRONG_COUNT_ANSWER
        control = re.search(re.escape("11"), haystack)

        assert control is not None, "control must exhibit the substring match"
        assert control.group(0) == "11"
        # ... and it is a substring of a longer number, which is why the control
        # is wrong and the recorded check is right.
        assert re.search(r"\d+", haystack[control.start() :]).group(0) == "118"

        ok, pattern, text = sb._search_any([r"\b11\b"], haystack, re.I)
        assert (ok, pattern, text) == (False, None, None)


class TestProbeEvidenceIsRecorded:
    """The live value the check compared against, not just the expected token."""

    def test_probe_command_output_and_tokens(self, tmp_path, monkeypatch):
        result = _result(_emit(tmp_path, monkeypatch, LIVE_CASE, "11 paused jobs"))
        probe = result["probe"]

        assert probe["cmd"] == "printf '11\\n'"
        assert probe["tokens"] == ["11"]
        assert probe["raw"].startswith("11")
        assert probe["raw_truncated"] is False
        assert probe["error"] is None

    def test_probe_output_is_bounded(self, tmp_path, monkeypatch):
        case = json.loads(json.dumps(LIVE_CASE))
        case["ground_truth"]["cmd"] = (
            "printf '11\\n'; head -c 3000 /dev/zero | tr '\\0' 'x'"
        )
        case["ground_truth"]["extract"] = r"(\d+)"
        result = _result(_emit(tmp_path, monkeypatch, case, "11 paused jobs"))
        probe = result["probe"]

        assert len(probe["raw"]) == sb.PROBE_RAW_MAX_CHARS
        assert probe["raw_truncated"] is True
        assert probe["tokens"] == ["11"]

    def test_static_cases_have_no_probe_block(self, tmp_path, monkeypatch):
        case = {
            "id": "fmt-tool-restraint",
            "domain": "format",
            "prompt": "What is 2 + 2?",
            "checks": [{"type": "max_words", "n": 3}],
        }
        record = _emit(tmp_path, monkeypatch, case, "Four.", extra_args=())
        result = _result(record, "fmt-tool-restraint")

        assert "probe" not in result
        assert result["answer"] == "Four."


class TestBackwardsCompatibility:
    """Existing readers (scoreboard_report.py, the flip gate) keep working."""

    def test_summary_fields_and_check_shape_survive(self, tmp_path, monkeypatch):
        record = _emit(tmp_path, monkeypatch, LIVE_CASE, "11 paused jobs")
        result = _result(record)

        for key in (
            "at",
            "score",
            "cases",
            "passed",
            "cost_usd",
            "partial",
            "results",
            "judged_count",
            "inconclusive_count",
        ):
            assert key in record
        for key in (
            "id",
            "domain",
            "pass",
            "inconclusive",
            "attempts",
            "weight",
            "checks",
            "answer_head",
        ):
            assert key in result
        # scoreboard_report.py reads c["label"] and c["ok"] off failed checks.
        for check in result["checks"]:
            assert isinstance(check["label"], str)
            assert isinstance(check["ok"], bool)

    def test_history_line_unchanged(self, tmp_path, monkeypatch):
        _emit(tmp_path, monkeypatch, LIVE_CASE, "11 paused jobs", extra_args=())
        line = (tmp_path / "history.jsonl").read_text(encoding="utf-8").strip()

        assert set(json.loads(line)) == {"at", "score", "cases", "passed", "cost_usd"}


@pytest.mark.parametrize("answer", ["You have eleven paused jobs.", "11 paused jobs"])
def test_ground_truth_accepts_numeral_and_word(tmp_path, monkeypatch, answer):
    result = _result(_emit(tmp_path, monkeypatch, LIVE_CASE, answer))
    check = _gt_check(result)

    assert result["pass"] is True
    assert check["ok"] is True
    assert check["matched_text"].lower() in ("11", "eleven")
