# -*- coding: utf-8 -*-
"""Failure-cause mutating counter — SLICE A of issue #3014.

Implements the mutating-step failure-cause counter from #2995 (SABER / Do-over
mutating-step safety). The snapshot-before-destructive primitive (the other
piece of #3014) is deferred to a next increment; this module ships the
measurement half only: record how often a failed run's root cause was a
mutating step and surface the share for realized-impact metrics. Pure, no
side effects on import, standard library only, thread-safe.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

__version__ = "1.0.0"


@dataclass
class FailureCauseSummary:
    """Aggregate of recorded failure causes.

    Attributes:
        failed_runs: Total number of failed runs recorded.
        mutating_cause: Count whose root cause was a mutating step.
        other_cause: Count whose root cause was NOT a mutating step.
        mutating_share: Fraction of failed runs caused by a mutating step
            (``None`` when no failures have been recorded).
    """

    failed_runs: int = 0
    mutating_cause: int = 0
    other_cause: int = 0
    mutating_share: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict (for realized-impact metrics)."""
        return {
            "failed_runs": self.failed_runs,
            "mutating_cause": self.mutating_cause,
            "other_cause": self.other_cause,
            "mutating_share": self.mutating_share,
        }


class MutatingFailureCounter:
    """Counts failed runs whose root cause was a mutating step.

    Appends one JSONL record per failed run and reports the share of failed
    runs whose root cause was a mutating step — the metric #2995 wants surfaced
    in realized-impact metrics.

    Ledger schema: ``{"run_id", "outcome": "failed", "cause_category":
    "mutating"|"other", "recorded_at": "YYYY-MM-DD"}``. Only failed runs are
    recorded.
    """

    def __init__(self, ledger_file: Optional[os.PathLike] = None) -> None:
        """Initialize with an optional ledger path."""
        if ledger_file is None:
            base = Path(
                os.environ.get(
                    "EVOLUTION_PROFILE_DIR", str(Path.home() / ".hermes" / "evolution")
                )
            )
            ledger_file = base / "mutation_guard" / "failure-causes.jsonl"
        self._ledger = Path(ledger_file)
        self._lock = threading.Lock()

    def record(
        self,
        run_id: str,
        cause_category: str,
        recorded_at: Optional[str] = None,
    ) -> None:
        """Record a failed run's root-cause category (mutating vs other)."""
        if cause_category not in ("mutating", "other"):
            raise ValueError("cause_category must be 'mutating' or 'other'")
        if not recorded_at:
            from datetime import date, datetime, timezone

            recorded_at = datetime.now(timezone.utc).date().isoformat()
        rec = {
            "run_id": str(run_id),
            "outcome": "failed",
            "cause_category": cause_category,
            "recorded_at": recorded_at,
        }
        with self._lock:
            self._ledger.parent.mkdir(parents=True, exist_ok=True)
            with open(self._ledger, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, sort_keys=True) + "\n")

    def summary(self) -> FailureCauseSummary:
        """Aggregate the ledger; malformed lines are skipped."""
        mutating = 0
        total = 0
        if self._ledger.exists():
            for ln in self._ledger.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except (json.JSONDecodeError, ValueError):
                    continue
                if rec.get("outcome") != "failed":
                    continue
                total += 1
                if rec.get("cause_category") == "mutating":
                    mutating += 1
        share = round(mutating / total, 3) if total else None
        return FailureCauseSummary(
            failed_runs=total,
            mutating_cause=mutating,
            other_cause=total - mutating,
            mutating_share=share,
        )
