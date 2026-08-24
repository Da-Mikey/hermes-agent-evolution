#!/usr/bin/env python3
"""TrACE-style adaptive compute — disagreement-driven budget allocation (#86).

S1 of the #86 decomposition: the pure disagreement metric and budget rule from
TrACE ("Don't Overthink It", arXiv:2604.08369), plus a CLI that computes
per-step budgets from a JSONL of candidate actions. Off-core, zero blast
radius — S2 (instrumentation), S3 (config-gated wiring into the decision
layer) and S4 (calibration harness) follow as separate slices.

The idea: reasoning effort is currently static per task (#78). TrACE spends
compute only where rollouts DISAGREE on the next action — routine steps where
every sampled rollout picks the same action get a small budget; genuinely
ambiguous steps (split votes) get the full budget.

- :func:`disagreement_index` — how much the sampled rollouts disagree on the
  next action: ``1 - max vote fraction`` (0.0 when unanimous, approaching 1.0
  as the vote splits evenly).
- :func:`budget_rule` — linear ramp: ``base_budget * min_multiplier`` at full
  agreement, ramping to ``base_budget * max_multiplier`` at full
  disagreement, capped so no step ever exceeds the maximum.

Pure functions + explicit IO boundary (JSONL reader + CLI) so it is
import-safe and unit-testable. The CLI's JSONL schema (one JSON object per
line) is the S2 instrumentation contract:

    {"step": 3, "actions": ["search_files", "search_files", "read_file"]}
    {"actions": ["read_file", "read_file"]}      # step defaults to line number
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional


def _action_key(action: Any) -> str:
    """Canonical label for one candidate action.

    Strings label themselves. Dicts prefer a ``tool`` / ``name`` / ``action``
    key (tool-call shaped), falling back to the whole dict. Anything else is
    ``str()``-ed. Only the LABEL participates in disagreement — args do not.
    """
    if isinstance(action, str):
        return action
    if isinstance(action, dict):
        for key in ("tool", "name", "action"):
            if key in action:
                return str(action[key])
        return str(action)
    return str(action)


def disagreement_index(actions: Iterable[Any]) -> float:
    """Disagreement over the next action: ``1 - max vote fraction``.

    0.0 when all rollouts agree on the same action; approaches 1.0 as the
    vote splits evenly (e.g. two actions each getting half the votes → 0.5).
    Raises ``ValueError`` on an empty sequence — no votes means no signal.
    """
    labels = [_action_key(a) for a in actions]
    if not labels:
        raise ValueError("disagreement_index requires at least one action")
    counts = Counter(labels)
    max_fraction = max(counts.values()) / len(labels)
    return 1.0 - max_fraction


def budget_rule(
    disagreement: float,
    base_budget: float,
    min_multiplier: float = 1.0,
    max_multiplier: float = 3.0,
) -> float:
    """Allocate a compute budget for a step from its disagreement index.

    Linear ramp: ``base_budget * min_multiplier`` when the rollouts fully
    agree (disagreement 0), ``base_budget * max_multiplier`` at full
    disagreement (1.0), capped so no step exceeds the maximum. The index is
    clamped to ``[0, 1]`` defensively. If ``max_multiplier <= min_multiplier``
    the budget is flat at ``base_budget * min_multiplier`` (no ramp).
    """
    d = max(0.0, min(1.0, float(disagreement)))
    if max_multiplier <= min_multiplier:
        multiplier = float(min_multiplier)
    else:
        multiplier = min_multiplier + (max_multiplier - min_multiplier) * d
    return float(base_budget) * multiplier


def iter_steps(path: str) -> Iterable[Dict[str, Any]]:
    """Yield ``{"step": int, "actions": list}`` records from a JSONL file.

    ``step`` is taken from the record if present, else 1-based line order.
    Blank lines are skipped. Malformed JSON raises ``ValueError`` with the
    line number so the CLI can fail loudly instead of silently dropping data.
    """
    with open(path, encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"line {line_no}: malformed JSON — {exc}") from exc
            if not isinstance(record, dict) or not isinstance(
                record.get("actions"), list
            ):
                raise ValueError(
                    f"line {line_no}: expected {{'actions': [...]}} record, got {line[:80]!r}"
                )
            step = record.get("step", line_no)
            try:
                step = int(step)
            except (TypeError, ValueError):
                raise ValueError(
                    f"line {line_no}: 'step' must be an int, got {step!r}"
                ) from None
            yield {"step": step, "actions": record["actions"]}


def main(argv: List[str]) -> int:
    args = argv[1:]
    positional = [a for a in args if not a.startswith("--")]
    flags = {
        args[i]: args[i + 1] for i in range(len(args) - 1) if args[i].startswith("--")
    }

    if not positional or "--help" in args or "-h" in args:
        print(
            "usage: evolution_disagreement.py <steps.jsonl> [--base-budget N] "
            "[--min-multiplier N] [--max-multiplier N] [--json]",
            file=sys.stderr,
        )
        return 2

    try:
        base_budget = float(flags.get("--base-budget", 4.0))
        min_multiplier = float(flags.get("--min-multiplier", 1.0))
        max_multiplier = float(flags.get("--max-multiplier", 3.0))
    except ValueError as exc:
        print(f"[evolution-disagreement] bad flag value: {exc}", file=sys.stderr)
        return 2
    if base_budget < 0 or min_multiplier < 0 or max_multiplier < 0:
        print(
            "[evolution-disagreement] budgets/multipliers must be >= 0", file=sys.stderr
        )
        return 2

    try:
        steps = list(iter_steps(positional[0]))
    except (OSError, ValueError) as exc:
        print(f"[evolution-disagreement] {exc}", file=sys.stderr)
        return 2
    if not steps:
        print("[evolution-disagreement] no records found in input", file=sys.stderr)
        return 2

    rows = []
    total_budget = 0.0
    indices = []
    for record in steps:
        index = disagreement_index(record["actions"])
        budget = budget_rule(index, base_budget, min_multiplier, max_multiplier)
        rows.append({
            "step": record["step"],
            "index": round(index, 6),
            "budget": round(budget, 6),
        })
        total_budget += budget
        indices.append(index)

    if "--json" in args:
        print(
            json.dumps(
                {
                    "steps": rows,
                    "summary": {
                        "count": len(rows),
                        "mean_index": round(sum(indices) / len(indices), 6),
                        "total_budget": round(total_budget, 6),
                        "base_budget": base_budget,
                    },
                },
                indent=2,
            )
        )
    else:
        for row in rows:
            print(
                f"step={row['step']} disagreement={row['index']:.3f} budget={row['budget']:.3f}"
            )
        print(
            f"summary: {len(rows)} steps, mean disagreement "
            f"{sum(indices) / len(indices):.3f}, total budget {total_budget:.3f}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
