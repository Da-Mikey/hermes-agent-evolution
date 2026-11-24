# -*- coding: utf-8 -*-
"""Tests for inconclusive samples in the flip gate (issue #287).

The defect: a probe the harness could not evaluate — the runtime safety gate
refused the query, or the untouched install control failed the same case — was
scored as a wrong answer, so the flip gate recorded a P->F against the baseline
and blamed the candidate PR. Two unrelated candidates (#284, #273) were blocked
on the same probe for exactly this reason; #273 could not touch an Ollama query
at all.

An inconclusive sample is not a pass and not a fail. These tests pin the three
behaviours the issue's definition of done names:

(a) a refusal is inconclusive, not a failure;
(b) a genuinely wrong answer still fails;
(c) the flip gate emits no P->F for an inconclusive sample and does not count
    it as a missing probe (a missing probe still blocks).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_flip_gate import (  # noqa: E402
    BLOCK,
    HELD,
    INCONCLUSIVE,
    MISSING_AFTER,
    PROMOTE,
    REGRESSION,
    _inconclusive,
    compare_runs,
    flip_verdict,
    format_table,
)
from evolution_merge_gate import check_flip_gate  # noqa: E402


class TestInconclusiveDetection:
    """The flag is read, never guessed from free text."""

    def test_explicit_flag(self):
        assert _inconclusive({"inconclusive": True}) is True

    def test_flag_wins_over_a_failed_check(self):
        # The scoreboard records the flag *and* the failed checks it could not
        # evaluate; the flag must be what the gate reads.
        assert _inconclusive({"inconclusive": True, "passed": False}) is True

    def test_status_strings(self):
        assert _inconclusive({"status": "inconclusive"}) is True
        assert _inconclusive({"status": "refused"}) is True

    def test_bare_strings(self):
        assert _inconclusive("inconclusive") is True
        assert _inconclusive(" Refused ") is True

    def test_bools_are_never_inconclusive(self):
        assert _inconclusive(True) is False
        assert _inconclusive(False) is False

    def test_free_text_is_not_inconclusive(self):
        # Deliberate: the gate must not start masking real failures because an
        # answer happened to contain the word "blocked". Detecting refusal
        # signatures in the answer is the scoreboard's job.
        assert (
            _inconclusive(
                "Blocked before I could count — the safety gate refused the Ollama query"
            )
            is False
        )
        assert _inconclusive({"status": "fail"}) is False
        assert _inconclusive({"passed": False}) is False

    def test_unrecognised_shapes_are_not_inconclusive(self):
        assert _inconclusive(None) is False
        assert _inconclusive(3) is False


class TestRefusalIsNotARegression:
    """(a) A refusal is inconclusive — the core defect."""

    def test_refused_sample_lands_in_its_own_cell(self):
        before = {"live-ollama-inventory": {"passed": True}}
        after = {"live-ollama-inventory": {"inconclusive": True, "passed": False}}

        table = compare_runs(before, after)

        assert table.cells["live-ollama-inventory"] == INCONCLUSIVE
        assert table.regressions == []
        assert table.inconclusive == ["live-ollama-inventory"]

    def test_refused_sample_is_not_missing_after(self):
        # A missing probe still blocks; an inconclusive one must not hide there.
        table = compare_runs({"p": {"passed": True}}, {"p": {"status": "refused"}})
        assert table.ids_in(MISSING_AFTER) == []

    def test_refusal_does_not_block_the_verdict(self):
        table = compare_runs(
            {"p": {"passed": True}},
            {"p": {"inconclusive": True}, "other": {"passed": True}},
        )
        verdict = flip_verdict(table)
        assert verdict.verdict == PROMOTE
        assert verdict.blocked is False

    def test_persistent_refusal_stays_visible_in_the_verdict_line(self):
        table = compare_runs({"p": {"passed": True}}, {"p": {"inconclusive": True}})
        verdict = flip_verdict(table)

        assert "p" in verdict.inconclusive
        # Named in the line a human reads, so it cannot silently disappear.
        assert "inconclusive" in verdict.reason
        assert "p" in verdict.reason

    def test_inconclusive_baseline_is_not_scored_either(self):
        # A baseline the harness could not evaluate cannot anchor a comparison.
        table = compare_runs({"p": {"inconclusive": True}}, {"p": {"passed": True}})
        assert table.cells["p"] == INCONCLUSIVE
        assert table.fixes == []


class TestWrongAnswerStillFails:
    """(b) A genuinely wrong answer is still a regression."""

    def test_plain_failure_is_still_a_regression(self):
        table = compare_runs({"p": {"passed": True}}, {"p": {"passed": False}})
        assert table.cells["p"] == REGRESSION
        assert flip_verdict(table).verdict == BLOCK

    def test_wrong_answer_next_to_a_refusal_still_blocks(self):
        # The refusal must not launder a real failure sitting beside it.
        table = compare_runs(
            {"refused": {"passed": True}, "wrong": {"passed": True}},
            {"refused": {"inconclusive": True}, "wrong": {"passed": False}},
        )
        verdict = flip_verdict(table)

        assert verdict.verdict == BLOCK
        assert verdict.regressions == ["wrong"]
        assert verdict.inconclusive == ["refused"]

    def test_free_text_failure_is_still_a_failure(self):
        # Unreadable results stay fail-closed: they must not look like a pass.
        table = compare_runs({"p": {"passed": True}}, {"p": {"status": "error"}})
        assert table.cells["p"] == REGRESSION


class TestMergeGateIntegration:
    """(c) The consumer: no P->F, no phantom missing probe, no block."""

    def test_check_flip_gate_returns_no_violation_for_a_refusal(self):
        violations = check_flip_gate(
            {"live-ollama-inventory": {"passed": True}},
            {"live-ollama-inventory": {"inconclusive": True}},
        )
        assert violations == []

    def test_check_flip_gate_still_blocks_a_real_regression(self):
        violations = check_flip_gate({"p": {"passed": True}}, {"p": {"passed": False}})
        assert violations
        assert violations[0].startswith("FLIP_GATE_BLOCK")
        assert "p" in violations[0]

    def test_check_flip_gate_still_blocks_a_missing_probe(self):
        violations = check_flip_gate({"p": {"passed": True}}, {})
        assert violations
        assert "absent after" in violations[0]


class TestReportingSurfaces:
    def test_table_counts_include_inconclusive(self):
        table = compare_runs({"p": {"passed": True}}, {"p": {"inconclusive": True}})
        assert table.to_dict()["counts"][INCONCLUSIVE] == 1
        assert table.to_dict()["cells"]["p"] == INCONCLUSIVE

    def test_verdict_dict_carries_the_list(self):
        table = compare_runs({"p": {"passed": True}}, {"p": {"inconclusive": True}})
        payload = flip_verdict(table).to_dict()
        assert payload["inconclusive"] == ["p"]
        assert payload["verdict"] == PROMOTE

    def test_format_table_names_inconclusive_probes(self):
        table = compare_runs(
            {"p": {"passed": True}, "q": {"passed": True}},
            {"p": {"inconclusive": True}, "q": {"passed": True}},
        )
        rendered = format_table(table, flip_verdict(table))
        assert "inconclusive 1" in rendered
        assert f"{HELD} 1" in rendered
        assert "inconclusive (not scored as a regression): p" in rendered
