# -*- coding: utf-8 -*-
"""Tests for headroom-normalised scoring (issue #208).

Covers every definition-of-done clause: the fraction is computed against the
remaining gap, the raw delta is kept as a secondary column, and each degenerate
case (at ceiling, regression, missing sample, empty set) is handled explicitly
rather than silently.
"""

from __future__ import annotations

import pytest

from evolution.lib.headroom_score import (
    AT_CEILING,
    MISSING_AFTER,
    MISSING_BEFORE,
    SCORED,
    EditScore,
    TaskScore,
    gap_closed_fraction,
    score_edit,
    to_stage_result,
)


class TestGapClosedFraction:
    def test_closes_half_the_remaining_gap(self):
        # 0.0 -> 0.5 against a ceiling of 1.0 closes half the gap.
        assert gap_closed_fraction(0.0, 0.5, 1.0) == pytest.approx(0.5)

    def test_closes_the_whole_gap(self):
        assert gap_closed_fraction(0.25, 1.0, 1.0) == pytest.approx(1.0)

    def test_no_movement_is_zero(self):
        assert gap_closed_fraction(0.4, 0.4, 1.0) == pytest.approx(0.0)

    def test_regression_is_negative(self):
        # 0.8 -> 0.6 against 1.0 loses 0.2 of a 0.2 gap: the whole thing.
        assert gap_closed_fraction(0.8, 0.6, 1.0) == pytest.approx(-1.0)

    def test_regression_from_a_small_gap_is_clamped(self):
        # A large drop against a tiny gap would exceed 100%: clamp at -1.0.
        assert gap_closed_fraction(0.9, 0.0, 1.0) == pytest.approx(-1.0)

    def test_gain_beyond_ceiling_is_clamped(self):
        # A pass rate above its ceiling is a data error, not a >100% gain.
        assert gap_closed_fraction(0.5, 1.4, 1.0) == pytest.approx(1.0)

    def test_at_ceiling_returns_none(self):
        # No remaining gap: there is nothing to normalise against.
        assert gap_closed_fraction(1.0, 1.0, 1.0) is None

    def test_non_positive_ceiling_rejected(self):
        with pytest.raises(ValueError):
            gap_closed_fraction(0.0, 0.5, 0.0)

    def test_partial_ceiling(self):
        # Ceilings need not be 1.0: 2.0 -> 6.0 against 10.0 closes half of 8.
        assert gap_closed_fraction(2.0, 6.0, 10.0) == pytest.approx(0.5)


