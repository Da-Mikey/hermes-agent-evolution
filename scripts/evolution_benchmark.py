#!/usr/bin/env python3
"""CLI and pipeline script for the AI4AI-Bench recursive self-improvement benchmark (issue #3064).

Usage:
    # Run the full benchmark and record scores to evolution/metrics.jsonl
    evolution_benchmark.py run [--record] [--json]

    # Display 7-day trend analysis and regression warnings
    evolution_benchmark.py trend [--days 7] [--json]

    # List all held-out benchmark tasks
    evolution_benchmark.py tasks [--json]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any, Dict, Optional

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evolution.lib.ai4ai_benchmark import (
    BUILTIN_TASKS,
    BenchmarkRunResult,
    compute_trend,
    get_metrics_path,
    load_benchmark_history,
    record_benchmark_run,
    run_benchmark_suite,
)


def _get_current_commit_sha() -> Optional[str]:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(ROOT),
        )
        return res.stdout.strip()
    except Exception:
        return None


def cmd_run(args) -> int:
    commit = _get_current_commit_sha()
    metrics_path = Path(args.metrics_path) if getattr(args, "metrics_path", None) else None
    
    stage_filter = getattr(args, "stage", None)
    tasks = [t for t in BUILTIN_TASKS if t.stage == stage_filter] if stage_filter else BUILTIN_TASKS

    result = run_benchmark_suite(tasks=tasks, commit_sha=commit)

    if getattr(args, "record", True):
        saved_path = record_benchmark_run(result, metrics_path=metrics_path)
    else:
        saved_path = None

    if getattr(args, "json", False):
        out = result.to_dict()
        if saved_path:
            out["recorded_to"] = str(saved_path)
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    print(f"═══ AI4AI-Bench Recursive Self-Improvement Benchmark ═══")
    print(f"Run ID: {result.run_id} | Commit: {result.commit_sha or 'unknown'}")
    print(f"Composite RSI Score: {result.composite_rsi_score * 100:.1f}% (Pass Rate: {result.pass_rate * 100:.1f}%, {result.passed_tasks}/{result.total_tasks} tasks)")
    print()
    print("📊 Stage Breakdown:")
    for stg, sc in result.stage_scores.items():
        print(f"  • {stg.capitalize():<12}: {sc * 100:.1f}%")
    print()
    print("📋 Task Results:")
    for tr in result.task_results:
        icon = "✓" if tr.passed else "✗"
        print(f"  [{icon}] {tr.task_id} ({tr.stage}): score={tr.score:.2f} ({tr.duration_ms}ms)")
        if tr.error:
            print(f"      Error: {tr.error}")
    if saved_path:
        print()
        print(f"✓ Recorded metrics to {saved_path}")

    return 0


def cmd_trend(args) -> int:
    days = getattr(args, "days", 7) or 7
    metrics_path = Path(args.metrics_path) if getattr(args, "metrics_path", None) else None
    history = load_benchmark_history(days=days, metrics_path=metrics_path)
    trend = compute_trend(history)

    if getattr(args, "json", False):
        print(json.dumps(trend, indent=2, ensure_ascii=False))
        return 0

    print(f"═══ AI4AI-Bench RSI Trend ({days}-Day Lookback) ═══")
    print(f"Historical Runs: {trend['runs_count']}")
    if trend['runs_count'] == 0:
        print("No benchmark runs recorded in this window. Run 'hermes evolution benchmark run' to start tracking.")
        return 0

    print(f"Current RSI Score : {trend['current_rsi_score'] * 100:.1f}%")
    print(f"Baseline RSI Score: {trend['baseline_rsi_score'] * 100:.1f}%")
    print(f"Average RSI Score : {trend['average_rsi_score'] * 100:.1f}%")
    print(f"Trajectory Trend  : {trend['rsi_trend'].upper()} (delta: {trend['delta']:+.2%})")

    if trend.get("regression_detected"):
        print("⚠️  REGRESSION WARNING: Significant RSI degradation detected (>5% drop)!")

    print()
    print("📈 Stage Averages:")
    for stg, val in trend.get("stage_averages", {}).items():
        print(f"  • {stg.capitalize():<12}: {val * 100:.1f}%")

    return 0


def cmd_tasks(args) -> int:
    if getattr(args, "json", False):
        print(json.dumps([asdict(t) for t in BUILTIN_TASKS], indent=2, ensure_ascii=False))
        return 0

    print(f"═══ AI4AI-Bench Held-Out Task Suite ({len(BUILTIN_TASKS)} tasks) ═══")
    for t in BUILTIN_TASKS:
        print(f"• [{t.stage.upper()}] {t.task_id}")
        print(f"  Title: {t.title}")
        print(f"  Target Component: {t.target_component}")
        print(f"  Description: {t.description}")
        if t.invariants:
            print(f"  Invariants: {', '.join(t.invariants)}")
        print()

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evolution_benchmark.py",
        description="AI4AI-Bench recursive self-improvement benchmark suite",
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="<command>")

    # run
    p_run = subparsers.add_parser("run", help="Run benchmark suite")
    p_run.add_argument("--stage", choices=["research", "proposal", "patch"], help="Filter by stage")
    p_run.add_argument("--no-record", dest="record", action="store_false", help="Do not append to metrics.jsonl")
    p_run.add_argument("--metrics-path", help="Custom metrics JSONL path")
    p_run.add_argument("--json", action="store_true", help="Output raw JSON")
    p_run.set_defaults(func=cmd_run)

    # trend
    p_trend = subparsers.add_parser("trend", help="Report historical RSI trend")
    p_trend.add_argument("--days", type=int, default=7, help="Lookback days (default: 7)")
    p_trend.add_argument("--metrics-path", help="Custom metrics JSONL path")
    p_trend.add_argument("--json", action="store_true", help="Output raw JSON")
    p_trend.set_defaults(func=cmd_trend)

    # tasks
    p_tasks = subparsers.add_parser("tasks", help="List benchmark tasks")
    p_tasks.add_argument("--json", action="store_true", help="Output raw JSON")
    p_tasks.set_defaults(func=cmd_tasks)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not hasattr(args, "func"):
        args = parser.parse_args(["run"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
