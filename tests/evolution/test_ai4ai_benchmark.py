"""Tests for AI4AI-Bench recursive self-improvement benchmark (issue #3064)."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import pytest

from evolution.lib.ai4ai_benchmark import (
    BUILTIN_TASKS,
    AI4AITask,
    BenchmarkRunResult,
    TaskResult,
    compute_trend,
    evaluate_task,
    load_benchmark_history,
    record_benchmark_run,
    run_benchmark_suite,
)


def test_builtin_tasks_structure():
    assert len(BUILTIN_TASKS) >= 4
    stages = {t.stage for t in BUILTIN_TASKS}
    assert "research" in stages
    assert "proposal" in stages
    assert "patch" in stages

    for task in BUILTIN_TASKS:
        assert task.task_id
        assert task.title
        assert task.target_component
        assert task.weight > 0


def test_evaluate_research_task():
    task = next(t for t in BUILTIN_TASKS if t.stage == "research")
    
    # Correct solver output
    res_good = evaluate_task(task, {"identified_bottleneck": task.eval_criteria["target_field"]})
    assert res_good.passed is True
    assert res_good.score == 1.0

    # Wrong solver output
    res_bad = evaluate_task(task, {"identified_bottleneck": "random_tool"})
    assert res_bad.passed is False
    assert res_bad.score < 0.5


def test_evaluate_proposal_task():
    task = next(t for t in BUILTIN_TASKS if t.stage == "proposal")
    
    # All required properties present
    req = task.eval_criteria.get("required_properties", [])
    res_good = evaluate_task(task, {"proposed_properties": req + ["extra_prop"]})
    assert res_good.passed is True
    assert res_good.score == 1.0

    # Partial properties present
    res_partial = evaluate_task(task, {"proposed_properties": req[:1]})
    assert res_partial.score == round(1.0 / len(req), 4)


def test_evaluate_patch_task():
    task = next(t for t in BUILTIN_TASKS if t.task_id == "ai4ai_patch_01_bounded_dedup_hash")
    
    # Correctness + speedup
    res_fast = evaluate_task(task, {
        "correctness": True,
        "speedup_factor": 2.5,
        "invariants_preserved": True,
    })
    assert res_fast.passed is True
    assert res_fast.score > 0.7

    # Invariant violation must fail even with correctness
    res_violation = evaluate_task(task, {
        "correctness": True,
        "speedup_factor": 3.0,
        "invariants_preserved": False,
    })
    assert res_violation.passed is False
    assert res_violation.score == 0.0
    assert res_violation.invariants_passed is False


def test_run_benchmark_suite_default():
    result = run_benchmark_suite()
    assert result.run_id.startswith("ai4ai-")
    assert result.total_tasks == len(BUILTIN_TASKS)
    assert result.passed_tasks > 0
    assert 0.0 <= result.pass_rate <= 1.0
    assert 0.0 <= result.composite_rsi_score <= 1.0
    assert "research" in result.stage_scores
    assert "proposal" in result.stage_scores
    assert "patch" in result.stage_scores


def test_metrics_record_and_history(tmp_path):
    metrics_file = tmp_path / "metrics.jsonl"
    result = run_benchmark_suite(commit_sha="testsha123")

    saved_path = record_benchmark_run(result, metrics_path=metrics_file)
    assert saved_path == metrics_file
    assert metrics_file.exists()

    history = load_benchmark_history(days=7, metrics_path=metrics_file)
    assert len(history) == 1
    assert history[0].commit_sha == "testsha123"
    assert history[0].composite_rsi_score == result.composite_rsi_score


def test_compute_trend_improving_and_regression():
    r1 = BenchmarkRunResult(
        run_id="r1",
        timestamp=int(time.time()) - 3600,
        commit_sha="sha1",
        total_tasks=4,
        passed_tasks=3,
        pass_rate=0.75,
        stage_scores={"research": 0.70, "proposal": 0.70, "patch": 0.70},
        composite_rsi_score=0.70,
        task_results=[],
    )
    r2 = BenchmarkRunResult(
        run_id="r2",
        timestamp=int(time.time()),
        commit_sha="sha2",
        total_tasks=4,
        passed_tasks=4,
        pass_rate=1.0,
        stage_scores={"research": 0.90, "proposal": 0.90, "patch": 0.90},
        composite_rsi_score=0.90,
        task_results=[],
    )

    # Trend improving
    trend_up = compute_trend([r1, r2])
    assert trend_up["runs_count"] == 2
    assert trend_up["rsi_trend"] == "improving"
    assert trend_up["delta"] == 0.20
    assert trend_up["regression_detected"] is False

    # Trend degrading (regression)
    r3 = BenchmarkRunResult(
        run_id="r3",
        timestamp=int(time.time()),
        commit_sha="sha3",
        total_tasks=4,
        passed_tasks=2,
        pass_rate=0.50,
        stage_scores={"research": 0.50, "proposal": 0.50, "patch": 0.50},
        composite_rsi_score=0.50,
        task_results=[],
    )
    trend_down = compute_trend([r1, r3])
    assert trend_down["rsi_trend"] == "degrading"
    assert trend_down["regression_detected"] is True


def test_cli_subcommand_execution(tmp_path):
    metrics_file = tmp_path / "metrics.jsonl"
    
    # Test runner script
    res = subprocess.run(
        [
            sys.executable,
            "scripts/evolution_benchmark.py",
            "run",
            "--metrics-path",
            str(metrics_file),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(res.stdout)
    assert "composite_rsi_score" in data
    assert data["total_tasks"] == len(BUILTIN_TASKS)

    # Test trend script
    res_trend = subprocess.run(
        [
            sys.executable,
            "scripts/evolution_benchmark.py",
            "trend",
            "--metrics-path",
            str(metrics_file),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data_trend = json.loads(res_trend.stdout)
    assert data_trend["runs_count"] == 1
