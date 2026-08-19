#!/usr/bin/env python3
"""Calibrate reasoning effort per model (issue #78).

Treats reasoning effort as a *measured* quantity rather than a fixed knob.
The motivating observation: on at least one paired contrast, explicit high
reasoning effort cost more with no detected accuracy gain. This harness runs a
small representative task set at several effort levels, normalises each run's
usage into a real cost (via ``agent.usage_pricing``), and reports the deltas so
a per-model effort default can be chosen from evidence instead of assumption.

Two modes:

- **Post-process** (deterministic, CI-tested): ``--records FILE`` reads a
  JSONL of run records and prints the comparison report. Each record is
  ``{"task_id", "effort", "input_tokens", "output_tokens", "reasoning_tokens",
  "cache_read_tokens", "cache_write_tokens", "latency_seconds", "success",
  "score"}``.
- **Live run** (best-effort): ``--run --dataset FILE`` drives
  ``batch_runner.py`` once per ``--efforts`` level and then feeds the collected
  usage into the same pure comparison core. Requires the same credentials the
  agent already uses.

The pure comparison/aggregation core has no side effects and is covered by
``tests/scripts/test_calibrate_reasoning_effort.py``.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Sequence

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrationRecord:
    """One task run at one effort level, with token/usage accounting."""

    task_id: str
    effort: str
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_seconds: float = 0.0
    success: bool = True
    score: Optional[float] = None


@dataclass(frozen=True)
class EffortAggregate:
    """Per-effort aggregate across all tasks."""

    effort: str
    n_runs: int
    n_success: int
    total_cost_usd: Decimal
    total_reasoning_tokens: int
    total_output_tokens: int
    total_latency_seconds: float
    mean_score: Optional[float]

    @property
    def success_rate(self) -> float:
        return self.n_success / self.n_runs if self.n_runs else 0.0

    @property
    def mean_cost_usd(self) -> Decimal:
        if not self.n_runs:
            return Decimal("0")
        return self.total_cost_usd / Decimal(self.n_runs)

    @property
    def mean_latency_seconds(self) -> float:
        return self.total_latency_seconds / self.n_runs if self.n_runs else 0.0


@dataclass(frozen=True)
class EffortDelta:
    """Difference of one effort level against a baseline."""

    effort: str
    baseline: str
    cost_delta_usd: Decimal
    reasoning_token_delta: int
    latency_delta_seconds: float
    score_delta: Optional[float]


# ---------------------------------------------------------------------------
# Cost normalisation (the only part that touches agent internals)
# ---------------------------------------------------------------------------


def normalize_cost(
    record: CalibrationRecord,
    model: str,
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Decimal:
    """Turn a record's token counts into a USD cost via ``usage_pricing``.

    Returns ``Decimal("0")`` when the model's pricing is unknown, so the
    harness still produces a (cost-agnostic) report rather than crashing on an
    unpriced model.
    """
    from agent.usage_pricing import CanonicalUsage, estimate_usage_cost

    usage = CanonicalUsage(
        input_tokens=record.input_tokens,
        output_tokens=record.output_tokens,
        cache_read_tokens=record.cache_read_tokens,
        cache_write_tokens=record.cache_write_tokens,
        reasoning_tokens=record.reasoning_tokens,
    )
    result = estimate_usage_cost(
        model,
        usage,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
    )
    return result.amount_usd or Decimal("0")


# ---------------------------------------------------------------------------
# Pure comparison core
# ---------------------------------------------------------------------------


def parse_records(lines: Iterable[str]) -> List[CalibrationRecord]:
    """Parse a JSONL stream into ``CalibrationRecord`` objects.

    Skips blank lines. Raises ``ValueError`` with the offending line on a
    malformed or missing-field record so a bad input file fails loudly rather
    than silently dropping data.
    """
    records: List[CalibrationRecord] = []
    for lineno, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"record {lineno}: invalid JSON: {exc}") from exc
        if not isinstance(obj, dict) or "task_id" not in obj or "effort" not in obj:
            raise ValueError(
                f"record {lineno}: missing 'task_id' or 'effort' field"
            )
        records.append(
            CalibrationRecord(
                task_id=str(obj["task_id"]),
                effort=str(obj["effort"]),
                input_tokens=int(obj.get("input_tokens", 0) or 0),
                output_tokens=int(obj.get("output_tokens", 0) or 0),
                reasoning_tokens=int(obj.get("reasoning_tokens", 0) or 0),
                cache_read_tokens=int(obj.get("cache_read_tokens", 0) or 0),
                cache_write_tokens=int(obj.get("cache_write_tokens", 0) or 0),
                latency_seconds=float(obj.get("latency_seconds", 0.0) or 0.0),
                success=bool(obj.get("success", True)),
                score=(
                    float(obj["score"])
                    if obj.get("score") is not None
                    else None
                ),
            )
        )
    return records


def aggregate_by_effort(
    records: Sequence[CalibrationRecord],
    model: str,
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
) -> Dict[str, EffortAggregate]:
    """Group records by effort level, summing usage and normalising cost."""
    agg: Dict[str, EffortAggregate] = {}
    scores: Dict[str, List[float]] = {}
    for rec in records:
        cost = normalize_cost(
            rec,
            model,
            provider=provider,
            base_url=base_url,
            api_key=api_key,
        )
        if rec.effort not in agg:
            agg[rec.effort] = EffortAggregate(
                effort=rec.effort,
                n_runs=0,
                n_success=0,
                total_cost_usd=Decimal("0"),
                total_reasoning_tokens=0,
                total_output_tokens=0,
                total_latency_seconds=0.0,
                mean_score=None,
            )
        cur = agg[rec.effort]
        agg[rec.effort] = EffortAggregate(
            effort=cur.effort,
            n_runs=cur.n_runs + 1,
            n_success=cur.n_success + (1 if rec.success else 0),
            total_cost_usd=cur.total_cost_usd + cost,
            total_reasoning_tokens=cur.total_reasoning_tokens
            + rec.reasoning_tokens,
            total_output_tokens=cur.total_output_tokens + rec.output_tokens,
            total_latency_seconds=cur.total_latency_seconds
            + rec.latency_seconds,
            mean_score=None,
        )
        if rec.score is not None:
            scores.setdefault(rec.effort, []).append(rec.score)

    # Fold in mean scores (computed once all scores are gathered).
    result: Dict[str, EffortAggregate] = {}
    for effort, cur in agg.items():
        eff_scores = scores.get(effort)
        mean = (
            sum(eff_scores) / len(eff_scores)
            if eff_scores
            else None
        )
        result[effort] = EffortAggregate(
            effort=cur.effort,
            n_runs=cur.n_runs,
            n_success=cur.n_success,
            total_cost_usd=cur.total_cost_usd,
            total_reasoning_tokens=cur.total_reasoning_tokens,
            total_output_tokens=cur.total_output_tokens,
            total_latency_seconds=cur.total_latency_seconds,
            mean_score=mean,
        )
    return result


def compare_efforts(
    aggregates: Dict[str, EffortAggregate],
    baseline_effort: str,
) -> List[EffortDelta]:
    """Compute each effort level's delta against ``baseline_effort``.

    A missing baseline raises ``ValueError``; the baseline itself is omitted
    (its delta is trivially zero).
    """
    if baseline_effort not in aggregates:
        raise ValueError(
            f"baseline effort {baseline_effort!r} not present in aggregates "
            f"(have: {sorted(aggregates)})"
        )
    base = aggregates[baseline_effort]
    deltas: List[EffortDelta] = []
    for effort, cur in aggregates.items():
        if effort == baseline_effort:
            continue
        score_delta = None
        if cur.mean_score is not None and base.mean_score is not None:
            score_delta = cur.mean_score - base.mean_score
        deltas.append(
            EffortDelta(
                effort=effort,
                baseline=baseline_effort,
                cost_delta_usd=cur.mean_cost_usd - base.mean_cost_usd,
                reasoning_token_delta=(
                    cur.total_reasoning_tokens - base.total_reasoning_tokens
                ),
                latency_delta_seconds=(
                    cur.mean_latency_seconds - base.mean_latency_seconds
                ),
                score_delta=score_delta,
            )
        )
    return deltas


def render_report(
    aggregates: Dict[str, EffortAggregate],
    deltas: Sequence[EffortDelta],
    *,
    model: str,
) -> str:
    """Render a human-readable comparison table."""
    lines: List[str] = [
        f"Reasoning-effort calibration — {model}",
        "=" * 60,
    ]
    header = (
        f"{'effort':<10} {'runs':>5} {'succ%':>7} {'cost/run':>10} "
        f"{'reason':>8} {'lat(s)':>8} {'score':>7}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for effort in sorted(aggregates):
        cur = aggregates[effort]
        lines.append(
            f"{effort:<10} {cur.n_runs:>5} {cur.success_rate * 100:>6.0f}% "
            f"{cur.mean_cost_usd:>10.6f} {cur.total_reasoning_tokens:>8} "
            f"{cur.mean_latency_seconds:>8.2f} "
            f"{cur.mean_score if cur.mean_score is None else round(cur.mean_score, 4):>7}"
        )
    if deltas:
        lines.append("")
        lines.append(f"Deltas (vs baseline '{deltas[0].baseline}'):")
        for d in deltas:
            score_str = (
                "n/a" if d.score_delta is None else f"{d.score_delta:+.4f}"
            )
            lines.append(
                f"  {d.effort:<10} cost {d.cost_delta_usd:+.6f} USD/run, "
                f"reasoning {d.reasoning_token_delta:+d} tok, "
                f"latency {d.latency_delta_seconds:+.2f}s, score {score_str}"
            )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Live-run driver (best-effort; the pure core above is the tested path)
# ---------------------------------------------------------------------------


def run_live(
    dataset_file: str,
    model: str,
    efforts: Sequence[str],
) -> List[CalibrationRecord]:
    """Run ``batch_runner.py`` once per effort level and collect usage.

    Relies on ``batch_runner`` (which already accepts ``--reasoning_effort``)
    as a subprocess driver. This is inherently I/O-bound and environment
    dependent, so failures propagate rather than being papered over.
    """
    records: List[CalibrationRecord] = []
    for effort in efforts:
        cmd = [
            sys.executable,
            "-m",
            "batch_runner",
            "--dataset_file",
            dataset_file,
            "--model",
            model,
            "--reasoning_effort",
            effort,
        ]
        subprocess.run(cmd, check=True)
        # NOTE: batch_runner writes trajectories to its run output dir. A real
        # deployment wires the per-run usage here. Kept explicit so the gap is
        # visible rather than silently faked.
        raise NotImplementedError(
            "live --run needs the batch_runner trajectory→record adapter; "
            "use --records to feed collected usage instead"
        )
    return records


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Calibrate reasoning effort per model (issue #78)."
    )
    parser.add_argument("--model", default="", help="Model name for pricing lookup")
    parser.add_argument(
        "--records",
        help="JSONL of run records to post-process (deterministic mode).",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help="Baseline effort to compute deltas against (default: alphabetically first).",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="Live-run mode: drive batch_runner per effort level.",
    )
    parser.add_argument("--dataset", help="Dataset file for --run mode.")
    parser.add_argument(
        "--efforts",
        default="low,high",
        help="Comma-separated effort levels for --run mode (default: low,high).",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    if args.run:
        if not args.dataset:
            print("error: --run requires --dataset", file=sys.stderr)
            return 2
        records = run_live(
            args.dataset,
            args.model,
            [e.strip() for e in args.efforts.split(",") if e.strip()],
        )
    elif args.records:
        with open(args.records, "r", encoding="utf-8") as fh:
            records = parse_records(fh)
    else:
        print("error: provide --records (post-process) or --run (live)", file=sys.stderr)
        return 2

    if not records:
        print("no records", file=sys.stderr)
        return 1

    aggregates = aggregate_by_effort(records, args.model)
    baseline = args.baseline or min(aggregates)
    try:
        deltas = compare_efforts(aggregates, baseline)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(render_report(aggregates, deltas, model=args.model))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
