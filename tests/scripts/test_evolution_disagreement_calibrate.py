"""TrACE-style adaptive compute — S4 calibration harness tests (#86).

The harness (scripts/evolution_disagreement_calibrate.py) is the REAL
CONSUMER of the S1 module: it invokes the S1 CLI as a subprocess and turns
the per-step budgets into the fixed-effort comparison the issue's success
criteria need. These tests exercise the harness end-to-end (temp JSONL →
report), which is what the integration gate asked for: a module nothing
calls does not merge.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_disagreement_calibrate import calibrate, main  # noqa: E402

MIXED = [
    {"step": 1, "actions": ["search_files", "search_files", "search_files"]},  # routine
    {"step": 2, "actions": ["read_file", "search_files"]},  # ambiguous (0.5)
    {"step": 3, "actions": ["patch", "patch"]},  # routine
]


def _write(tmp_path, records):
    p = tmp_path / "steps.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(p)


class TestCalibrate:
    def test_mixed_workload_saves_on_routine_steps(self, tmp_path):
        path = _write(tmp_path, MIXED)
        report = calibrate(path, base_budget=4.0, min_mult=1.0, max_mult=3.0)
        s = report["summary"]
        assert s["count"] == 3
        # routine steps (index 0) -> base budget 4; ambiguous step (0.5) ->
        # midpoint 8 (4 * (1 + 2*0.5)).
        assert abs(s["adaptive_total"] - (4.0 + 8.0 + 4.0)) < 1e-6
        # fixed effort = full budget every step: 4*3 per step = 36.
        assert abs(s["fixed_baseline"] - 36.0) < 1e-6
        assert s["savings"] > 0
        assert s["ambiguous_steps"] == 1
        assert s["at_min_budget"] == 2  # the two routine steps pay the floor
        assert "routine steps discounted" in s["verdict"]
        # the ambiguous step is preserved ABOVE the routine floor
        step2 = next(s_ for s_ in report["steps"] if s_["step"] == 2)
        assert step2["budget"] == 8.0

    def test_unanimous_workload_saves_max(self, tmp_path):
        path = _write(
            tmp_path,
            [{"actions": ["a", "a"]}, {"actions": ["b", "b"]}],
        )
        report = calibrate(path, base_budget=4.0)
        s = report["summary"]
        assert s["mean_index"] == 0.0
        assert abs(s["adaptive_total"] - 8.0) < 1e-6  # 2 steps x base 4
        assert abs(s["fixed_baseline"] - 24.0) < 1e-6  # 2 x 4 x 3
        assert s["savings"] > 0
        assert s["at_min_budget"] == 2
        assert "routine steps discounted" in s["verdict"]

    def test_even_split_gets_max_budget(self, tmp_path):
        path = _write(tmp_path, [{"actions": ["x", "y", "z"]}])
        report = calibrate(path, base_budget=2.0, max_mult=3.0)
        s = report["summary"]
        assert s["ambiguous_steps"] == 1
        # index = 1 - 1/3 = 0.6667 -> budget = 2 * (1 + 2*0.6667) = 4.6667
        assert abs(s["adaptive_total"] - (2.0 * (1.0 + 2.0 * (2.0 / 3.0)))) < 1e-6
        assert s["at_min_budget"] == 0  # nothing pays the floor

    def test_flat_rule_when_max_lte_min(self, tmp_path):
        path = _write(tmp_path, MIXED)
        report = calibrate(path, base_budget=4.0, min_mult=1.0, max_mult=1.0)
        assert abs(report["summary"]["adaptive_total"] - 12.0) < 1e-6


class TestCalibrateCli:
    def test_cli_end_to_end_json(self, tmp_path):
        path = _write(tmp_path, MIXED)
        rc = main(["evolve", path, "--json"])
        assert rc == 0

    def test_cli_bad_input_rc2(self, tmp_path):
        bad = tmp_path / "bad.jsonl"
        bad.write_text("not json\n", encoding="utf-8")
        rc = main(["evolve", str(bad)])
        assert rc == 2

    def test_cli_missing_args_rc2(self):
        assert main(["evolve"]) == 2
