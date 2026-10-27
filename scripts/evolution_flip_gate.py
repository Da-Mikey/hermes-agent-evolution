#!/usr/bin/env python3
"""Example-level flip gate for skill promotion (issue #1446, Child B of #1308).

The promotion decision used to be "did the aggregate score go up?". AgentDevel
(arXiv:2601.04620) shows that is the question that ships regressions: an
aggregate can rise while specific previously-working cases break. Their ablation
puts the regression rate at **14.8% with 4 bad releases** without example-level
flip gating, and **3.1% with 0** with the full release-engineering pipeline.

This module answers the reliability question instead — *did we break anything
that worked?* — by classifying every probe into one of four cells:

    P->F  regression   the alarming one: it passed before, it fails now
    F->P  fix          the gain
    P->P  held         unchanged pass
    F->F  still broken unchanged fail

and blocking promotion when P->F exceeds a threshold, no matter how many F->P
gains sit beside it. Gains do not buy regressions.

Slice A (#1355, ``evolution_probe_set.py``) defines the frozen probe sets this
consumes. Wiring the verdict into merge verification is Child C (#1447), and
version metadata on promoted skills is Child D (#1448) — this module only
produces the table and the verdict.

Deterministic: no LLM, no network. Pure functions plus a thin CLI, matching the
``evolution_*.py`` idiom (JSON to stdout, distinct exit codes).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "1"

#: The four outcome cells.
REGRESSION = "P->F"
FIX = "F->P"
HELD = "P->P"
STILL_BROKEN = "F->F"

#: Probes present in one run but not the other. Never silently dropped: a probe
#: that vanished between versions may be *hiding* a regression, and one that
#: appeared has no baseline to be judged against.
MISSING_AFTER = "missing-after"
MISSING_BEFORE = "missing-before"

#: A sample the harness could not evaluate (safety gate refused the query, or
#: the install control failed identically). Not a pass and not a fail: the
#: candidate was never given a fair run, so attributing the outcome to it is
#: unsound (#287). Reported separately and never counted as a regression.
INCONCLUSIVE = "inconclusive"

#: Default tolerance. Zero on purpose — the entire premise is that a single
#: broken previously-working example is worth a human look. Callers that need
#: slack must ask for it explicitly.
DEFAULT_MAX_REGRESSIONS = 0

PROMOTE = "promote"
BLOCK = "block"


def _load_score_edit():
    """Import the headroom scorer, or ``None`` when ``evolution.lib`` is absent.

    The gate is a standalone script under ``scripts/`` and is also imported by
    ``evolution_merge_gate``; in both cases the repo root has to be on
    ``sys.path`` for ``evolution.lib`` to resolve. A deploy that ships only
    ``scripts/`` still gets a working gate — the headroom column is additive
    evidence and must never be able to change or suppress the verdict (#208).
    """
    try:
        from evolution.lib.headroom_score import score_edit  # noqa: E402
    except ImportError:
        root = str(Path(__file__).resolve().parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            from evolution.lib.headroom_score import score_edit  # noqa: E402
        except ImportError:
            return None
    return score_edit


@dataclass
class FlipTable:
    """Per-probe outcome deltas between two runs of the same probe set."""

    cells: Dict[str, str] = field(default_factory=dict)  # probe id -> cell

    def ids_in(self, cell: str) -> List[str]:
        """Probe ids landing in ``cell``, sorted for stable output."""
        return sorted(pid for pid, c in self.cells.items() if c == cell)

    def count(self, cell: str) -> int:
        return sum(1 for c in self.cells.values() if c == cell)

    @property
    def regressions(self) -> List[str]:
        return self.ids_in(REGRESSION)

    @property
    def fixes(self) -> List[str]:
        return self.ids_in(FIX)

    @property
    def inconclusive(self) -> List[str]:
        return self.ids_in(INCONCLUSIVE)

    def pass_rates(self) -> tuple[Dict[str, float], Dict[str, float]]:
        """Reconstruct before/after pass rates (0.0/1.0) from the cells.

        The table is already a lossless summary of the two runs for every
        comparable probe: P->P and P->F passed before, P->P and F->P pass
        after. Probes that are missing or inconclusive in either run are
        excluded — averaging an absent sample in as a zero is the defect
        ``headroom_score`` exists to avoid.
        """
        before: Dict[str, float] = {}
        after: Dict[str, float] = {}
        for pid, cell in self.cells.items():
            if cell in (MISSING_AFTER, MISSING_BEFORE, INCONCLUSIVE):
                continue
            before[pid] = 1.0 if cell in (HELD, REGRESSION) else 0.0
            after[pid] = 1.0 if cell in (HELD, FIX) else 0.0
        return before, after

    def metrics(self) -> Optional[Dict[str, Any]]:
        """Headroom-normalised score for this edit, or ``None`` if unavailable.

        The primary column is the mean fraction of the remaining gap the edit
        closed (#208, RSIGym RSI-index shape); ``raw_delta`` is retained beside
        it as the secondary column so absolute movement stays auditable. The
        promotion rule is deliberately still driven by P->F counts — the issue
        requires a backtest of archived candidates before that default flips.
        """
        score_edit = _load_score_edit()
        if score_edit is None:
            return None
        before, after = self.pass_rates()
        try:
            return score_edit(before, after).to_dict()
        except (TypeError, ValueError):  # defensive: never let a metric abort a gate
            return None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "cells": dict(sorted(self.cells.items())),
            "counts": {
                cell: self.count(cell)
                for cell in (
                    REGRESSION,
                    FIX,
                    HELD,
                    STILL_BROKEN,
                    MISSING_AFTER,
                    MISSING_BEFORE,
                    INCONCLUSIVE,
                )
            },
            "metrics": self.metrics(),
        }


@dataclass
class FlipVerdict:
    """The promotion decision plus the reason it was reached."""

    verdict: str
    reason: str
    regressions: List[str] = field(default_factory=list)
    fixes: List[str] = field(default_factory=list)
    inconclusive: List[str] = field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.verdict == BLOCK

    def to_dict(self) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "regressions": list(self.regressions),
            "fixes": list(self.fixes),
            "inconclusive": list(self.inconclusive),
        }


def _passed(value: Any) -> bool:
    """Coerce a probe result into pass/fail.

    Accepts the shapes a grader is likely to emit: a bare bool, a dict with
    ``passed``/``pass``/``ok``/``success``, or a status string. Anything
    unrecognised is treated as a FAIL — an unreadable result must not be able
    to look like a pass and let a regression through.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        for key in ("passed", "pass", "ok", "success"):
            if key in value:
                return bool(value[key])
        status = str(value.get("status", "")).strip().lower()
        if status:
            return status in ("pass", "passed", "ok", "success")
        return False
    if isinstance(value, str):
        return value.strip().lower() in ("pass", "passed", "ok", "success", "true")
    return False


def _inconclusive(value: Any) -> bool:
    """True when a probe result is marked *inconclusive* rather than passed/failed.

    A harness can fail to evaluate a sample for reasons that say nothing about
    the candidate: the runtime safety gate refuses the query outright (#287), or
    the untouched install control fails the same case by variance. Scoring that
    as a failure attributes the harness's own refusal to the PR.

    The marker is read from an explicit flag, never guessed from free text:
    detecting a refusal signature in the agent's *answer* is the scoreboard's
    job, and it records ``inconclusive: true`` in the run JSON. This function
    only consumes that flag, so the gate cannot start masking real failures
    because an answer happened to contain the word "blocked".

    Accepted shapes: ``{"inconclusive": True}`` (optionally alongside
    ``passed``/``ok``), or a status string of exactly ``"inconclusive"`` or
    ``"refused"``.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, dict):
        if value.get("inconclusive"):
            return True
        status = str(value.get("status", "")).strip().lower()
        return status in ("inconclusive", "refused")
    if isinstance(value, str):
        return value.strip().lower() in ("inconclusive", "refused")
    return False


def compare_runs(before: Dict[str, Any], after: Dict[str, Any]) -> FlipTable:
    """Classify every probe id into one of the four cells (or a missing marker).

    ``before`` and ``after`` map probe id -> result, in any of the shapes
    :func:`_passed` accepts. Ids present in only one run are recorded as
    ``missing-after`` / ``missing-before`` rather than dropped.

    A probe marked inconclusive in *either* run gets its own cell: no
    P->F / F->P verdict can be drawn from a sample the harness could not
    evaluate, so it is neither a regression nor a missing probe (#287). An
    inconclusive *baseline* is treated the same way, because a run that could
    not evaluate the probe cannot anchor a comparison either.
    """
    table = FlipTable()
    for pid in set(before) | set(after):
        in_before, in_after = pid in before, pid in after
        if in_before and not in_after:
            table.cells[pid] = MISSING_AFTER
            continue
        if in_after and not in_before:
            table.cells[pid] = MISSING_BEFORE
            continue
        if _inconclusive(before[pid]) or _inconclusive(after[pid]):
            table.cells[pid] = INCONCLUSIVE
            continue
        was, now = _passed(before[pid]), _passed(after[pid])
        if was and not now:
            table.cells[pid] = REGRESSION
        elif not was and now:
            table.cells[pid] = FIX
        elif was:
            table.cells[pid] = HELD
        else:
            table.cells[pid] = STILL_BROKEN
    return table


def _inconclusive_note(ids: List[str]) -> str:
    """Verdict-line suffix naming inconclusive probes, or ``""`` if none.

    Kept in the verdict *reason* rather than the violation list on purpose: an
    inconclusive sample must not block a merge (#287 DoD 3), but a *persistent*
    refusal has to stay visible to whoever reads the gate, so it is always
    named in the line that explains the verdict.
    """
    if not ids:
        return ""
    return (
        f"; {len(ids)} probe(s) inconclusive ({', '.join(ids[:5])}) — the harness "
        f"could not evaluate the sample (refusal or failed install control), so "
        f"it is reported here instead of being scored as a regression"
    )


def flip_verdict(
    table: FlipTable,
    max_regressions: int = DEFAULT_MAX_REGRESSIONS,
    *,
    block_on_missing: bool = True,
) -> FlipVerdict:
    """Decide promote/block from a :class:`FlipTable`.

    Blocks when P->F exceeds ``max_regressions``, regardless of how many F->P
    gains accompany them — the point of the gate is that gains do not buy
    regressions.

    ``block_on_missing`` also blocks when a probe disappeared between runs. A
    probe set is frozen by contract (#1355); one going missing means either the
    set was mutated or the run did not complete, and both make the comparison
    unsound rather than merely incomplete.
    """
    regressions = table.regressions
    fixes = table.fixes
    inconclusive = table.inconclusive
    n_reg = len(regressions)
    note = _inconclusive_note(inconclusive)

    if block_on_missing:
        missing = table.ids_in(MISSING_AFTER)
        if missing:
            return FlipVerdict(
                verdict=BLOCK,
                reason=(
                    f"{len(missing)} probe(s) present before and absent after "
                    f"({', '.join(missing[:5])}) — the probe set is frozen by "
                    f"contract, so a comparison missing probes is unsound"
                    f"{note}"
                ),
                regressions=regressions,
                fixes=fixes,
                inconclusive=inconclusive,
            )

    if n_reg > max_regressions:
        return FlipVerdict(
            verdict=BLOCK,
            reason=(
                f"{n_reg} regression(s) ({', '.join(regressions[:5])}) exceed the "
                f"allowed {max_regressions}; {len(fixes)} fix(es) do not offset "
                f"breaking what previously worked{note}"
            ),
            regressions=regressions,
            fixes=fixes,
            inconclusive=inconclusive,
        )

    return FlipVerdict(
        verdict=PROMOTE,
        reason=(
            f"{n_reg} regression(s) within the allowed {max_regressions}; "
            f"{len(fixes)} fix(es), {table.count(HELD)} held{note}"
        ),
        regressions=regressions,
        fixes=fixes,
        inconclusive=inconclusive,
    )


def format_table(table: FlipTable, verdict: FlipVerdict) -> str:
    """Human-readable summary for a PR comment or a log line."""
    lines = [
        f"[flip-gate] {verdict.verdict.upper()}: {verdict.reason}",
        f"  {REGRESSION} {table.count(REGRESSION)}  "
        f"{FIX} {table.count(FIX)}  "
        f"{HELD} {table.count(HELD)}  "
        f"{STILL_BROKEN} {table.count(STILL_BROKEN)}  "
        f"inconclusive {table.count(INCONCLUSIVE)}",
    ]
    for cell, label in (
        (REGRESSION, "regressed"),
        (FIX, "fixed"),
        (INCONCLUSIVE, "inconclusive (not scored as a regression)"),
    ):
        ids = table.ids_in(cell)
        if ids:
            lines.append(f"  {label}: {', '.join(ids)}")
    for cell in (MISSING_AFTER, MISSING_BEFORE):
        ids = table.ids_in(cell)
        if ids:
            lines.append(f"  {cell}: {', '.join(ids)}")
    metrics = table.metrics()
    if metrics is not None:
        lines.append(
            f"  headroom score {metrics['headroom_score']:+.3f} "
            f"(fraction of remaining gap closed; raw delta "
            f"{metrics['raw_delta']:+.3f} over {metrics['tasks_scored']} scorable "
            f"probe(s))"
        )
    return "\n".join(lines)


def _load(path: Path) -> Dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected an object mapping probe id -> result")
    return data


def _usage() -> str:
    return (
        "usage: evolution_flip_gate.py <before.json> <after.json> "
        "[--max-regressions N] [--allow-missing]\n"
        "  Each file maps probe id -> result (bool, status string, or an object\n"
        "  with passed/ok/success/status).\n"
        "  Exit 0 = promote, 1 = block, 2 = bad input."
    )


def main(argv: Optional[List[str]] = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--help" in args or "-h" in args:
        print(_usage())
        return 0

    max_regressions = DEFAULT_MAX_REGRESSIONS
    if "--max-regressions" in args:
        i = args.index("--max-regressions")
        try:
            max_regressions = int(args[i + 1])
            del args[i : i + 2]
        except (IndexError, ValueError):
            print(_usage(), file=sys.stderr)
            return 2

    block_on_missing = True
    if "--allow-missing" in args:
        block_on_missing = False
        args.remove("--allow-missing")

    if len(args) != 2:
        print(_usage(), file=sys.stderr)
        return 2

    try:
        before, after = _load(Path(args[0])), _load(Path(args[1]))
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    table = compare_runs(before, after)
    verdict = flip_verdict(table, max_regressions, block_on_missing=block_on_missing)

    print(json.dumps({**table.to_dict(), **verdict.to_dict()}, indent=2))
    print(format_table(table, verdict), file=sys.stderr)
    return 1 if verdict.blocked else 0


if __name__ == "__main__":
    sys.exit(main())
