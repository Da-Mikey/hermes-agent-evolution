# -*- coding: utf-8 -*-
"""Mutation guard: snapshot-before-destructive primitive + failure-cause counter.

Implements issue #3014 — the shippable slice (SLICE A) of #2995 (SABER /
Do-over mutating-step safety). The pre-execution harm-scoring gate already
exists (``evolution/lib/agent_process_bench.py``, #2662) and flags
``destructive-command`` calls; this module delivers the two MISSING pieces:

1. **Snapshot-before-destructive-command primitive** (the Do-over pattern):
   before an autonomous run executes a destructive shell command, snapshot the
   exact files the command will touch so a failed mutation has rollback
   material. ``snapshot_destructive_command`` extracts the touched paths from
   a command string and copies them into a per-run rollback directory, leaving
   a manifest describing what was captured and where the copies live.
2. **Failure-cause mutating counter**: instruments the evolution loop to record
   how often a failed run's root cause was a mutating step, so the share can be
   surfaced in realized-impact metrics (if the SABER distribution holds,
   mutation gating is the highest-leverage safety fix).

Design goals (matching the existing module style, e.g. ``agent_process_bench``):
    * Pure functions + dataclasses; **no side effects on import**.
    * Path *extraction* is pure and unit-testable; file IO is explicit and
      separated from parsing.
    * Standard library only — no external dependencies.
    * Thread-safe shared state via ``threading.Lock``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

__version__ = "1.0.0"

__all__ = [
    "extract_destructive_paths",
    "is_destructive_command",
    "Snapshot",
    "snapshot_destructive_command",
    "MutatingFailureCounter",
]

# Command shapes that destroy or move file content, with the regex group
# capturing the path(s) that are at risk of being lost if the mutation is
# wrong. ``rm``, ``mv`` (source), ``truncate``, ``dd of=`` and ``>``/``>>``
# redirects are the destructive class the pre-execution gate cares about.
_DESTRUCTIVE_RE = re.compile(
    r"\b(rm(?:\s+-[a-zA-Z]*)?\s+)(?P<rm>[^&|;]+)"
    r"|\bmv\s+(?P<mv_src>[^\s]+)\s+"
    r"|\btruncate\s+(?:-[a-zA-Z]+\s+)?[^\s]+\s+(?P<truncate>[^\s]+)"
    r"|\bdd\s+[^|;]*?\bof=(?P<dd>[^\s]+)"
    r"|\b(?:[^&|;]*?)\s*(?:>>|>)\s*(?P<redir>[^\s]+)",
    re.I,
)


def is_destructive_command(command: str) -> bool:
    """Return True if *command* contains a destructive file-mutation operation.

    Mirrors the intent of ``agent_process_bench``'s ``destructive-command`` flag
    but is path-aware: ``rm`` / ``mv`` / ``truncate`` / ``dd of=`` / redirects
    that touch file content. ``rm -rf /`` (the gate's hard block) is caught by
    the pre-execution gate; here we care about the *files* such a command would
    mutate so we can snapshot them.
    """
    if not command:
        return False
    return _DESTRUCTIVE_RE.search(command) is not None


def extract_destructive_paths(command: str, cwd: Optional[str] = None) -> List[str]:
    """Extract the absolute paths a destructive command would touch (pure).

    Returns a de-duplicated list of absolute paths (source of ``mv``, targets
    of ``rm``/``truncate``/``dd of=``/redirects). Relative paths are resolved
    against *cwd* (defaults to the current directory). Pure: performs no IO, so
    it is trivially unit-testable.
    """
    if not command:
        return []
    base = Path(cwd).resolve() if cwd else Path.cwd()
    seen: Dict[str, bool] = {}
    for m in _DESTRUCTIVE_RE.finditer(command):
        for group in ("rm", "mv_src", "truncate", "dd", "redir"):
            raw = m.group(group)
            if not raw:
                continue
            for token in raw.split():
                token = token.strip().strip("'\"").rstrip(",")
                if (
                    not token
                    or token.startswith("-")
                    or token.startswith(("/dev/", "/proc/", "/sys/"))
                ):
                    continue
                p = Path(token)
                ap = str(p if p.is_absolute() else base / p)
                seen[ap] = True
    return list(seen)


@dataclass
class Snapshot:
    """Result of snapshotting the files a destructive command would touch.

    Attributes:
        command: The destructive command string that triggered the snapshot.
        snapshot_dir: Directory where the file copies were written.
        files: Mapping of original absolute path -> copied backup path.
        created: ISO timestamp the snapshot was taken.
    """

    command: str
    snapshot_dir: str
    files: Dict[str, str] = field(default_factory=dict)
    created: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dict (rollback manifest)."""
        return {
            "command": self.command,
            "snapshot_dir": self.snapshot_dir,
            "files": dict(self.files),
            "created": self.created,
        }

    @property
    def rollback_paths(self) -> List[str]:
        """Ordered list of backup copies, for restoring on failure."""
        return list(self.files.values())


def snapshot_destructive_command(
    command: str,
    cwd: Optional[str] = None,
    snapshot_dir: Optional[str] = None,
) -> Optional[Snapshot]:
    """Snapshot files a destructive command would touch before it runs.

    Extracts touched paths from *command* (relative to *cwd*), copies each
    existing file into *snapshot_dir* (default ``<cwd>/.evolution-snapshots``),
    and returns a :class:`Snapshot` manifest. Returns ``None`` if the command is
    not destructive or touches no existing files.

    Args:
        command: The shell command about to execute.
        cwd: Working directory the command runs in (for relative-path resolution).
        snapshot_dir: Optional override for where copies are written.

    Raises:
        OSError: if a copy fails (rollback insurance must never silently drop).
    """
    if not is_destructive_command(command):
        return None
    base = Path(cwd).resolve() if cwd else Path.cwd()
    dest = Path(snapshot_dir) if snapshot_dir else base / ".evolution-snapshots"
    dest.mkdir(parents=True, exist_ok=True)

    from datetime import datetime, timezone

    created = datetime.now(timezone.utc).isoformat()
    snap = Snapshot(command=command, snapshot_dir=str(dest), created=created)
    for i, path in enumerate(extract_destructive_paths(command, cwd=str(base))):
        src = Path(path)
        if not src.is_file():
            continue
        backup = dest / f"{i:03d}_{src.name}"
        shutil.copy2(src, backup)
        snap.files[str(src)] = str(backup)
    return snap if snap.files else None


class MutatingFailureCounter:
    """Counts failed runs whose root cause was a mutating step.

    Writes an append-only JSONL ledger (one record per failed run) and reports
    the share of failed runs whose root cause was classified as a mutating
    step — the metric #2995 wants surfaced in realized-impact metrics. If the
    SABER distribution (mutating steps dominate failure risk) holds on real
    data, a high ``mutating_share`` is evidence that mutation gating is the
    highest-leverage safety fix.

    Ledger line schema: ``{"run_id", "outcome": "failed", "cause_category":
    "mutating"|"other", "recorded_at": "YYYY-MM-DD"}``. Only failed runs are
    recorded; ``record`` no-ops for non-failed outcomes so the counter stays a
    *failure-cause* counter.
    """

    def __init__(self, ledger_file: Optional[os.PathLike] = None) -> None:
        """Initialize the counter with an optional ledger path."""
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
        """Record a failed run's root-cause category (mutating vs other).

        Args:
            run_id: Identifier for the failed run.
            cause_category: ``"mutating"`` if the failure root cause was a
                mutating step, else ``"other"``.
            recorded_at: ISO date; defaults to UTC today.
        """
        if cause_category not in ("mutating", "other"):
            raise ValueError("cause_category must be 'mutating' or 'other'")
        if not recorded_at:
            from datetime import date, timezone, datetime

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

    def summary(self) -> Dict[str, Any]:
        """Aggregate the ledger: failed count, mutating count, and share.

        Returns ``{"failed_runs", "mutating_cause", "other_cause",
        "mutating_share"}`` where ``mutating_share`` is the fraction of failed
        runs whose root cause was a mutating step (``None`` when no failures
        have been recorded). Malformed lines are skipped — the ledger must
        never crash the pipeline.
        """
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
        return {
            "failed_runs": total,
            "mutating_cause": mutating,
            "other_cause": total - mutating,
            "mutating_share": round(mutating / total, 3) if total else None,
        }
