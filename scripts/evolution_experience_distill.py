#!/usr/bin/env python3
"""Experience-bank distillation — cluster harvested failures into global patterns.

Runs as a ``no_agent`` cron job (no LLM, fully deterministic), invoked inline by
the harvest job and callable directly.  Reads the per-session diagnoses the
harvester appended to ``entries.jsonl`` (see ``agent/experience_bank.py`` —
the single source of truth for the schema and storage layout) and rewrites
``patterns.json`` with the distilled, prompt-injectable guidance patterns.

Pipeline:

1. **Evidence selection** — only entries with ``success is False`` AND
   ``confidence == "high"`` AND ``ts`` inside the 90-day evidence window
   count as failure evidence.  Low-confidence heuristics and unknown
   outcomes (``success is None``) never cluster.
2. **Honest null** — entries with ``primary_dimension is None`` or
   ``failure_category is None`` are counted as ``unattributed`` and never
   become patterns.
3. **Clustering** — group evidence by ``(primary_dimension,
   failure_category, tool)``.  A cluster becomes a pattern only with
   >= 3 entries from >= 3 DISTINCT sessions (evidence diversity: one broken
   cron job repeating must not mint a "global" pattern).
4. **Guidance templates** — only ``(dimension, category)`` pairs with a
   specific, action-oriented template in :data:`GUIDANCE_TEMPLATES` produce
   patterns; anything else is counted ``no_template``.  Never fall back to
   generic advice — that is prompt noise.
5. **Merge + expiry** — matching patterns keep ``first_seen`` and get
   refreshed ``last_seen`` / ``evidence_count`` / guidance; patterns with no
   supporting evidence newer than 30 days are dropped.
6. **Compaction** — ``entries.jsonl`` is atomically rewritten keeping only
   entries inside the 90-day window, bounding unbounded growth.

Concurrency: a non-blocking ``fcntl.flock`` on ``.distill.lock`` next to the
bank serializes overlapping cron runs; a locked run returns immediately with
``{"skipped": "locked"}`` and writes nothing.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agent.experience_bank import (
    ExperienceEntry,
    ExperiencePattern,
    entries_path,
    iter_entries,
    load_patterns,
    pattern_id,
    save_patterns,
)
from hermes_constants import get_hermes_home

#: How far back failure evidence may reach, relative to ``now``.
EVIDENCE_WINDOW_DAYS = 90.0

#: A pattern with no supporting evidence newer than this is dropped.
STALE_PATTERN_DAYS = 30.0

#: A cluster needs at least this many entries...
MIN_EVIDENCE_ENTRIES = 3

#: ...spread across at least this many DISTINCT session ids.
MIN_DISTINCT_SESSIONS = 3

_SECONDS_PER_DAY = 86400.0

#: Deterministic (trigger, guidance) per (dimension, category).
#:
#: Only the unambiguous attributions get templates: network -> tool,
#: permission -> tool, not_found -> context, syntax_error -> output.  Of the
#: ambiguous categories, only (tool, timeout) is sound when the entry names a
#: concrete failing tool — everything else (validation, resource_limit,
#: orchestration-level timeouts, ...) is deliberately skipped: no pattern is
#: better than a generic one.  Templates containing ``{tool}`` require a
#: known tool; a tool-less cluster against such a template is ``no_template``.
GUIDANCE_TEMPLATES: Dict[Tuple[str, str], Tuple[str, str]] = {
    ("tool", "network"): (
        "`{tool}` fails with network errors (DNS, connection refused, unreachable host)",
        "When `{tool}` fails with a network error, probe connectivity once with a "
        "minimal request before retrying; if the endpoint is down, switch to an "
        "alternative source or report the outage instead of retrying the same call.",
    ),
    ("tool", "permission"): (
        "`{tool}` fails with permission / 403 / access-denied errors",
        "When `{tool}` returns a permission error, do not retry the same call — "
        "identify the missing credential or scope (API key, token, file mode), fix "
        "or ask the user for it, then retry once with the corrected access.",
    ),
    ("tool", "timeout"): (
        "`{tool}` calls time out",
        "When `{tool}` calls time out, reduce the command's scope (narrower search, "
        "smaller batch, explicit timeout flag) instead of retrying the same invocation.",
    ),
    ("context", "not_found"): (
        "A referenced file path or resource does not exist",
        "Before referencing a path or resource, verify it exists (list the parent "
        "directory or search for it first); when something is not found, re-locate "
        "the current target instead of retrying the same path.",
    ),
    ("output", "syntax_error"): (
        "Generated output fails to parse (syntax error, malformed JSON/code)",
        "When output fails to parse, validate structure before emitting — run a "
        "syntax check on code, serialize JSON/YAML with a library rather than "
        "hand-writing it — and regenerate the malformed fragment instead of "
        "appending to it.",
    ),
}


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------

def _lock_path() -> Path:
    """Path of the distillation lock file, next to the bank files."""
    return get_hermes_home() / "evolution" / "experience" / ".distill.lock"


def _acquire_lock() -> Optional[int]:
    """Take a non-blocking exclusive flock; return the fd, or None if held.

    The caller must keep the fd open for the whole run and ``os.close`` it at
    the end (closing releases the lock).
    """
    path = _lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


# ---------------------------------------------------------------------------
# Entries compaction (bounds entries.jsonl growth)
# ---------------------------------------------------------------------------

def _compact_entries(entries: List[ExperienceEntry], cutoff_ts: float) -> None:
    """Atomically rewrite ``entries.jsonl`` keeping entries at/after *cutoff_ts*.

    Reads come from the already-parsed *entries* list (via ``iter_entries``),
    re-serialized through ``ExperienceEntry.to_dict`` with the same compact
    separators ``append_entry`` uses, so an idempotent re-run is
    byte-identical.  Missing file: nothing to do.  OSError: warn to stderr
    and keep going — a failed compaction must not kill the cron job.
    """
    path = entries_path()
    if not path.exists():
        return
    kept = [e for e in entries if e.ts >= cutoff_ts]
    try:
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for entry in kept:
                    fh.write(
                        json.dumps(entry.to_dict(), separators=(",", ":")) + "\n"
                    )
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        print(f"[evolution-distill] failed to compact {path}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Core distillation
# ---------------------------------------------------------------------------

def _empty_stats() -> Dict[str, Any]:
    return {
        "entries_seen": 0,
        "evidence_entries": 0,
        "clusters": 0,
        "patterns_written": 0,
        "patterns_dropped": 0,
        "unattributed": 0,
        "no_template": 0,
        "entries_pruned": 0,
    }


def _fill(template: str, tool: Optional[str]) -> Optional[str]:
    """Fill the ``{tool}`` placeholder; None when a tool is required but unknown."""
    if "{tool}" in template:
        if not tool:
            return None
        return template.replace("{tool}", tool)
    return template


def _distill(now: float, stats: Dict[str, Any]) -> Dict[str, Any]:
    evidence_cutoff = now - EVIDENCE_WINDOW_DAYS * _SECONDS_PER_DAY
    stale_cutoff = now - STALE_PATTERN_DAYS * _SECONDS_PER_DAY

    entries = list(iter_entries())
    stats["entries_seen"] = len(entries)

    # 1. Evidence selection: only high-confidence failures inside the window.
    evidence = [
        e
        for e in entries
        if e.success is False and e.confidence == "high" and e.ts >= evidence_cutoff
    ]
    stats["evidence_entries"] = len(evidence)

    # 2. Honest null: unattributed evidence never clusters.
    attributable: List[ExperienceEntry] = []
    for e in evidence:
        if e.primary_dimension is None or e.failure_category is None:
            stats["unattributed"] += 1
        else:
            attributable.append(e)

    # 3. Cluster by (dimension, category, tool); tool may be None.
    groups: Dict[Tuple[str, str, Optional[str]], List[ExperienceEntry]] = {}
    for e in attributable:
        key = (e.primary_dimension or "", e.failure_category or "", e.tool)
        groups.setdefault(key, []).append(e)
    stats["clusters"] = len(groups)

    existing = {p.id: p for p in load_patterns()}
    merged: Dict[str, ExperiencePattern] = {}

    # 4. Threshold + template gate; deterministic iteration order.
    for (dimension, category, tool) in sorted(groups, key=lambda k: (k[0], k[1], k[2] or "")):
        members = groups[(dimension, category, tool)]
        distinct_sessions = {m.session_id for m in members}
        if (
            len(members) < MIN_EVIDENCE_ENTRIES
            or len(distinct_sessions) < MIN_DISTINCT_SESSIONS
        ):
            continue
        template = GUIDANCE_TEMPLATES.get((dimension, category))
        if template is None:
            stats["no_template"] += 1
            continue
        trigger = _fill(template[0], tool)
        guidance = _fill(template[1], tool)
        if trigger is None or guidance is None:
            # Template is tool-scoped but this cluster has no known tool.
            stats["no_template"] += 1
            continue

        pid = pattern_id(dimension, category, tool)
        first_seen = min(m.ts for m in members)
        last_seen = max(m.ts for m in members)
        prior = existing.get(pid)
        merged[pid] = ExperiencePattern(
            id=pid,
            dimension=dimension,
            category=category,
            tool=tool,
            trigger=trigger,
            guidance=guidance,
            evidence_count=len(members),
            # 5a. Merge: first_seen survives across runs.
            first_seen=prior.first_seen if prior is not None else first_seen,
            last_seen=last_seen,
        )

    # 5b. Keep still-fresh untouched patterns; drop stale ones.
    for pid, pattern in existing.items():
        if pid in merged:
            continue
        if pattern.last_seen >= stale_cutoff:
            merged[pid] = pattern
        else:
            stats["patterns_dropped"] += 1

    # Stable-diff friendly file: sort by pattern id.
    final = [merged[pid] for pid in sorted(merged)]
    save_patterns(final)
    stats["patterns_written"] = len(final)

    # 6. Compact the entries log inside the evidence window.
    _compact_entries(entries, evidence_cutoff)
    stats["entries_pruned"] = stats["entries_seen"] - sum(
        1 for e in entries if e.ts >= evidence_cutoff
    )

    return stats


def run_distillation(now: float | None = None) -> dict:
    """Cluster failure entries into patterns. Returns stats dict.

    *now* is injectable for tests (defaults to ``time.time()``).  When another
    distiller holds the lock, returns immediately with ``"skipped": "locked"``
    in the stats and writes nothing.
    """
    if now is None:
        now = time.time()
    stats = _empty_stats()
    lock_fd = _acquire_lock()
    if lock_fd is None:
        stats["skipped"] = "locked"
        return stats
    try:
        return _distill(now, stats)
    finally:
        os.close(lock_fd)


def main(argv: Optional[List[str]] = None) -> int:
    """Cron entry point: run once, print the stats as one JSON line."""
    stats = run_distillation()
    print(json.dumps(stats, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
