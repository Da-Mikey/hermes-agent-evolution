"""hermes evolution subcommand parser (issue #3064).

Provides recursive self-improvement benchmark suite execution (AI4AI-Bench),
historical metrics reporting, and generalization trend tracking.
"""

from __future__ import annotations

from typing import Callable


def build_evolution_parser(subparsers, *, cmd_evolution: Callable) -> None:
    """Attach the evolution subcommand to subparsers."""
    evo_parser = subparsers.add_parser(
        "evolution",
        help="Manage and benchmark Hermes recursive self-improvement pipeline",
        description=(
            "Inspect and evaluate the recursive self-improvement capabilities "
            "of Hermes using the AI4AI-Bench algorithmic-design benchmark suite."
        ),
    )
    evo_subparsers = evo_parser.add_subparsers(
        dest="evolution_command",
        metavar="<subcommand>",
    )

    # 1. benchmark
    bench_parser = evo_subparsers.add_parser(
        "benchmark",
        help="Run AI4AI-Bench self-improvement benchmark or inspect historical RSI trends",
    )
    bench_subparsers = bench_parser.add_subparsers(
        dest="benchmark_action",
        metavar="<action>",
    )

    # 1a. run
    p_run = bench_subparsers.add_parser("run", help="Run benchmark suite")
    p_run.add_argument("--stage", choices=["research", "proposal", "patch"], help="Filter by stage")
    p_run.add_argument("--no-record", dest="record", action="store_false", help="Do not record metrics")
    p_run.add_argument("--metrics-path", help="Custom metrics JSONL path")
    p_run.add_argument("--json", action="store_true", help="Output raw JSON")

    # 1b. trend
    p_trend = bench_subparsers.add_parser("trend", help="Report historical RSI trend over lookback window")
    p_trend.add_argument("--days", type=int, default=7, help="Lookback days (default: 7)")
    p_trend.add_argument("--metrics-path", help="Custom metrics JSONL path")
    p_trend.add_argument("--json", action="store_true", help="Output raw JSON")

    # 1c. tasks
    p_tasks = bench_subparsers.add_parser("tasks", help="List all held-out benchmark tasks")
    p_tasks.add_argument("--json", action="store_true", help="Output raw JSON")

    evo_parser.set_defaults(func=cmd_evolution)
