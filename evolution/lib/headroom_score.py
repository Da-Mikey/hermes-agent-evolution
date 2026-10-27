# -*- coding: utf-8 -*-
"""Headroom-normalised scoring for candidate edits (issue #208).

A raw pass-rate delta scores an edit that nudges an easy task by five points
exactly like an edit that closes the last five points of a hard gap.  That
biases the loop towards whatever is cheapest to move and away from the headroom
that actually matters.

This module adds the *headroom-normalised* score — the mean fraction of the
remaining gap an edit closes, the RSI-index shape used by RSIGym
(arXiv:2610.10310) — and keeps the raw delta beside it as a secondary column so
an absolute regression stays visible and the change stays auditable.

Design notes
------------
* Pure Python, no external dependencies, import-safe (matches the rest of the
  ``evolution/lib`` corpus).
* Degenerate cases are explicit rather than silent:

  - a task already at its ceiling has **no remaining gap**, so it is excluded
    from the mean instead of contributing ``0.0`` (which would dilute the score
    of an edit that closed real headroom elsewhere) or ``1.0`` (which would
    inflate it);
  - a task without a comparable sample in one of the runs is excluded and
    reported, never averaged in as a zero;
  - a regression contributes a **negative** fraction, clamped at ``-1.0`` — a
    task cannot lose more than everything it had left to gain.
  - a regression in a task that was already **at ceiling** cannot be expressed
    as a fraction at all (zero headroom to divide by).  It is recorded as
    ``regime="at_ceiling"`` with ``regressed=True``, and the edit's
    ``has_regression`` flag is set, so a downstream gate cannot miss it.

The public entry point is :func:`score_edit`; :func:`to_stage_result` wraps a
score in the pipeline's :class:`~evolution.lib.stage_result.StageResult`
contract so both metrics travel with the stage's evidence pointers.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from evolution.lib.stage_result import StageResult

#: Default ceiling for a normalised pass rate.
PERFECT = 1.0

#: Tolerance below which a task is treated as already at its ceiling.
_EPS = 1e-9

#: Regimes a task can be classified into.
SCORED = "scored"
AT_CEILING = "at_ceiling"
MISSING_BEFORE = "missing_before"
MISSING_AFTER = "missing_after"

__all__ = [
    "AT_CEILING",
    "EditScore",
    "MISSING_AFTER",
    "MISSING_BEFORE",
    "PERFECT",
    "SCORED",
    "TaskScore",
    "gap_closed_fraction",
    "score_edit",
    "to_stage_result",
]


def _clamp_fraction(value: float) -> float:
    """Clamp a normalised fraction to ``[-1.0, 1.0]``."""
    return max(-1.0, min(1.0, value))


def gap_closed_fraction(
    before: float,
    after: float,
    ceiling: float = PERFECT,
) -> float | None:
    """Fraction of the remaining gap that ``before -> after`` closes.

    Computed as ``(after - before) / (ceiling - before)`` and clamped to
    ``[-1.0, 1.0]``.

    Returns ``None`` when there is no remaining headroom to normalise against
    (``before`` at or above ``ceiling``).  Callers must not average a ``None``
    as if it were ``0.0``; :func:`score_edit` excludes such tasks instead.

    Raises
    ------
    ValueError
        If ``ceiling`` is not strictly positive — a non-positive ceiling makes
        the fraction meaningless, so it is rejected rather than guessed at.
    """
    ceiling = float(ceiling)
    if ceiling <= 0:
        raise ValueError(f"ceiling must be > 0, got {ceiling!r}")
    before = float(before)
    after = float(after)
    remaining = ceiling - before
    if remaining <= _EPS:
        return None
    return _clamp_fraction((after - before) / remaining)


@dataclass(frozen=True)
class TaskScore:
    """Per-task scoring record.

    Attributes
    ----------
    task
        Task identifier.
    before, after
        Pass rates (or scores) before and after the candidate edit.
    ceiling
        The task's ceiling — the pass rate at which it has no headroom left.
    raw_delta
        ``after - before``: the secondary column, kept so absolute movement is
        still visible.
    gap_closed
        Fraction of the remaining gap closed, or ``None`` when the task was not
        scorable (at ceiling, or missing from one run).
    regime
        One of ``"scored"``, ``"at_ceiling"``, ``"missing_before"``,
        ``"missing_after"``.
    regressed
        True when ``after < before``, whether or not a fraction could be
        computed.
    note
        Human-readable explanation for an unscorable task.
    """

    task: str
    before: float
    after: float
    ceiling: float
    raw_delta: float
    gap_closed: float | None
    regime: str
    regressed: bool = False
    note: str = ""

    @property
    def scored(self) -> bool:
        """True when this task contributed to ``headroom_score``."""
        return self.regime == SCORED

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "before": self.before,
            "after": self.after,
            "ceiling": self.ceiling,
            "raw_delta": self.raw_delta,
            "gap_closed": self.gap_closed,
            "regime": self.regime,
            "regressed": self.regressed,
            "note": self.note,
        }


@dataclass(frozen=True)
class EditScore:
    """Score for one candidate edit.

    ``headroom_score`` is the primary acceptance signal; ``raw_delta`` is kept
    as the secondary column the issue asks for.
    """

    raw_delta: float
    headroom_score: float
    tasks: tuple[TaskScore, ...] = field(default_factory=tuple)

    # -- derived views ----------------------------------------------------

    @property
    def scored_tasks(self) -> tuple[TaskScore, ...]:
        return tuple(t for t in self.tasks if t.scored)

    @property
    def at_ceiling(self) -> tuple[str, ...]:
        return tuple(t.task for t in self.tasks if t.regime == AT_CEILING)

    @property
    def unscored(self) -> tuple[str, ...]:
        """Tasks excluded because one of the runs had no sample for them."""
        return tuple(
            t.task for t in self.tasks if t.regime in (MISSING_BEFORE, MISSING_AFTER)
        )

    @property
    def moved(self) -> tuple[str, ...]:
        """Tasks whose pass rate moved at all, in either direction."""
        return tuple(t.task for t in self.tasks if t.raw_delta != 0.0)

    @property
    def regressed(self) -> tuple[str, ...]:
        return tuple(t.task for t in self.tasks if t.regressed)

    @property
    def has_regression(self) -> bool:
        """True if any task regressed, including a regression from ceiling."""
        return bool(self.regressed)

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable report: both metrics plus the moved tasks."""
        return {
            "raw_delta": self.raw_delta,
            "headroom_score": self.headroom_score,
            "tasks_scored": len(self.scored_tasks),
            "moved": list(self.moved),
            "regressed": list(self.regressed),
            "at_ceiling": list(self.at_ceiling),
            "unscored": list(self.unscored),
            "has_regression": self.has_regression,
            "tasks": [t.to_dict() for t in self.tasks],
        }


