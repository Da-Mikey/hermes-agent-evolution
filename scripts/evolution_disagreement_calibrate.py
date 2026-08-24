#!/usr/bin/env python3
"""TrACE-style adaptive compute — S4 calibration harness for the S1 metric (#86).

The REAL CONSUMER of ``scripts/evolution_disagreement.py`` (S1). The
integration review of PR #3154 rejected S1 as dead code: a module nothing
calls does not merge. This harness is the S4 calibration slice — it invokes
the S1 CLI as a subprocess over a JSONL of per-step candidate actions
(schema: ``{"step": int?, "actions": [...]}``), computes the disagreement
index and adaptive budget per step, and compares the adaptive total against
the FIXED-EFFORT baseline the system pays today: the FULL budget on every
step (``base_budget * max_multiplier * N`` — static effort never discounts
a step). That comparison is exactly what #86's success criteria ask for:

- routine steps (rollouts agree) should cost LESS than fixed effort;
- ambiguous steps (rollouts split) must keep the full budget (accuracy
  preserved — they cost the same as the static baseline).

Usage:

    evolution_disagreement_calibrate.py <steps.jsonl> [--base-budget N]
        [--min-multiplier N] [--max-multiplier N] [--json]

Exit codes: 0 = report produced; 2 = bad input/flags (mirrors the S1 CLI).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

_HERE = Path(__file__).resolve().parent
_S1_CLI = _HERE / "evolution_disagreement.py"


def _invoke_s1(jsonl_path: str, base_budget: float, min_mult: float, max_mult: float) -> Dict[str, Any]:
    """Run the S1 CLI (the metric + budget rule) over the input, as a subprocess.

    The harness deliberately goes through the CLI's public boundary rather
    than importing the module: the CLI is the S2 instrumentation contract,
    so exercising it end-to-end is the integration the review asked for.
    """
    proc = subprocess.run(
        [
            sys.executable,
            str(_S1_CLI),
            jsonl_path,
            "--base-budget",
            str(base_budget),
            "--min-multiplier",
            str(min_mult),
            "--max-multiplier",
            str(max_mult),
            "--json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError(
            f"S1 CLI failed (rc {proc.returncode}): "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise ValueError(f"S1 CLI returned malformed JSON: {exc}") from exc


def calibrate(
    jsonl_path: str,
    base_budget: float,
    min_mult: float = 1.0,
    max_mult: float = 3.0,
) -> Dict[str, Any]:
    """Run the calibration pass: per-step budgets + fixed-effort comparison.

    Returns a report dict:

        {
          "steps": [{"step": int, "index": float, "budget": float}, ...],
          "summary": {
            "count": int, "mean_index": float,
            "adaptive_total": float, "fixed_baseline": float,
            "savings": float,           # fixed_baseline - adaptive_total
            "savings_pct": float,       # savings / fixed_baseline * 100
            "ambiguous_steps": int,     # index >= 0.5 (split votes)
            "at_min_budget": int,       # steps paying the floor (routine)
            "verdict": str,
          },
        }

    The verdict reads out the calibration meaning: routine steps discounted,
    ambiguous steps preserved — the #86 success criteria, made checkable.
    """
    report = _invoke_s1(jsonl_path, base_budget, min_mult, max_mult)
    steps = report.get("steps") or []
    summary = dict(report.get("summary") or {})
    count = len(steps)
    adaptive_total = float(summary.get("total_budget", 0.0))
    # Fixed-effort baseline = FULL budget on every step (static effort never
    # discounts a step): base_budget * max_multiplier per step.
    fixed_baseline = float(base_budget) * float(max_mult) * count
    savings = fixed_baseline - adaptive_total
    savings_pct = (savings / fixed_baseline * 100.0) if fixed_baseline else 0.0
    ambiguous = [s for s in steps if float(s.get("index", 0.0)) >= 0.5]
    # Routine steps paying the FLOOR (budget == base * min_multiplier) — the
    # discounted steps that produce the savings. (A "preserved at max budget"
    # counter would be dead: disagreement_index == 1.0 is unreachable for any
    # real vote, so no step is ever exactly at the ceiling.)
    at_min_budget = [
        s
        for s in steps
        if abs(float(s.get("budget", 0.0)) - float(base_budget) * min_mult) < 1e-9
    ]
    if count == 0:
        verdict = "no steps"
    elif savings_pct > 1.0 and len(ambiguous) <= count * 0.5:
        verdict = "routine steps discounted; ambiguous steps preserved"
    elif savings_pct > 1.0:
        verdict = "savings present but most steps are ambiguous — budget may be too conservative"
    else:
        verdict = "no savings — workload is uniformly ambiguous or unanimous at max budget"
    summary.update(
        {
            "count": count,
            "mean_index": float(summary.get("mean_index", 0.0)),
            "adaptive_total": round(adaptive_total, 6),
            "fixed_baseline": round(fixed_baseline, 6),
            "savings": round(savings, 6),
            "savings_pct": round(savings_pct, 6),
            "ambiguous_steps": len(ambiguous),
            "at_min_budget": len(at_min_budget),
            "verdict": verdict,
        }
    )
    return {"steps": steps, "summary": summary}


def main(argv: List[str]) -> int:
    args = argv[1:]
    positional = [a for a in args if not a.startswith("--")]
    flags = {args[i]: args[i + 1] for i in range(len(args) - 1) if args[i].startswith("--")}

    if not positional or "--help" in args or "-h" in args:
        print(
            "usage: evolution_disagreement_calibrate.py <steps.jsonl> "
            "[--base-budget N] [--min-multiplier N] [--max-multiplier N] [--json]",
            file=sys.stderr,
        )
        return 2

    try:
        base_budget = float(flags.get("--base-budget", 4.0))
        min_mult = float(flags.get("--min-multiplier", 1.0))
        max_mult = float(flags.get("--max-multiplier", 3.0))
    except ValueError as exc:
        print(f"[evolution-disagreement-calibrate] bad flag value: {exc}", file=sys.stderr)
        return 2

    try:
        report = calibrate(positional[0], base_budget, min_mult, max_mult)
    except (OSError, ValueError) as exc:
        print(f"[evolution-disagreement-calibrate] {exc}", file=sys.stderr)
        return 2

    if "--json" in args:
        print(json.dumps(report, indent=2))
    else:
        summary = report["summary"]
        print(f"steps={summary['count']} mean_disagreement={summary['mean_index']:.3f}")
        print(
            f"adaptive_total={summary['adaptive_total']:.3f} "
            f"fixed_baseline={summary['fixed_baseline']:.3f} "
            f"savings={summary['savings']:.3f} ({summary['savings_pct']:.1f}%)"
        )
        print(
            f"ambiguous_steps={summary['ambiguous_steps']} "
            f"at_min_budget={summary['at_min_budget']}"
        )
        print(f"verdict: {summary['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
