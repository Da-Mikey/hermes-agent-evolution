"""TrACE-style adaptive compute — disagreement metric + budget rule (#86, S1).

Pure metric and rule from scripts/evolution_disagreement.py, exercised with
synthetic per-step candidate-action sets. The JSONL IO boundary (iter_steps)
is tested with a temp file; the CLI's exit paths are tested via main(argv).
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_disagreement import (  # noqa: E402
    _action_key,
    budget_rule,
    disagreement_index,
    iter_steps,
    main,
)


class TestActionKey:
    def test_string_passes_through(self):
        assert _action_key("read_file") == "read_file"

    def test_dict_prefers_tool_then_name(self):
        assert _action_key({"tool": "read_file", "path": "a"}) == "read_file"
        assert _action_key({"name": "search_files"}) == "search_files"
        assert _action_key({"action": "x"}) == "x"

    def test_dict_without_keys_falls_back_to_str(self):
        assert _action_key({"a": 1}) == "{'a': 1}"

    def test_other_types_str_ed(self):
        assert _action_key(42) == "42"


class TestDisagreementIndex:
    def test_unanimous_is_zero(self):
        assert disagreement_index(["read_file", "read_file", "read_file"]) == 0.0

    def test_single_action_is_zero(self):
        assert disagreement_index(["read_file"]) == 0.0

    def test_even_split(self):
        assert disagreement_index(["a", "b"]) == 0.5

    def test_two_thirds_majority(self):
        assert disagreement_index(["a", "a", "b"]) == 1.0 - 2.0 / 3.0

    def test_dict_actions_labeled_by_tool(self):
        actions = [
            {"tool": "search_files", "pattern": "x"},
            {"tool": "search_files", "pattern": "y"},
            {"tool": "read_file", "path": "z"},
        ]
        assert disagreement_index(actions) == 1.0 - 2.0 / 3.0

    def test_empty_raises(self):
        try:
            disagreement_index([])
        except ValueError:
            return
        raise AssertionError("expected ValueError for empty actions")


class TestBudgetRule:
    def test_agreement_gets_base_budget(self):
        assert budget_rule(0.0, 4.0) == 4.0

    def test_full_disagreement_gets_capped_budget(self):
        assert budget_rule(1.0, 4.0, max_multiplier=3.0) == 12.0

    def test_linear_ramp_midpoint(self):
        # d=0.5 -> multiplier 2.0 -> budget 8.0
        assert budget_rule(0.5, 4.0, min_multiplier=1.0, max_multiplier=3.0) == 8.0

    def test_flat_when_no_ramp(self):
        assert budget_rule(1.0, 4.0, min_multiplier=2.0, max_multiplier=2.0) == 8.0

    def test_clamps_out_of_range(self):
        assert budget_rule(-1.0, 4.0) == 4.0
        assert budget_rule(2.0, 4.0, max_multiplier=3.0) == 12.0

    def test_custom_multipliers(self):
        assert budget_rule(0.0, 4.0, min_multiplier=2.0, max_multiplier=5.0) == 8.0
        assert budget_rule(1.0, 4.0, min_multiplier=2.0, max_multiplier=5.0) == 20.0


class TestIterSteps:
    def test_reads_records_with_step(self, tmp_path):
        p = tmp_path / "steps.jsonl"
        p.write_text(
            '{"step": 3, "actions": ["a", "a", "b"]}\n{"actions": ["a", "a"]}\n',
            encoding="utf-8",
        )
        out = list(iter_steps(str(p)))
        assert out == [
            {"step": 3, "actions": ["a", "a", "b"]},
            {"step": 2, "actions": ["a", "a"]},  # line number fallback
        ]

    def test_skips_blank_lines(self, tmp_path):
        p = tmp_path / "steps.jsonl"
        p.write_text('\n{"actions": ["a"]}\n\n', encoding="utf-8")
        assert list(iter_steps(str(p))) == [{"step": 2, "actions": ["a"]}]

    def test_malformed_json_raises_with_line(self, tmp_path):
        p = tmp_path / "steps.jsonl"
        p.write_text('{"actions": ["a"]}\nnot json\n', encoding="utf-8")
        try:
            list(iter_steps(str(p)))
        except ValueError as exc:
            assert "line 2" in str(exc)
            return
        raise AssertionError("expected ValueError on malformed line")


class TestCli:
    def test_runs_and_reports_budgets(self, tmp_path, capsys):
        p = tmp_path / "steps.jsonl"
        p.write_text(
            '{"step": 1, "actions": ["a", "a"]}\n{"step": 2, "actions": ["a", "b"]}\n',
            encoding="utf-8",
        )
        assert main(["prog", str(p), "--base-budget", "4"]) == 0
        captured = capsys.readouterr().out
        assert "step=1 disagreement=0.000 budget=4.000" in captured
        assert "step=2 disagreement=0.500 budget=8.000" in captured

    def test_json_output_shape(self, tmp_path, capsys):
        p = tmp_path / "steps.jsonl"
        p.write_text('{"actions": ["a", "b"]}\n', encoding="utf-8")
        assert main(["prog", str(p), "--json"]) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["steps"][0]["index"] == 0.5
        assert data["summary"]["count"] == 1
        assert data["summary"]["total_budget"] == 8.0  # 4.0 * 2.0 at d=0.5

    def test_malformed_input_exits_2(self, tmp_path, capsys):
        p = tmp_path / "steps.jsonl"
        p.write_text("garbage\n", encoding="utf-8")
        assert main(["prog", str(p)]) == 2

    def test_missing_arg_exits_2(self, capsys):
        assert main(["prog"]) == 2

    def test_empty_input_exits_2(self, tmp_path, capsys):
        p = tmp_path / "steps.jsonl"
        p.write_text("", encoding="utf-8")
        assert main(["prog", str(p)]) == 2
