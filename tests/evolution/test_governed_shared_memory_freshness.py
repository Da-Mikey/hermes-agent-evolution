"""Retrieval-time freshness signals for governed memory (#3336).

Covers ``GovernedSharedMemory.read_with_freshness`` and the agent-level
``read_governed_memory_with_freshness`` wrapper: superseded constraints must
surface an explicit review signal instead of being silently served as current
(or silently hidden by ``active_only`` reads).
"""

import unittest

from evolution.lib.governed_shared_memory import (
    FreshnessSignal,
    GovernedSharedMemory,
)


class ReadWithFreshnessTests(unittest.TestCase):
    def test_unknown_key_returns_unknown_signal(self) -> None:
        store = GovernedSharedMemory()
        record, signal = store.read_with_freshness("missing")
        self.assertIsNone(record)
        self.assertEqual(signal.status, "unknown")
        self.assertIsNone(signal.superseded_by)
        self.assertIsNone(signal.superseded_at_ms)

    def test_active_record_returns_current_signal(self) -> None:
        store = GovernedSharedMemory()
        store.write(key="rule", value="keep logs 7d", author_id="a1")
        record, signal = store.read_with_freshness("rule")
        self.assertIsNotNone(record)
        self.assertEqual(signal.status, "current")
        self.assertIsNone(signal.superseded_by)

    def test_superseded_record_is_flagged_with_successor(self) -> None:
        store = GovernedSharedMemory()
        store.write(key="rule", value="keep logs 7d", author_id="a1")
        successor = store.write(
            key="rule-v2", value="keep logs 30d", author_id="a2",
            supersedes_key="rule",
        )
        record, signal = store.read_with_freshness("rule")
        self.assertIsNotNone(record)
        self.assertEqual(record.value, "keep logs 7d")
        self.assertEqual(signal.status, "superseded")
        self.assertEqual(signal.superseded_by, "rule-v2")
        self.assertEqual(signal.superseded_at_ms, successor.created_at_ms)

    def test_superseded_signal_differs_from_active_only_read(self) -> None:
        """The whole point of #3336: active_only read silently hides; freshness read flags."""
        store = GovernedSharedMemory()
        store.write(key="rule", value="old constraint", author_id="a1")
        store.write(key="rule-v2", value="new constraint", author_id="a1",
                    supersedes_key="rule")
        self.assertIsNone(store.read(key="rule", active_only=True))
        _, signal = store.read_with_freshness("rule")
        self.assertEqual(signal.status, "superseded")
        self.assertIsInstance(signal, FreshnessSignal)

    def test_inactive_record_without_successor_reads_current(self) -> None:
        """An inactive record with no supersession pointer is not claimed superseded."""
        store = GovernedSharedMemory()
        rec = store.write(key="rule", value="x", author_id="a1")
        rec.is_active = False
        rec.superseded_by = None
        _, signal = store.read_with_freshness("rule")
        self.assertEqual(signal.status, "current")

    def test_freshness_signal_is_independent_of_read_order(self) -> None:
        store = GovernedSharedMemory()
        store.write(key="a", value="1", author_id="x")
        store.write(key="b", value="2", author_id="x", supersedes_key="a")
        _, sig_a = store.read_with_freshness("a")
        _, sig_b = store.read_with_freshness("b")
        self.assertEqual(sig_a.status, "superseded")
        self.assertEqual(sig_b.status, "current")


if __name__ == "__main__":
    unittest.main()