class TestScoreEdit:
    def test_identical_raw_delta_scores_differently(self):
        """The whole point of the issue: same delta, different headroom."""
        cheap = score_edit({"easy": 0.90}, {"easy": 0.95})
        hard = score_edit({"hard": 0.00}, {"hard": 0.05})

        assert cheap.raw_delta == pytest.approx(hard.raw_delta)
        # The hard task had all its gap left, so 5 points closes 5% of it...
        assert hard.headroom_score == pytest.approx(0.05)
        # ...while the easy task had only 10 points of headroom left, so the
        # same 5 points closes half of what remained.
        assert cheap.headroom_score == pytest.approx(0.5)
        assert cheap.headroom_score > hard.headroom_score

    def test_raw_delta_is_retained_as_secondary_column(self):
        score = score_edit({"a": 0.2}, {"a": 0.6})
        assert score.raw_delta == pytest.approx(0.4)
        assert score.headroom_score == pytest.approx(0.5)
        payload = score.to_dict()
        assert payload["raw_delta"] == pytest.approx(0.4)
        assert payload["headroom_score"] == pytest.approx(0.5)

    def test_at_ceiling_task_does_not_inflate_the_mean(self):
        """A task already at ceiling contributes no gap and must not be counted."""
        score = score_edit(
            before={"done": 1.0, "hard": 0.0},
            after={"done": 1.0, "hard": 0.5},
        )
        # Only the hard task is scored, so the mean is its fraction alone.
        assert score.headroom_score == pytest.approx(0.5)
        assert score.at_ceiling == ("done",)
        assert len(score.scored_tasks) == 1

    def test_at_ceiling_task_does_not_dilute_the_mean(self):
        # If the ceiling task were averaged in as 0.0 the score would halve.
        score = score_edit(
            before={"done": 1.0, "hard": 0.0},
            after={"done": 1.0, "hard": 1.0},
        )
        assert score.headroom_score == pytest.approx(1.0)

    def test_regression_from_ceiling_is_flagged_not_scored(self):
        score = score_edit({"done": 1.0}, {"done": 0.9})
        task = score.tasks[0]
        assert task.regime == AT_CEILING
        assert task.gap_closed is None
        assert task.regressed is True
        # No fraction exists, but the edit must still be visibly a regression.
        assert score.has_regression is True
        assert score.regressed == ("done",)

    def test_regression_contributes_a_negative_fraction(self):
        # 0.6 -> 0.4 leaves 0.4 of gap to lose, and loses exactly half of it.
        score = score_edit({"hard": 0.6}, {"hard": 0.4})
        assert score.headroom_score == pytest.approx(-0.5)
        assert score.has_regression is True

    def test_mixed_gains_and_regressions_net_out(self):
        score = score_edit(
            before={"a": 0.0, "b": 0.5},
            after={"a": 0.5, "b": 0.25},
        )
        # a closes half its gap (+0.5), b loses half of its gap (-0.5).
        assert score.headroom_score == pytest.approx(0.0)
        assert score.moved == ("a", "b")

    def test_empty_set_scores_zero_and_does_not_raise(self):
        score = score_edit({}, {})
        assert score.raw_delta == 0.0
        assert score.headroom_score == 0.0
        assert score.tasks == ()
        assert score.moved == ()
        assert score.has_regression is False

    def test_all_at_ceiling_scores_zero(self):
        score = score_edit({"a": 1.0}, {"a": 1.0})
        assert score.headroom_score == 0.0
        assert score.at_ceiling == ("a",)

    def test_missing_after_is_reported_not_averaged(self):
        score = score_edit(
            before={"a": 0.0, "gone": 0.5},
            after={"a": 1.0},
        )
        # The vanished task must not appear as a 0.0 and drag the mean down.
        assert score.headroom_score == pytest.approx(1.0)
        assert score.unscored == ("gone",)
        gone = next(t for t in score.tasks if t.task == "gone")
        assert gone.regime == MISSING_AFTER

    def test_missing_before_is_reported(self):
        score = score_edit(before={}, after={"new": 0.5})
        assert score.tasks[0].regime == MISSING_BEFORE
        assert score.unscored == ("new",)
        assert score.headroom_score == 0.0

    def test_per_task_ceilings(self):
        score = score_edit(
            before={"half": 0.0},
            after={"half": 5.0},
            ceilings={"half": 10.0},
        )
        assert score.headroom_score == pytest.approx(0.5)
        assert score.tasks[0].ceiling == 10.0

    def test_missing_ceiling_falls_back_to_perfect(self):
        score = score_edit(
            before={"x": 0.0},
            after={"x": 0.5},
            ceilings={"other": 10.0},
        )
        assert score.tasks[0].ceiling == 1.0
        assert score.headroom_score == pytest.approx(0.5)

    def test_non_positive_ceiling_rejected_in_bulk_path(self):
        with pytest.raises(ValueError):
            score_edit({"a": 0.0}, {"a": 0.5}, ceilings={"a": 0.0})

    def test_report_lists_moved_tasks(self):
        score = score_edit(
            before={"a": 0.0, "b": 0.5, "c": 0.25},
            after={"a": 1.0, "b": 0.5, "c": 0.5},
        )
        assert score.moved == ("a", "c")
        payload = score.to_dict()
        assert payload["moved"] == ["a", "c"]
        # All three have headroom, so all three are scored — the unmoved one
        # contributes a 0.0 fraction, which is the honest reading of "this
        # edit did nothing for it" and correctly pulls the mean down.
        assert payload["tasks_scored"] == 3
        assert len(payload["tasks"]) == 3
        assert score.headroom_score == pytest.approx((1.0 + 0.0 + 1 / 3) / 3)


class TestStageResultEmission:
    def test_wraps_both_metrics_in_stage_result(self):
        score = score_edit({"hard": 0.0}, {"hard": 0.5})
        result = to_stage_result(score, evidence_pointers=["run/before.json"])

        assert result.stage == "headroom_score"
        assert result.evidence_pointers == ["run/before.json"]
        assert result.result["headroom_score"] == pytest.approx(0.5)
        assert result.result["raw_delta"] == pytest.approx(0.5)

    def test_round_trips_through_dict(self):
        result = to_stage_result(score_edit({"a": 0.0}, {"a": 0.25}))
        restored = type(result).from_dict(result.to_dict())
        assert restored.result["headroom_score"] == pytest.approx(0.25)


class TestTaskScoreIsFrozen:
    def test_task_score_is_immutable(self):
        task = TaskScore(
            task="a",
            before=0.0,
            after=0.5,
            ceiling=1.0,
            raw_delta=0.5,
            gap_closed=0.5,
            regime=SCORED,
        )
        with pytest.raises(Exception):
            task.task = "b"  # type: ignore[misc]

    def test_edit_score_defaults(self):
        empty = EditScore(raw_delta=0.0, headroom_score=0.0)
        assert empty.tasks == ()
        assert empty.scored_tasks == ()
