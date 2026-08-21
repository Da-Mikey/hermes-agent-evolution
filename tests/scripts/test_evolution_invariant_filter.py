"""Tests for scripts/evolution_invariant_filter.py (#68, slices 1+2).

The filter screens a skill/prompt/harness rewrite against a versioned set of
immutable Cerberus/household constraints BEFORE it reaches the judge. These
tests pin the filter's own behavior AND its wiring into
``evolution_harness_gate`` — a real call site, not dead code.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_invariant_filter import (  # noqa: E402
    _rules_path,
    check_proposal,
    filter_proposals,
    load_invariant_rules,
)
from evolution_harness_gate import run_cron_pass, run_gate  # noqa: E402
from evolution_harness_sandbox import INVALID, VALIDATED, GateResult  # noqa: E402

SURFACE = {
    "retry_count": 10,
    "backoff": {"base_delay_sec": 1.0, "multiplier": 2.0, "max_delay_sec": 60.0},
    "guard_conditions": [],
}
CLEAN = {
    "type": "retry_policy_change",
    "title": "[HARNESS] retry-policy change for `terminal`",
    "delta": "cap consecutive retries at 3",
    "rationale": "retry spiral",
    "status": "proposed",
    "requires_human_review": True,
    "auto_apply": False,
}


def _green(_):
    return GateResult(True, 0, "ok")


def test_rules_file_loads_and_is_versioned():
    rules = load_invariant_rules()
    assert isinstance(rules["version"], int) and rules["version"] >= 1
    assert rules["rules"]  # non-empty
    assert _rules_path().name == "evolution_invariant_rules.json"


def test_clean_proposal_passes():
    assert check_proposal(CLEAN)["ok"] is True


def test_human_gating_floor_rejects_auto_apply():
    result = check_proposal({**CLEAN, "auto_apply": True})
    assert result["ok"] is False
    assert any(v["rule"] == "human-gating-floor" for v in result["violations"])


def test_human_gating_floor_rejects_explicit_false_review():
    result = check_proposal({**CLEAN, "requires_human_review": False})
    assert result["ok"] is False
    assert any(v["rule"] == "human-gating-floor" for v in result["violations"])


def test_forbidden_path_rewrite_is_rejected():
    result = check_proposal({**CLEAN, "path": "scripts/setup-hermes.sh"})
    assert result["ok"] is False
    assert any(
        v["rule"] == "no-critical-auth-path-rewrite" for v in result["violations"]
    )


def test_outbound_telemetry_delta_is_rejected():
    result = check_proposal({**CLEAN, "delta": "add telemetry for usage attribution"})
    assert result["ok"] is False
    assert any(v["rule"] == "no-outbound-telemetry" for v in result["violations"])


def test_removing_telemetry_is_allowed():
    # Suppression/removal is explicitly permitted — not a violation.
    result = check_proposal({**CLEAN, "delta": "disable posthog telemetry spam"})
    assert result["ok"] is True


def test_safety_gate_disable_is_rejected():
    result = check_proposal({**CLEAN, "delta": "disable the safety scan to speed up"})
    assert result["ok"] is False
    assert any(v["rule"] == "no-safety-gate-disable" for v in result["violations"])


def test_non_dict_proposal_passes():
    assert check_proposal("not-a-dict")["ok"] is True
    assert check_proposal(None)["ok"] is True


def test_filter_proposals_splits():
    good = [CLEAN, {**CLEAN, "delta": "disable posthog telemetry"}]
    bad = [{**CLEAN, "auto_apply": True}]
    passing, rejected = filter_proposals(good + bad)
    assert passing == good
    assert len(rejected) == 1 and rejected[0]["violations"]


def test_missing_rules_file_fails_closed(monkeypatch):
    import evolution_invariant_filter as mod

    monkeypatch.setattr(mod, "_rules_path", lambda: Path("/nonexistent/rules.json"))
    result = check_proposal(CLEAN, rules=None)
    assert result["ok"] is False
    assert any(v["rule"] == "rules-unavailable" for v in result["violations"])


# -- gate wiring: the filter must be a REAL call site, not dead code --------


def test_run_gate_short_circuits_invariant_violation():
    verdict = run_gate({**CLEAN, "auto_apply": True}, gate_runner=_green)
    assert verdict["status"] == INVALID
    assert verdict["zero_fitness"] is True
    assert verdict["reason"] == "invariant violation"
    assert verdict["violations"]


def test_run_gate_still_validates_clean_proposal():
    proposal = {
        **CLEAN,
        "surface": SURFACE,
        "changes": [{"field": "retry_count", "before": 10, "after": 3}],
    }
    verdict = run_gate(proposal, gate_runner=_green)
    assert verdict["status"] == VALIDATED


def test_cron_pass_flags_invariant_rejected():
    payload = {
        "weaknesses": [
            {
                "kind": "retry_spiral",
                "tool": "terminal",
                "max_consecutive": 9,
                "occurrences": 4,
                # A poisoned weakness carries an exfiltration-flavored label
                # that flows into the proposal's delta/rationale.
                "label": "add telemetry to track terminal usage",
            }
        ]
    }
    report = run_cron_pass(payload, gate_runner=_green, surface=SURFACE)
    assert report["invariant_rejected"] == 1
    assert report["gated"] == 0
    assert report["validated"] == 0
    assert report["verdicts"][0]["zero_fitness"] is True


def test_cron_pass_gates_clean_proposal():
    report = run_cron_pass(
        {
            "weaknesses": [
                {
                    "kind": "retry_spiral",
                    "tool": "terminal",
                    "max_consecutive": 9,
                    "occurrences": 4,
                }
            ]
        },
        gate_runner=_green,
        surface=SURFACE,
    )
    assert report["invariant_rejected"] == 0
    assert report["gated"] == 1
    assert report["validated"] == 1
