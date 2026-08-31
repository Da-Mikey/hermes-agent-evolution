"""Tests for shuffle-order selection stability (#3337)."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.evolution_selection_stability import (
    jaccard,
    make_command_select_fn,
    selection_stability,
)


class SelectionStabilityTests(unittest.TestCase):
    def test_jaccard_basics(self) -> None:
        self.assertEqual(jaccard(set(), set()), 1.0)
        self.assertEqual(jaccard({"a"}, {"a"}), 1.0)
        self.assertEqual(jaccard({"a"}, {"b"}), 0.0)
        self.assertAlmostEqual(jaccard({"a", "b"}, {"b", "c"}), 1 / 3)

    def test_fewer_than_two_runs_rejected(self) -> None:
        with self.assertRaises(ValueError):
            selection_stability([1, 2, 3], lambda ids: ids, runs=1)

    def test_deterministic_selector_is_stable(self) -> None:
        """A pure top-3 selector must be order-independent (by construction here)."""
        backlog = list(range(10))

        def select(ids):
            return sorted(ids)[:3]

        report = selection_stability(backlog, select, runs=5, seed=7)
        self.assertTrue(report.stable)
        self.assertEqual(report.stability_score, 1.0)
        self.assertEqual(report.flipped_ids, [])
        # Orders genuinely differ across runs (the shuffle is real).
        self.assertEqual(len({tuple(o) for o in report.orders}), 5)

    def test_order_sensitive_selector_is_flagged_unstable(self) -> None:
        """A selector that just takes the first k items IS an ordering artifact."""
        backlog = list(range(20))
        report = selection_stability(backlog, lambda ids: ids[:3], runs=5, seed=0)
        self.assertFalse(report.stable)
        self.assertLess(report.stability_score, report.threshold)
        self.assertTrue(report.flipped_ids)

    def test_partial_flip_measured(self) -> None:
        """Selector that keeps 'top' but drops 'borderline' on some orders."""
        backlog = ["top1", "top2", "borderline", "filler1", "filler2"]

        def select(ids):
            picked = [i for i in ids if i.startswith("top")]
            # picks up 'borderline' only when it appears early (order artifact)
            if ids.index("borderline") < 2:
                picked.append("borderline")
            return picked

        report = selection_stability(backlog, select, runs=6, seed=3)
        self.assertIn("borderline", report.flipped_ids)
        self.assertNotIn("top1", report.flipped_ids)

    def test_dict_backlog_items_extract_id(self) -> None:
        backlog = [{"number": n, "title": f"issue-{n}"} for n in range(8)]
        report = selection_stability(backlog, lambda ids: ids[:4], runs=4, seed=1)
        self.assertFalse(report.stable)
        for sel in report.selections:
            for item in sel:
                self.assertIsInstance(item, int)

    def test_min_pairwise_tracks_worst_pair(self) -> None:
        backlog = list(range(6))
        report = selection_stability(backlog, lambda ids: ids[:2], runs=4, seed=2)
        self.assertLessEqual(report.min_pairwise, report.stability_score)


class CommandSelectorTests(unittest.TestCase):
    def test_command_selector_roundtrip(self) -> None:
        with TemporaryDirectory() as tmp:
            backlog_path = Path(tmp) / "backlog.json"
            backlog_path.write_text(json.dumps(list(range(6))))
            out_path = Path(tmp) / "report.json"
            rc = __import__(
                "scripts.evolution_selection_stability", fromlist=["main"]
            ).main([
                "--backlog",
                str(backlog_path),
                "--select-cmd",
                'python3 -c "import sys,json; d=json.load(sys.stdin); '
                'print(json.dumps(sorted(d)[:3]))"',
                "--runs",
                "3",
                "--output",
                str(out_path),
            ])
            self.assertEqual(rc, 0)
            report = json.loads(out_path.read_text())
            self.assertTrue(report["stable"])
            self.assertEqual(report["n_orders"], 3)

    def test_failing_command_raises(self) -> None:
        fn = make_command_select_fn("exit 3")
        with self.assertRaises(RuntimeError):
            fn([1, 2, 3])


if __name__ == "__main__":
    unittest.main()
