"""Tests for scripts/calibrate_reasoning_effort.py (issue #78).

Cover the pure comparison/aggregation core only — no model/API calls. Cost
normalisation is exercised through the "unknown model → $0" path so the tests
are hermetic and do not depend on the live pricing table.
"""

import pytest

from scripts.calibrate_reasoning_effort import (
    CalibrationRecord,
    aggregate_by_effort,
    compare_efforts,
    normalize_cost,
    parse_records,
    render_report,
)

FAKE_MODEL = "unknown/fake-model-for-tests"


def _rec(task_id, effort, *, reasoning=0, output=0, latency=0.0, score=None, success=True):
    return CalibrationRecord(
        task_id=task_id,
        effort=effort,
        input_tokens=100,
        output_tokens=output,
        reasoning_tokens=reasoning,
        latency_seconds=latency,
        success=success,
        score=score,
    )


def test_parse_records_reads_valid_jsonl():
    lines = [
        '{"task_id": "a", "effort": "low", "reasoning_tokens": 5}\n',
        '{"task_id": "b", "effort": "high", "reasoning_tokens": 50, "score": 0.9}\n',
        "\n",  # blank line skipped
    ]
    records = parse_records(lines)
    assert len(records) == 2
    assert records[0].task_id == "a"
    assert records[0].reasoning_tokens == 5
    assert records[1].score == 0.9


def test_parse_records_missing_required_field_raises():
    with pytest.raises(ValueError):
        parse_records(['{"task_id": "a"}\n'])


def test_parse_records_malformed_json_raises():
    with pytest.raises(ValueError):
        parse_records(["not json\n"])


def test_normalize_cost_unknown_model_returns_zero():
    rec = _rec("a", "low", reasoning=10, output=20)
    assert normalize_cost(rec, FAKE_MODEL) == 0


def test_aggregate_by_effort_sums_tokens_and_success():
    records = [
        _rec("a", "low", reasoning=5, output=10, success=True),
        _rec("b", "low", reasoning=15, output=30, success=False),
        _rec("c", "high", reasoning=100, output=40, success=True),
    ]
    agg = aggregate_by_effort(records, FAKE_MODEL)
    assert set(agg) == {"low", "high"}

    low = agg["low"]
    assert low.n_runs == 2
    assert low.n_success == 1
    assert low.success_rate == 0.5
    assert low.total_reasoning_tokens == 20
    assert low.total_output_tokens == 40

    high = agg["high"]
    assert high.n_runs == 1
    assert high.total_reasoning_tokens == 100


def test_aggregate_by_effort_computes_mean_score():
    records = [
        _rec("a", "low", score=0.5),
        _rec("b", "low", score=0.9),
        _rec("c", "high", score=0.7),
    ]
    agg = aggregate_by_effort(records, FAKE_MODEL)
    assert agg["low"].mean_score == pytest.approx(0.7)
    assert agg["high"].mean_score == pytest.approx(0.7)


def test_compare_efforts_deltas_against_baseline():
    records = [
        _rec("a", "low", reasoning=10, latency=1.0, score=0.8),
        _rec("b", "high", reasoning=50, latency=3.0, score=0.8),
    ]
    agg = aggregate_by_effort(records, FAKE_MODEL)
    deltas = compare_efforts(agg, "low")
    assert len(deltas) == 1
    d = deltas[0]
    assert d.effort == "high"
    assert d.baseline == "low"
    assert d.reasoning_token_delta == 40
    assert d.latency_delta_seconds == pytest.approx(2.0)
    assert d.score_delta == pytest.approx(0.0)


def test_compare_efforts_missing_baseline_raises():
    agg = aggregate_by_effort([_rec("a", "low")], FAKE_MODEL)
    with pytest.raises(ValueError):
        compare_efforts(agg, "nonexistent")


def test_render_report_includes_effort_rows_and_deltas():
    records = [
        _rec("a", "low", reasoning=10, latency=1.0, score=0.8),
        _rec("b", "high", reasoning=50, latency=3.0, score=0.8),
    ]
    agg = aggregate_by_effort(records, FAKE_MODEL)
    deltas = compare_efforts(agg, "low")
    report = render_report(agg, deltas, model=FAKE_MODEL)
    assert "low" in report
    assert "high" in report
    assert "Deltas" in report
    assert FAKE_MODEL in report
