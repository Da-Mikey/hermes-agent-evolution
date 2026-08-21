# -*- coding: utf-8 -*-
"""Tests for :mod:`evolution.lib.mutation_guard` (issue #3014, counter slice)."""

from __future__ import annotations

import json

import pytest

from evolution.lib.mutation_guard import FailureCauseSummary, MutatingFailureCounter


class TestMutatingFailureCounter:
    def test_record_and_summary(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        counter = MutatingFailureCounter(ledger)
        counter.record("run-1", "mutating")
        counter.record("run-2", "other")
        counter.record("run-3", "mutating")
        s = counter.summary()
        assert s.failed_runs == 3
        assert s.mutating_cause == 2
        assert s.other_cause == 1
        assert s.mutating_share == round(2 / 3, 3)

    def test_no_records_empty_summary(self, tmp_path):
        counter = MutatingFailureCounter(tmp_path / "nope.jsonl")
        s = counter.summary()
        assert s.failed_runs == 0
        assert s.mutating_cause == 0
        assert s.mutating_share is None

    def test_invalid_cause_rejected(self, tmp_path):
        counter = MutatingFailureCounter(tmp_path / "ledger.jsonl")
        with pytest.raises(ValueError):
            counter.record("run-x", "maybe")

    def test_ledger_lines_are_json(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        counter = MutatingFailureCounter(ledger)
        counter.record("run-1", "mutating", recorded_at="2026-08-21")
        lines = ledger.read_text(encoding="utf-8").strip().splitlines()
        rec = json.loads(lines[0])
        assert rec["run_id"] == "run-1"
        assert rec["cause_category"] == "mutating"
        assert rec["outcome"] == "failed"
        assert rec["recorded_at"] == "2026-08-21"

    def test_malformed_lines_skipped(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text("not-json\n", encoding="utf-8")
        counter = MutatingFailureCounter(ledger)
        counter.record("run-1", "other")
        s = counter.summary()
        assert s.failed_runs == 1
        assert s.mutating_cause == 0


class TestFailureCauseSummary:
    def test_defaults(self):
        s = FailureCauseSummary()
        assert s.failed_runs == 0
        assert s.mutating_share is None

    def test_to_dict_plain_types(self):
        s = FailureCauseSummary(
            failed_runs=4, mutating_cause=1, other_cause=3, mutating_share=0.25
        )
        d = s.to_dict()
        assert d["failed_runs"] == 4
        assert d["mutating_share"] == 0.25
        json.dumps(d)  # must not raise