def score_edit(
    before: Mapping[str, float],
    after: Mapping[str, float],
    ceilings: Mapping[str, float] | float = PERFECT,
) -> EditScore:
    """Score a candidate edit by the fraction of remaining gap it closes.

    Parameters
    ----------
    before
        Task -> pass rate measured before the edit.
    after
        Task -> pass rate measured after the edit.
    ceilings
        Either a single ceiling applied to every task, or a mapping of
        task -> ceiling.  Tasks absent from the mapping fall back to
        :data:`PERFECT`.

    Returns
    -------
    EditScore
        ``headroom_score`` is the mean ``gap_closed`` across the tasks that had
        remaining headroom (``0.0`` when no task did — see ``at_ceiling``).
        ``raw_delta`` is the mean absolute delta across every *comparable* task,
        so it stays directly comparable with the historical metric.
    """
    before_map = {str(k): float(v) for k, v in dict(before).items()}
    after_map = {str(k): float(v) for k, v in dict(after).items()}
    ceiling_map: Mapping[str, float] | None
    default_ceiling: float
    if isinstance(ceilings, Mapping):
        ceiling_map = ceilings
        default_ceiling = PERFECT
    else:
        ceiling_map = None
        default_ceiling = float(ceilings)

    tasks: list[TaskScore] = []
    for task in sorted(set(before_map) | set(after_map)):
        ceiling = (
            float(ceiling_map.get(task, default_ceiling))
            if ceiling_map is not None
            else default_ceiling
        )
        if ceiling <= 0:
            raise ValueError(f"ceiling for {task!r} must be > 0, got {ceiling!r}")

        in_before, in_after = task in before_map, task in after_map
        if not in_before or not in_after:
            # Never average an absent sample in as a zero: report and exclude.
            tasks.append(
                TaskScore(
                    task=task,
                    before=before_map.get(task, 0.0),
                    after=after_map.get(task, 0.0),
                    ceiling=ceiling,
                    raw_delta=0.0,
                    gap_closed=None,
                    regime=MISSING_BEFORE if not in_before else MISSING_AFTER,
                    note=(
                        f"no sample in the {'before' if not in_before else 'after'} run"
                        " — excluded from both metrics"
                    ),
                )
            )
            continue

        prev, now = before_map[task], after_map[task]
        fraction = gap_closed_fraction(prev, now, ceiling)
        if fraction is None:
            tasks.append(
                TaskScore(
                    task=task,
                    before=prev,
                    after=now,
                    ceiling=ceiling,
                    raw_delta=now - prev,
                    gap_closed=None,
                    regime=AT_CEILING,
                    regressed=now < prev,
                    note=(
                        "already at ceiling — no remaining gap to normalise "
                        "against, excluded from the headroom mean"
                    ),
                )
            )
            continue

        tasks.append(
            TaskScore(
                task=task,
                before=prev,
                after=now,
                ceiling=ceiling,
                raw_delta=now - prev,
                gap_closed=fraction,
                regime=SCORED,
                regressed=now < prev,
            )
        )

    scored = [t for t in tasks if t.scored]
    comparable = [t for t in tasks if t.regime in (SCORED, AT_CEILING)]

    fractions: list[float] = []
    for task_score in tasks:
        if task_score.scored and task_score.gap_closed is not None:
            fractions.append(float(task_score.gap_closed))

    headroom_score = sum(fractions) / len(fractions) if fractions else 0.0
    raw_delta = (
        sum(t.raw_delta for t in comparable) / len(comparable) if comparable else 0.0
    )

    return EditScore(
        raw_delta=raw_delta,
        headroom_score=headroom_score,
        tasks=tuple(tasks),
    )


def to_stage_result(
    score: EditScore,
    *,
    evidence_pointers: Iterable[str] | None = None,
    stage: str = "headroom_score",
    confidence: int = 0,
    timestamp: str = "",
) -> StageResult:
    """Wrap an :class:`EditScore` in the pipeline's ``StageResult`` contract.

    Both metrics travel in ``result`` (``raw_delta`` and ``headroom_score``)
    alongside the per-task detail, so downstream stages inherit the normalised
    signal without a second computation.
    """
    return StageResult.wrap(
        score.to_dict(),
        list(evidence_pointers or []),
        confidence=confidence,
        stage=stage,
        timestamp=timestamp,
    )
