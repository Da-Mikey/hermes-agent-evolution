# -*- coding: utf-8 -*-
"""Consumer-side tests for the headroom-normalised score (issue #208).

The rework brief for #208 was explicit: a scorer that only its own tests call
does not satisfy the issue. These tests exercise the *consumer* — the flip gate
that decides promote/block, and the merge gate that calls it — and assert that
the headroom column is emitted there, alongside the raw delta, from live
candidate data.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = REPO_ROOT / "scripts"
for _p in (str(REPO_ROOT), str(SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from evolution_flip_gate import (  # noqa: E402
    BLOCK,
    PROMOTE,
    compare_runs,
    flip_verdict,
    format_table,
    main,
)


def _mixed_runs():
    """One fix, one still-broken, one held, one regression (the #208 shape)."""
    before = {"p_fix": False, "p_broken": False, "p_held": True, "p_reg": True}
    after = {"p_fix": True, "p_broken": False, "p_held": True, "p_reg": False}
    return before, after


class TestMetricsOnTheFlipTable:
    def test_table_dict_carries_both_metrics(self):
        table = compare_runs(*_mixed_runs())

        metrics = table.to_dict()["metrics"]

        assert metrics is not None
        # Primary column: mean fraction of the remaining gap closed. The fix
        # closes 100% of its gap, the still-broken probe closes none.
        assert metrics["headroom_score"] == pytest.approx(0.5)
        # Secondary column: raw delta. It reads 0.0 here even though half the
        # failing surface was closed — exactly the blind spot #208 names.
        assert metrics["raw_delta"] == pytest.approx(0.0)
        assert metrics["tasks_scored"] == 2
        assert metrics["moved"] == ["p_fix", "p_reg"]

    def test_regression_is_recorded_not_averaged_away(self):
        table = compare_runs(*_mixed_runs())

        metrics = table.to_dict()["metrics"]

        # p_reg passed before and fails after: at ceiling, so no fraction can be
        # formed, but it must still be recorded as a regression.
        assert metrics["regressed"] == ["p_reg"]
        assert metrics["has_regression"] is True
        assert "p_reg" in metrics["at_ceiling"]

    def test_missing_and_inconclusive_probes_are_excluded(self):
        before = {"p1": True, "gone": True, "refused": True}
        after = {"p1": True, "refused": {"inconclusive": True}, "new": True}
        table = compare_runs(before, after)

        rates_before, rates_after = table.pass_rates()

        assert "gone" not in rates_before
        assert "new" not in rates_after
        assert "refused" not in rates_before
        assert "refused" not in rates_after
        assert set(rates_before) == {"p1"}

    def test_metrics_do_not_change_the_verdict(self):
        before, after = _mixed_runs()
        table = compare_runs(before, after)

        # A P->F is still a block regardless of the headroom column: the issue
        # requires a backtest of archived candidates before this default flips.
        assert flip_verdict(table).verdict == BLOCK
        assert flip_verdict(table, max_regressions=1).verdict == PROMOTE


class TestConsumerEmitsHeadroom:
    def test_human_summary_carries_headroom_line(self):
        table = compare_runs(*_mixed_runs())

        summary = format_table(table, flip_verdict(table))

        assert "headroom score +0.500" in summary
        assert "raw delta +0.000" in summary

    def test_merge_gate_block_message_carries_headroom(self):
        """The merge gate is the acceptance path — it must invoke the scorer."""
        import evolution_merge_gate

        before, after = _mixed_runs()

        violations = evolution_merge_gate.check_flip_gate(before, after)

        assert violations, "a P->F must still block the merge"
        assert violations[0].startswith("FLIP_GATE_BLOCK")
        assert "headroom score" in violations[0]

    def test_cli_json_carries_metrics(self, tmp_path, capsys):
        before, after = _mixed_runs()
        before_path = tmp_path / "before.json"
        after_path = tmp_path / "after.json"
        before_path.write_text(json.dumps(before), encoding="utf-8")
        after_path.write_text(json.dumps(after), encoding="utf-8")

        rc = main([str(before_path), str(after_path)])
        payload = json.loads(capsys.readouterr().out)

        assert rc == 1  # blocked — unchanged behaviour
        assert payload["metrics"]["headroom_score"] == pytest.approx(0.5)
        assert payload["metrics"]["raw_delta"] == pytest.approx(0.0)
