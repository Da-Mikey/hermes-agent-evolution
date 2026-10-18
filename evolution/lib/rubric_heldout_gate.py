# -*- coding: utf-8 -*-
"""RubricForge Slice 3 — held-out false-pass gate (#2782).

Child of #2760. An evolved rubric may overfit the labeled set it was
selected on (agreement 1.0 by construction). Before ADOPTION the winner is
re-measured on a HELD-OUT split it never saw: the gate permits adoption
only when the evolved rubric's false-pass rate on the held-out examples
does not exceed the GENERIC judge's baseline rate there.

A "false pass" is a labeled-bad example the rubric accepts (verdict True on
``label: False``) — the eval stage's failure mode RubricForge exists to
halve. Pure functions over the S1 primitives; no LLM, no IO.

Issue #215 (AgentSWE) adds the two acceptance siblings of that gate: a
hollow-runtime check (the held-out path must have been genuinely exercised
for every case it is scored on, not merely present) and an
unsupported-completion check (a claimed success must be backed by the
captured evidence). Both are pure and fail closed on unprovable claims.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from evolution.lib.rubric_forge import RubricJudge, score_rubric

logger = logging.getLogger(__name__)

__all__ = [
    "CompletionClaim",
    "HeldOutEvidence",
    "HeldOutVerdict",
    "HollowRuntimeVerdict",
    "UnsupportedCompletionVerdict",
    "held_out_false_pass_gate",
    "hollow_runtime_gate",
    "split_labeled_set",
    "unsupported_completion_gate",
]


@dataclass
class HeldOutVerdict:
    """Adoption decision for one evolved rubric."""

    adopt: bool
    evolved_false_pass_rate: float
    baseline_false_pass_rate: float
    held_out_size: int
    reason: str


def split_labeled_set(
    labeled: Sequence[Any],
    labels: Sequence[bool],
    *,
    holdout: int = 3,
) -> Tuple[List[Any], List[bool], List[Any], List[bool]]:
    """Deterministic tail split: the LAST ``holdout`` examples are held out.

    Deterministic (no RNG) so adoption decisions are reproducible; callers
    that want randomness shuffle before calling. Falls back to an empty
    held-out set when the labeled set is too small to split.
    """
    if len(labeled) != len(labels):
        raise ValueError("labeled and labels must be parallel sequences")
    holdout = max(0, min(holdout, len(labeled) // 2))
    cut = len(labeled) - holdout
    return (
        list(labeled[:cut]),
        list(labels[:cut]),
        list(labeled[cut:]),
        list(labels[cut:]),
    )


def _false_pass_rate(
    rubric: str, held_out: Sequence[Any], labels: Sequence[bool], judge: RubricJudge
) -> float:
    """Fraction of held-out labeled-BAD examples the rubric accepts anyway."""
    bad = [(ex, lb) for ex, lb in zip(held_out, labels) if not lb]
    if not bad:
        return 0.0
    false_passes = 0
    for ex, _lb in bad:
        try:
            if bool(judge(rubric, ex)):
                false_passes += 1
        except Exception:  # noqa: BLE001 - a throwing judge accepts nothing
            pass
    return false_passes / len(bad)


def held_out_false_pass_gate(
    evolved_rubric: str,
    generic_rubric: str,
    labeled: Sequence[Any],
    labels: Sequence[bool],
    judge: RubricJudge,
    *,
    holdout: int = 3,
) -> HeldOutVerdict:
    """Gate evolved-rubric adoption on held-out false-pass parity (#2782).

    Split the labeled set, re-check the evolved winner on the held-out tail,
    and compare its false-pass rate there against the generic judge's.
    Adoption is permitted only when the evolved rate does NOT exceed the
    baseline (``<=``). An empty held-out set cannot prove parity — the gate
    refuses (fail-closed) with the reason.
    """
    _, _, held_out, held_labels = split_labeled_set(labeled, labels, holdout=holdout)
    if not held_out:
        return HeldOutVerdict(
            adopt=False,
            evolved_false_pass_rate=0.0,
            baseline_false_pass_rate=0.0,
            held_out_size=0,
            reason="held-out set empty — parity unprovable, refusing adoption",
        )
    evolved_rate = _false_pass_rate(evolved_rubric, held_out, held_labels, judge)
    baseline_rate = _false_pass_rate(generic_rubric, held_out, held_labels, judge)
    adopt = evolved_rate <= baseline_rate
    reason = (
        "evolved false-pass rate within baseline on held-out examples"
        if adopt
        else "evolved rubric false-passes MORE on held-out examples — overfit, blocked"
    )
    return HeldOutVerdict(
        adopt=adopt,
        evolved_false_pass_rate=evolved_rate,
        baseline_false_pass_rate=baseline_rate,
        held_out_size=len(held_out),
        reason=reason,
    )


@dataclass
class HollowRuntimeVerdict:
    """Exercise decision for the held-out measurement path (#215)."""

    exercised: bool
    case_count: int
    unexercised_cases: List[str]
    reason: str


@dataclass
class CompletionClaim:
    """A stated completion outcome, checked against captured evidence (#215).

    ``adopt`` / ``passed`` are the claim's own assertions of success. The rate
    fields are the numbers the claim states for the held-out measurement; a
    success claimed without them cannot be supported, so ``None`` refuses.
    """

    adopt: bool = False
    passed: bool = False
    evolved_false_pass_rate: Optional[float] = None
    baseline_false_pass_rate: Optional[float] = None


@dataclass
class HeldOutEvidence:
    """Captured held-out measurements backing a completion claim (#215)."""

    held_out_size: int = 0
    evolved_false_pass_rate: Optional[float] = None
    baseline_false_pass_rate: Optional[float] = None


@dataclass
class UnsupportedCompletionVerdict:
    """Support decision for a claimed completion (#215)."""

    supported: bool
    held_out_size: int
    reason: str


def hollow_runtime_gate(
    invocations: Mapping[str, int] | None,
    *,
    expected_cases: Sequence[str] | None = None,
) -> HollowRuntimeVerdict:
    """Refuse adoption when the held-out path was not actually exercised (#215).

    AgentSWE's hollow-runtime failure mode: code that looks complete but whose
    path was never run. ``invocations`` maps held-out case id → the number of
    times the judge (or measured path) really ran for that case, captured by
    the caller — never assumed. ``expected_cases`` names every case the path
    must exercise; a case absent from ``invocations`` counts as zero.

    Fail-closed on unprovable exercise: missing or empty evidence refuses, an
    all-zero mapping refuses (never invoked), and any mix of zero and non-zero
    refuses too (partial exercise is still hollow). Only a case set that was
    exercised at least once for EVERY expected case permits adoption.
    """
    if invocations is None:
        return HollowRuntimeVerdict(
            exercised=False,
            case_count=0,
            unexercised_cases=[],
            reason="no execution evidence captured — unexercised path, refusing",
        )
    declared = [str(case) for case in (expected_cases or [])]
    observed = [str(case) for case in invocations]
    cases = list(dict.fromkeys(declared + observed))
    if not cases:
        return HollowRuntimeVerdict(
            exercised=False,
            case_count=0,
            unexercised_cases=[],
            reason="held-out execution evidence empty — unexercised path, refusing",
        )
    counts: Dict[str, int] = {}
    for case in cases:
        try:
            counts[case] = int(invocations.get(case, 0) or 0)
        except (TypeError, ValueError):
            counts[case] = 0
    unexercised = sorted(case for case, runs in counts.items() if runs <= 0)
    if not unexercised:
        return HollowRuntimeVerdict(
            exercised=True,
            case_count=len(cases),
            unexercised_cases=[],
            reason=f"held-out path exercised for all {len(cases)} case(s)",
        )
    if len(unexercised) == len(cases):
        return HollowRuntimeVerdict(
            exercised=False,
            case_count=len(cases),
            unexercised_cases=unexercised,
            reason=(
                f"held-out path never invoked for {len(cases)} case(s) "
                "— hollow runtime, refusing"
            ),
        )
    return HollowRuntimeVerdict(
        exercised=False,
        case_count=len(cases),
        unexercised_cases=unexercised,
        reason=(
            f"held-out path exercised for only {len(cases) - len(unexercised)}/{len(cases)} "
            "case(s) — partial exercise is hollow, refusing"
        ),
    )


def unsupported_completion_gate(
    claim: CompletionClaim | None,
    evidence: HeldOutEvidence | None = None,
) -> UnsupportedCompletionVerdict:
    """Fail a run whose claimed success the captured evidence cannot support (#215).

    AgentSWE's unsupported-completion failure mode: a report announcing a
    success the measurement never established. ``claim`` is the outcome the run
    states; ``evidence`` is what was actually captured for the held-out split.
    Purely evidence-derived: no LLM, no IO.

    A success is claimed when ``adopt`` or ``passed`` is true. Such a claim is
    refused (fail-closed) when no evidence was captured, when the held-out set
    is empty, when the claimed measured rates are absent, when the evidence
    carries no measured rates, or when the measured evolved false-pass rate
    exceeds the baseline — a claim the numbers contradict. A run that claims no
    success has nothing to support and passes.
    """
    held_out_size = evidence.held_out_size if evidence is not None else 0
    if claim is None or not (claim.adopt or claim.passed):
        return UnsupportedCompletionVerdict(
            supported=True,
            held_out_size=held_out_size,
            reason="no success claimed — nothing to support",
        )
    if evidence is None:
        return UnsupportedCompletionVerdict(
            supported=False,
            held_out_size=0,
            reason="success claimed with no captured evidence — unsupported, refusing",
        )
    if evidence.held_out_size <= 0:
        return UnsupportedCompletionVerdict(
            supported=False,
            held_out_size=evidence.held_out_size,
            reason=(
                "success claimed while the held-out set is empty — unsupported, refusing"
            ),
        )
    if claim.evolved_false_pass_rate is None or claim.baseline_false_pass_rate is None:
        return UnsupportedCompletionVerdict(
            supported=False,
            held_out_size=evidence.held_out_size,
            reason=(
                "success claimed but the claimed measured rates are absent "
                "— unsupported, refusing"
            ),
        )
    if (
        evidence.evolved_false_pass_rate is None
        or evidence.baseline_false_pass_rate is None
    ):
        return UnsupportedCompletionVerdict(
            supported=False,
            held_out_size=evidence.held_out_size,
            reason=(
                "success claimed but the captured evidence carries no measured rates "
                "— unsupported, refusing"
            ),
        )
    if evidence.evolved_false_pass_rate > evidence.baseline_false_pass_rate:
        return UnsupportedCompletionVerdict(
            supported=False,
            held_out_size=evidence.held_out_size,
            reason=(
                "success claimed but the measured evolved false-pass rate exceeds the "
                "baseline — claim contradicted by evidence, refusing"
            ),
        )
    return UnsupportedCompletionVerdict(
        supported=True,
        held_out_size=evidence.held_out_size,
        reason="claimed success is supported by the captured held-out evidence",
    )
