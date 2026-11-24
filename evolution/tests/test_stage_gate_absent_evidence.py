# -*- coding: utf-8 -*-
"""Acceptance tests for #281 — absent evidence must not read as a pass.

Two gates returned a *pass* when the evidence they needed was absent rather than
a third outcome: ``StageResult``'s ACCEPT branch was decided on confidence alone,
and ``verify_implementation_claim`` returned True by construction when nothing
concrete was cited.  Both read "I could not find contrary evidence" as "the claim
holds".

These tests pin the boundary behaviour of the stage gate; the claim verifier's
three-valued contract is covered in
``tests/scripts/test_evolution_analysis_audit.py``.
"""

from __future__ import annotations

import json

from evolution.lib.stage_gate import (
    ACCEPT,
    CANNOT_DETERMINE,
    REFINE,
    GateDecision,
    compute_gate_rates,
    decide,
    load_decisions,
    record_decision,
)
from evolution.lib.stage_result import StageResult


def _result(
    confidence: int,
    evidence: list[str] | None = None,
    required_facts: list[str] | None = None,
) -> StageResult:
    return StageResult(
        result={"payload": True},
        evidence_pointers=list(evidence or []),
        confidence=confidence,
        stage="local_triage",
        required_facts=list(required_facts or []),
    )


class TestAcceptRequiresEvidence:
    def test_high_confidence_zero_pointers_is_cannot_determine(self):
        """Acceptance criterion 1: at the decide() boundary."""
        d = decide(_result(100))
        assert d.branch == CANNOT_DETERMINE
        assert d.proceeds is False
        assert d.missing_facts == []
        assert "no evidence pointer" in d.reason

    def test_one_pointer_restores_accept(self):
        assert decide(_result(100, ["a.json"])).branch == ACCEPT

    def test_cannot_determine_is_not_restart(self):
        """An undetermined gate has not disproved anything — do not restart."""
        d = decide(_result(100))
        assert d.branch != "restart"
        assert d.retained_evidence == []


class TestRequiredFacts:
    def test_absent_required_fact_is_cannot_determine_at_confidence_100(self):
        """Acceptance criterion 2: confidence cannot override a declared fact."""
        d = decide(_result(100, ["run.log"], required_facts=["gateway restart"]))
        assert d.branch == CANNOT_DETERMINE
        assert d.missing_facts == ["gateway restart"]

    def test_covered_required_fact_accepts(self):
        d = decide(
            _result(90, ["logs/gateway restart.log"], required_facts=["gateway restart"])
        )
        assert d.branch == ACCEPT
        assert d.missing_facts == []

    def test_required_fact_match_is_case_insensitive(self):
        d = decide(_result(90, ["FULL SUITE.log"], required_facts=["full suite"]))
        assert d.branch == ACCEPT

    def test_missing_fact_survives_low_confidence(self):
        """The fact check precedes the threshold branch, so REFINE cannot hide it."""
        d = decide(_result(10, ["a.json"], required_facts=["pytest"]))
        assert d.branch == CANNOT_DETERMINE
        assert d.missing_facts == ["pytest"]


class TestLedgerDiagnosability:
    def test_missing_facts_are_recorded(self, tmp_path):
        """Acceptance criterion 5: a cannot_determine is diagnosable from the record."""
        ledger = tmp_path / "gate.jsonl"
        record_decision(ledger, decide(_result(100, [], required_facts=["ha_events"])))
        recs = load_decisions(ledger)
        assert len(recs) == 1
        assert recs[0]["branch"] == CANNOT_DETERMINE
        assert recs[0]["missing_facts"] == ["ha_events"]

    def test_historical_records_still_load(self, tmp_path):
        """Acceptance criterion 4: pre-#281 lines (no missing_facts) stay parseable."""
        ledger = tmp_path / "gate.jsonl"
        ledger.write_text(
            json.dumps(
                {
                    "branch": ACCEPT,
                    "stage": "analysis",
                    "confidence": 80,
                    "threshold": 70,
                    "reason": "legacy",
                    "retained_evidence": ["a.json"],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        recs = load_decisions(ledger)
        assert [r["branch"] for r in recs] == [ACCEPT]
        assert "missing_facts" not in recs[0]

    def test_rates_count_cannot_determine_separately(self):
        recs = [
            {"branch": ACCEPT, "stage": "s"},
            {"branch": CANNOT_DETERMINE, "stage": "s"},
            {"branch": REFINE, "stage": "s"},
            {"branch": CANNOT_DETERMINE, "stage": "s"},
        ]
        b = compute_gate_rates(recs)["s"]
        assert b["total"] == 4
        assert b[CANNOT_DETERMINE] == 2
        assert b["stage_cannot_determine_rate"] == 0.5
        assert b["stage_refine_rate"] == 0.25


class TestRefineUnchanged:
    def test_refine_behaviour_unchanged(self):
        """Acceptance criterion 4: confidence 50 + pointers still refines."""
        d = decide(_result(50, ["a.json", "b.json", "c.json"]))
        assert d.branch == REFINE
        assert d.retained_evidence == ["a.json", "b.json", "c.json"]


class TestStageResultSerialisation:
    def test_required_facts_round_trip(self):
        sr = _result(80, ["a.json"], required_facts=["fact one"])
        again = StageResult.from_dict(sr.to_dict())
        assert again.required_facts == ["fact one"]

    def test_legacy_payload_defaults_to_empty(self):
        legacy = {"result": None, "evidence_pointers": [], "confidence": 0}
        assert StageResult.from_dict(legacy).required_facts == []

    def test_gate_decision_missing_facts_default(self):
        d = GateDecision(
            branch=ACCEPT,
            stage="s",
            confidence=90,
            threshold=70,
            reason="r",
            retained_evidence=["a"],
        )
        assert d.missing_facts == []
        assert d.to_dict()["missing_facts"] == []
