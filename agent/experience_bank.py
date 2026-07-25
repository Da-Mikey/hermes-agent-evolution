# -*- coding: utf-8 -*-
"""Experience bank — single source of truth for harvested execution experience.

MemoHarness-inspired (arXiv:2607.14159): the agent harness is optimized
across six dimensions — context, tool, generation, orchestration, memory,
output — using a dual-layer experience store:

1. **Per-case diagnoses** (:class:`ExperienceEntry`) — one record per
   harvested session, appended to ``entries.jsonl`` by the harvest cron
   script (``scripts/evolution_experience_harvest.py``).
2. **Distilled global patterns** (:class:`ExperiencePattern`) — aggregated
   guidance rewritten wholesale into ``patterns.json`` by the distill cron
   script (``scripts/evolution_experience_distill.py``) and injected into
   the system prompt at session-construction time.

Storage layout (profile-aware, under ``get_hermes_home()``)::

    <HERMES_HOME>/evolution/experience/
        entries.jsonl        # append-only, one JSON object per line
        patterns.json        # rewritten wholesale by the distiller
        harvest_state.json   # small cursor dict (dedup between harvests)

Design goals (matching ``evolution/lib/root_cause_diagnosis.py``):

    * Pure functions + dataclasses; **no side effects on import**.
    * Full type hints, ``from __future__ import annotations``.
    * JSON serialization (``to_dict`` / ``from_dict``) for every dataclass.
    * **No external dependencies** — standard library only.
    * **Defensive everywhere** — this module is imported on the
      system-prompt build path, so nothing here may raise on missing or
      corrupt state. Reads degrade to empty (skipping corrupt lines with a
      single stderr warning per call); writes log to stderr and swallow
      OSError.

The bank deliberately does **not** import ``evolution/`` code: the runtime
prompt path must never pull in the evolution pipeline.  The
:data:`CATEGORY_TO_DIMENSION` mapping is therefore keyed by the
``FailureCategory`` enum's ``.value`` strings; a test asserts the mapping
stays complete against the enum.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from hermes_constants import get_hermes_home

__version__ = "1.1.0"

__all__ = [
    "HARNESS_DIMENSIONS",
    "CATEGORY_TO_DIMENSION",
    "CONFIDENCE_LEVELS",
    "ExperienceEntry",
    "ExperiencePattern",
    "pattern_id",
    "entries_path",
    "patterns_path",
    "harvest_state_path",
    "append_entry",
    "iter_entries",
    "load_patterns",
    "save_patterns",
    "format_patterns_prompt",
    "get_harvest_state",
    "set_harvest_state",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The six harness dimensions from MemoHarness.
HARNESS_DIMENSIONS: tuple = (
    "context",
    "tool",
    "generation",
    "orchestration",
    "memory",
    "output",
)

#: Mapping from ``FailureCategory.value`` strings (see
#: ``evolution/lib/root_cause_diagnosis.py``) to the harness dimension that
#: best explains the failure, or ``None`` when no dimension applies.
#: Keyed by value strings — NOT by the enum — so this module stays free of
#: any ``evolution/`` import.  The exact assignments are a judgment call and
#: may be refined; keep this a plain dict at module top.
CATEGORY_TO_DIMENSION: Dict[str, Optional[str]] = {
    "network": "tool",
    "permission": "tool",
    "not_found": "context",
    "validation": "tool",
    "timeout": "orchestration",
    "resource_limit": "generation",
    "syntax_error": "output",
    "unknown": None,
}

#: Allowed values for :attr:`ExperienceEntry.confidence`.
CONFIDENCE_LEVELS: tuple = ("high", "low")

_SECONDS_PER_DAY = 86400.0


# ---------------------------------------------------------------------------
# Paths (resolved lazily — tests monkeypatch HERMES_HOME)
# ---------------------------------------------------------------------------

def _experience_dir() -> Path:
    """Return the experience-bank directory (not created here)."""
    return get_hermes_home() / "evolution" / "experience"


def entries_path() -> Path:
    """Path of the append-only per-session diagnosis log."""
    return _experience_dir() / "entries.jsonl"


def patterns_path() -> Path:
    """Path of the distilled global-pattern store."""
    return _experience_dir() / "patterns.json"


def harvest_state_path() -> Path:
    """Path of the harvest dedup-cursor dict."""
    return _experience_dir() / "harvest_state.json"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

def _validate_dimension(dim: Optional[str], field_name: str) -> Optional[str]:
    """Validate a single dimension value at construction time."""
    if dim is not None and dim not in HARNESS_DIMENSIONS:
        raise ValueError(
            f"{field_name} must be one of {HARNESS_DIMENSIONS} or None, "
            f"got {dim!r}"
        )
    return dim


def _coerce_dimension(dim: Any) -> Optional[str]:
    """Coerce a stored dimension value for deserialization.

    Forward-compat: old files may contain dimensions a newer schema dropped
    (or vice versa).  Unknown values become ``None`` rather than raising —
    stored data must never crash new code.
    """
    if isinstance(dim, str) and dim in HARNESS_DIMENSIONS:
        return dim
    return None


@dataclass
class ExperienceEntry:
    """One harvested per-session diagnosis.

    Attributes:
        v: Schema version (currently 1).  ``from_dict`` defaults to 1 when
            the key is absent, so v0 lines written before versioning still
            deserialize.
        ts: Epoch seconds when the entry was recorded.
        session_id: Source session identifier.
        platform: Originating platform (``"cli"``, ``"telegram"``, ...).
        model: Model identifier used by the session.
        success: Tri-state outcome — ``True`` / ``False`` / ``None`` for
            unknown (we have no ground-truth labels).
        outcome_source: Which heuristic produced the *success* verdict,
            e.g. ``"heuristic:unhandled_exception"``; empty when none.
        confidence: ``"high"`` or ``"low"`` — how much trust to place in
            the heuristic verdict.  Validated at construction;
            ``from_dict`` coerces unknown values to ``"low"``.
        terminal_reason: Why the session ended, e.g. ``"completed"``,
            ``"interrupted"``, ``"iteration_exhausted"``,
            ``"unhandled_exception"``.
        primary_dimension: Main harness dimension implicated, or ``None``.
        secondary_dimensions: Other implicated dimensions.
        failure_category: ``FailureCategory.value`` string, or ``None``.
        tool: Dominant failing tool name when known, else ``None``.
        analysis: Free-text diagnosis summary.
        stats: Session counters, e.g. ``tool_calls``, ``tool_errors``,
            ``iterations``.
    """

    v: int = 1
    ts: float = 0.0
    session_id: str = ""
    platform: str = ""
    model: str = ""
    success: Optional[bool] = None
    outcome_source: str = ""
    confidence: str = "low"
    terminal_reason: str = ""
    primary_dimension: Optional[str] = None
    secondary_dimensions: List[str] = field(default_factory=list)
    failure_category: Optional[str] = None
    tool: Optional[str] = None
    analysis: str = ""
    stats: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_dimension(self.primary_dimension, "primary_dimension")
        for dim in self.secondary_dimensions:
            _validate_dimension(dim, "secondary_dimensions")
        if self.success is not None and not isinstance(self.success, bool):
            raise ValueError(
                f"success must be True, False, or None, got {self.success!r}"
            )
        if self.confidence not in CONFIDENCE_LEVELS:
            raise ValueError(
                f"confidence must be one of {CONFIDENCE_LEVELS}, "
                f"got {self.confidence!r}"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "v": self.v,
            "ts": self.ts,
            "session_id": self.session_id,
            "platform": self.platform,
            "model": self.model,
            "success": self.success,
            "outcome_source": self.outcome_source,
            "confidence": self.confidence,
            "terminal_reason": self.terminal_reason,
            "primary_dimension": self.primary_dimension,
            "secondary_dimensions": list(self.secondary_dimensions),
            "failure_category": self.failure_category,
            "tool": self.tool,
            "analysis": self.analysis,
            "stats": dict(self.stats),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExperienceEntry":
        """Deserialize from a dictionary produced by :meth:`to_dict`.

        Tolerant: unknown dimension strings are coerced to ``None``, unknown
        confidence values to ``"low"``, and fields added after v0
        (``v`` / ``outcome_source`` / ``confidence`` / ``terminal_reason`` /
        ``tool``) default safely when absent — old files never crash new
        code.  Because every field is sanitized before construction, the
        strict ``__post_init__`` validation always passes.
        """
        primary = _coerce_dimension(d.get("primary_dimension"))
        secondary_raw = d.get("secondary_dimensions") or []
        if not isinstance(secondary_raw, list):
            secondary_raw = []
        secondary = [
            coerced
            for coerced in (_coerce_dimension(x) for x in secondary_raw)
            if coerced is not None
        ]
        success = d.get("success")
        if success is not None and not isinstance(success, bool):
            success = None
        confidence = d.get("confidence")
        if confidence not in CONFIDENCE_LEVELS:
            confidence = "low"
        tool = d.get("tool")
        stats = d.get("stats")
        try:
            v = int(d.get("v", 1) or 1)
        except (TypeError, ValueError):
            v = 1
        return cls(
            v=v,
            ts=float(d.get("ts", 0.0) or 0.0),
            session_id=str(d.get("session_id", "")),
            platform=str(d.get("platform", "")),
            model=str(d.get("model", "")),
            success=success,
            outcome_source=str(d.get("outcome_source", "") or ""),
            confidence=confidence,
            terminal_reason=str(d.get("terminal_reason", "") or ""),
            primary_dimension=primary,
            secondary_dimensions=secondary,
            failure_category=(
                str(d["failure_category"]) if d.get("failure_category") else None
            ),
            tool=str(tool) if tool else None,
            analysis=str(d.get("analysis", "")),
            stats=dict(stats) if isinstance(stats, dict) else {},
        )


def pattern_id(
    dimension: str,
    category: str,
    tool: Optional[str] = None,
) -> str:
    """Build the stable pattern slug — SSoT for the id format.

    ``"<dimension>-<category>"`` normally, or
    ``"<dimension>-<category>-<tool>"`` when *tool* is set.  Both the
    distiller and tests must use this helper instead of hand-rolling slugs.
    """
    base = f"{dimension}-{category}"
    return f"{base}-{tool}" if tool else base


@dataclass
class ExperiencePattern:
    """A distilled global pattern with deterministic guidance.

    Attributes:
        v: Schema version (currently 1); ``from_dict`` defaults to 1 when
            absent.
        id: Stable slug — see :func:`pattern_id` (``"<dimension>-<category>"``,
            plus ``"-<tool>"`` when *tool* is set).
        dimension: Harness dimension this pattern belongs to.
        category: ``FailureCategory.value`` string the pattern clusters on.
        tool: Dominant failing tool this pattern is scoped to, else ``None``.
        trigger: When this pattern fires.
        guidance: What the agent should do about it.
        evidence_count: Number of supporting entries.
        first_seen: Epoch seconds of the oldest supporting entry.
        last_seen: Epoch seconds of the newest supporting entry.
    """

    v: int = 1
    id: str = ""
    dimension: str = "tool"
    category: str = "unknown"
    tool: Optional[str] = None
    trigger: str = ""
    guidance: str = ""
    evidence_count: int = 0
    first_seen: float = 0.0
    last_seen: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a JSON-compatible dictionary."""
        return {
            "v": self.v,
            "id": self.id,
            "dimension": self.dimension,
            "category": self.category,
            "tool": self.tool,
            "trigger": self.trigger,
            "guidance": self.guidance,
            "evidence_count": self.evidence_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExperiencePattern":
        """Deserialize from a dictionary produced by :meth:`to_dict`.

        Tolerant of malformed field types (coerces to safe defaults) and of
        v0 dicts missing the ``v`` / ``tool`` keys.
        """
        def _s(key: str) -> str:
            return str(d.get(key, "") or "")

        def _f(key: str) -> float:
            try:
                return float(d.get(key, 0.0) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        def _i(key: str, default: int = 0) -> int:
            try:
                return int(d.get(key, default) or default)
            except (TypeError, ValueError):
                return default

        tool = d.get("tool")
        return cls(
            v=_i("v", 1),
            id=_s("id"),
            dimension=_s("dimension") or "tool",
            category=_s("category") or "unknown",
            tool=str(tool) if tool else None,
            trigger=_s("trigger"),
            guidance=_s("guidance"),
            evidence_count=_i("evidence_count"),
            first_seen=_f("first_seen"),
            last_seen=_f("last_seen"),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _warn(message: str) -> None:
    """Log a non-fatal storage problem to stderr.  Never raises."""
    try:
        print(f"[experience_bank] {message}", file=sys.stderr)
    except Exception:  # pragma: no cover - stderr itself is broken
        pass


def _atomic_write_json(path: Path, payload: Any) -> None:
    """Write *payload* as JSON to *path* atomically (tmp file + os.replace).

    Never raises on OSError — logs to stderr and swallows, since callers may
    run inside prompt-critical paths' ecosystem.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.write("\n")
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        _warn(f"failed to write {path}: {exc}")


# ---------------------------------------------------------------------------
# Entries (append-only per-session diagnoses)
# ---------------------------------------------------------------------------

def append_entry(entry: ExperienceEntry) -> None:
    """Append one entry as a JSON line to ``entries.jsonl``.

    The directory is created on demand.  A single small ``write()`` of one
    line is atomic enough on POSIX for our single-writer cron harvester; if
    concurrent writers ever appear, revisit with a lock file.

    Never raises on OSError — logs to stderr and swallows.
    """
    path = entries_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry.to_dict(), separators=(",", ":"))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        _warn(f"failed to append entry to {path}: {exc}")


def iter_entries(since_ts: Optional[float] = None) -> Iterator[ExperienceEntry]:
    """Yield entries from ``entries.jsonl``, oldest first.

    Tolerant: returns an empty iterator when the file is missing, and skips
    blank or corrupt lines.  Corrupt-line tolerance is not silent — when any
    line is skipped, ONE summary warning is printed to stderr per call.
    When *since_ts* is given, only entries with ``ts >= since_ts`` are
    yielded.
    """
    path = entries_path()
    try:
        fh = open(path, "r", encoding="utf-8")
    except OSError:
        return

    skipped = 0
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(data, dict):
                skipped += 1
                continue
            try:
                entry = ExperienceEntry.from_dict(data)
            except (TypeError, ValueError, AttributeError):
                skipped += 1
                continue
            if since_ts is not None and entry.ts < since_ts:
                continue
            yield entry
    if skipped:
        noun = "entry" if skipped == 1 else "entries"
        _warn(f"skipped {skipped} corrupt {noun} in {path}")


# ---------------------------------------------------------------------------
# Patterns (distilled global guidance)
# ---------------------------------------------------------------------------

def load_patterns(
    max_age_days: Optional[float] = None,
) -> List[ExperiencePattern]:
    """Load distilled patterns from ``patterns.json``.

    Tolerant: returns ``[]`` when the file is missing or corrupt.  Corrupt
    tolerance is not silent — when the file exists but is unparseable, ONE
    warning is printed to stderr.  When *max_age_days* is given, patterns
    whose ``last_seen`` is older than that many days (relative to now) are
    filtered out.
    """
    path = patterns_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except OSError:
        return []  # missing/unreadable — nothing to warn about
    except ValueError:
        _warn(f"could not parse patterns file {path}; treating as empty")
        return []

    if isinstance(raw, dict):
        raw = raw.get("patterns", [])
    if not isinstance(raw, list):
        return []

    patterns: List[ExperiencePattern] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            patterns.append(ExperiencePattern.from_dict(item))
        except (TypeError, ValueError, AttributeError):
            continue

    if max_age_days is not None:
        cutoff = time.time() - max_age_days * _SECONDS_PER_DAY
        patterns = [p for p in patterns if p.last_seen >= cutoff]

    return patterns


def save_patterns(patterns: Sequence[ExperiencePattern]) -> None:
    """Rewrite ``patterns.json`` with the given patterns (atomic write).

    Never raises on OSError — logs to stderr and swallows.
    """
    payload = [p.to_dict() for p in patterns]
    _atomic_write_json(patterns_path(), payload)


def format_patterns_prompt(
    patterns: Optional[Sequence[ExperiencePattern]] = None,
    *,
    max_patterns: int = 5,
    max_chars: int = 1200,
) -> str:
    """Render the system-prompt block for distilled patterns.

    When *patterns* is ``None``, loads them from disk.  Returns ``""`` when
    there is nothing to show.  Otherwise renders a compact block starting
    with the exact heading line ``## Learned execution patterns`` followed
    by one line per pattern::

        - [<dimension>] <guidance> (evidence: <n>)

    Ordering is fully deterministic: evidence_count descending, then
    last_seen descending, then id ascending as the final tie-breaker.  At
    most *max_patterns* lines are emitted, and the whole block is truncated
    to *max_chars* on a line boundary — the trailing partial line is
    dropped, never cut mid-line.
    """
    if patterns is None:
        patterns = load_patterns()
    if not patterns:
        return ""

    ordered = sorted(
        patterns,
        key=lambda p: (-p.evidence_count, -p.last_seen, p.id),
    )[: max(0, max_patterns)]
    if not ordered:
        return ""

    lines = ["## Learned execution patterns"]
    for p in ordered:
        lines.append(f"- [{p.dimension}] {p.guidance} (evidence: {p.evidence_count})")

    block = "\n".join(lines)
    if len(block) > max_chars:
        kept: List[str] = []
        total = 0
        for line in block.split("\n"):
            # +1 accounts for the newline joining each kept line.
            if total + len(line) + (1 if kept else 0) > max_chars:
                break
            kept.append(line)
            total += len(line) + (1 if len(kept) > 1 else 0)
        # Never emit a heading with zero pattern lines, and never a
        # mid-line cut: anything dropped was dropped whole.
        if len(kept) <= 1:
            return ""
        block = "\n".join(kept)
    return block


# ---------------------------------------------------------------------------
# Harvest state (dedup cursor)
# ---------------------------------------------------------------------------

def get_harvest_state() -> Dict[str, Any]:
    """Read the harvest dedup cursor.  Returns ``{}`` on missing/corrupt."""
    path = harvest_state_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def set_harvest_state(state: Dict[str, Any]) -> None:
    """Persist the harvest dedup cursor (atomic write).  Never raises."""
    _atomic_write_json(harvest_state_path(), dict(state))
