#!/usr/bin/env python3
"""Experience-bank harvest — per-session execution diagnoses (no LLM).

Runs as a ``no_agent`` cron job (deterministic, no model calls).  Scans the
SQLite SessionDB (``hermes_state.py``) for sessions that finished since the
last run and appends ONE :class:`ExperienceEntry` per new session to
``<HERMES_HOME>/evolution/experience/entries.jsonl`` — the per-case layer of
the MemoHarness-inspired experience bank (see ``agent/experience_bank.py``,
which owns the schema).  When at least one entry was appended, distillation
is invoked inline at the end (sibling ``evolution_experience_distill.py``).

Design-review constraints honored here:

* **Strong-signal-only failure verdicts.** ``success=False`` is emitted only
  for failure modes with real persisted evidence (loop-guard hard stop,
  max-iteration exhaustion, unhandled processing error — see the marker
  constants below), always with ``confidence="high"`` and an
  ``outcome_source`` naming the heuristic.  Everything else is
  ``success=None`` (unknown) or a low-confidence clean-completion heuristic.
* **Honest-null dimension attribution.**  Only the unambiguous failure
  categories (network / permission / not_found / syntax_error) get a
  ``primary_dimension``; ambiguous ones (validation / timeout /
  resource_limit) put their candidate dimensions in
  ``secondary_dimensions`` and leave primary ``None``.
* **Controlled-vocabulary analysis.**  The ``analysis`` field is built
  exclusively from tool names (character-scrubbed), FailureCategory value
  strings, and integer counts — raw user/model/tool text is NEVER copied
  into an entry (anti-injection + anti-secret-leak).
* **One cron job + flock.**  A non-blocking ``flock`` guards against
  overlapping runs; distillation rides on this job, not a second one.

Failure detection matches Hermes' own markers: tool-error results are found
with the SAME predicate the live agent loop uses
(``agent.display._detect_tool_failure``), and categories come from
``evolution.lib.root_cause_diagnosis.ErrorClassifier`` — nothing invented
here.

Sessions whose last activity is < 30 minutes old are skipped (possibly
still running — never harvest in-progress sessions).  Dedup between runs
uses the harvest-state cursor (``cursor_ts`` with a 15-minute overlap) plus
a ``seen_ids`` list capped at the most recent 5000 ids.

Exit codes: 0 always for the lock-contention no-op and clean runs; 1 only
for an unexpected top-level failure (the cron scheduler surfaces it).
"""

from __future__ import annotations

import fcntl
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent.display import _detect_tool_failure  # noqa: E402
from agent.experience_bank import (  # noqa: E402
    CATEGORY_TO_DIMENSION,
    ExperienceEntry,
    append_entry,
    get_harvest_state,
    set_harvest_state,
)
from evolution.lib.root_cause_diagnosis import ErrorClassifier  # noqa: E402
from hermes_constants import get_hermes_home  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Sessions with activity newer than this may still be running — skip them.
_ACTIVE_SESSION_GRACE_S = 30 * 60.0

#: Look-behind overlap so a session that straddles the previous cursor is
#: re-examined (dedup happens via seen_ids).
_CURSOR_OVERLAP_S = 15 * 60.0

#: Cap on the dedup id list kept in harvest_state.json.
_SEEN_IDS_CAP = 5000

#: Strong failure markers, mirrored from the agent loop (do not paraphrase —
#: these strings are what actually lands in persisted session messages):
#: * ``agent/conversation_loop.py`` loop-guard hard stops append a final
#:   assistant message starting with ``[loop-guard] ``
#:   (turn_exit_reason loop_guard_{cron,interactive}_hard_stop).
#: * ``agent/conversation_loop.py`` local-processing / repeated-error exits
#:   append a final assistant message starting with the apology prefix
#:   (turn_exit_reason local_processing_error(...) /
#:   error_near_max_iterations(...)).
#: * ``agent/chat_completion_helpers.handle_max_iterations`` appends a
#:   synthetic user message with the max-iterations summary request.
_LOOP_GUARD_PREFIX = "[loop-guard] "
_APOLOGY_PREFIX = "I apologize, but I encountered"
_MAX_ITER_MARKER = "You've reached the maximum number of tool-calling iterations"

#: Categories whose CATEGORY_TO_DIMENSION assignment is unambiguous — only
#: these may set primary_dimension (design-review honest-null rule).
_UNAMBIGUOUS_CATEGORIES = frozenset(
    {"network", "permission", "not_found", "syntax_error"}
)

#: Candidate dimension lists for the ambiguous categories; primary stays None.
_AMBIGUOUS_SECONDARY: Dict[str, List[str]] = {
    "validation": ["tool", "output"],
    "timeout": ["tool", "generation", "orchestration"],
    "resource_limit": ["context", "generation", "tool"],
}

#: Analysis-field vocabulary scrub: anything outside this alphabet in a tool
#: name is folded to "_" so the analysis string can never carry raw text.
_TOOL_NAME_SAFE = set("abcdefghijklmnopqrstuvwxyz0123456789_")


# ---------------------------------------------------------------------------
# Session DB access
# ---------------------------------------------------------------------------

def _default_db_path() -> Path:
    """Resolve the SessionDB path lazily (tests monkeypatch HERMES_HOME)."""
    return get_hermes_home() / "state.db"


def _iter_session_rows(db: Any) -> List[Dict[str, Any]]:
    """Return one metadata row per session, including last message time.

    Uses a single aggregate query instead of list_sessions_rich so child
    (subagent) sessions are included and no compression projection hides
    rows — the harvester wants EVERY session, not a user-facing listing.
    """
    sql = (
        "SELECT s.id, s.source, s.model, s.started_at, s.ended_at, "
        "s.end_reason, s.message_count, s.tool_call_count, "
        "(SELECT MAX(m.timestamp) FROM messages m WHERE m.session_id = s.id) "
        "  AS last_msg_ts "
        "FROM sessions s"
    )
    with db._lock:
        rows = db._conn.execute(sql).fetchall()
    return [dict(r) for r in rows]


def _last_active(row: Dict[str, Any]) -> float:
    """Best-effort last-activity timestamp for a session row."""
    candidates = [
        row.get("started_at") or 0.0,
        row.get("ended_at") or 0.0,
        row.get("last_msg_ts") or 0.0,
    ]
    return float(max(candidates))


# ---------------------------------------------------------------------------
# Per-session analysis
# ---------------------------------------------------------------------------

def _scrub_tool_name(name: Any) -> str:
    """Reduce a tool name to the controlled-vocabulary alphabet."""
    cleaned = "".join(
        ch if ch in _TOOL_NAME_SAFE else "_" for ch in str(name or "").lower()
    )
    return cleaned.strip("_") or "unknown"


def _message_text(msg: Dict[str, Any]) -> str:
    """Return a message's content as a plain string ("" when not textual)."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    return ""


def analyze_session(messages: List[Dict[str, Any]], ended: bool) -> Dict[str, Any]:
    """Deterministically analyze one session's messages.

    Args:
        messages: Active messages in insertion order (SessionDB.get_messages).
        ended: Whether the session row has ``ended_at`` set (a session that
            was never ended and is no longer active counts as interrupted).

    Returns a dict with the verdict fields ready to feed ExperienceEntry:
    ``success``, ``outcome_source``, ``confidence``, ``terminal_reason``,
    ``primary_dimension``, ``secondary_dimensions``, ``failure_category``,
    ``tool``, ``analysis``, ``stats``, plus ``has_unrecovered`` (internal —
    used by the caller's nothing-to-learn skip rule).
    """
    # ── Tool-error tallies, using Hermes' own failure predicate ──
    # An error is UNRECOVERED only when the same tool never succeeds later
    # in the session; recovered errors are normal agent behavior.
    tool_msgs: List[Dict[str, Any]] = [
        m for m in messages if m.get("role") == "tool"
    ]
    last_success_idx: Dict[str, int] = {}
    failures: List[tuple] = []  # (index, tool, category)
    for idx, msg in enumerate(tool_msgs):
        tool = _scrub_tool_name(msg.get("tool_name"))
        content = msg.get("content")
        is_error, _suffix = _detect_tool_failure(
            str(msg.get("tool_name") or ""), content if isinstance(content, str) else None
        )
        if is_error:
            category = ErrorClassifier().classify(
                content if isinstance(content, str) else ""
            )
            failures.append((idx, tool, category.value))
        else:
            last_success_idx[tool] = idx

    unrecovered: Counter = Counter()   # (tool, category) -> count
    recovered_per_tool: Counter = Counter()
    first_seen_order: List[tuple] = []
    for idx, tool, category in failures:
        if idx > last_success_idx.get(tool, -1):
            key = (tool, category)
            if key not in unrecovered:
                first_seen_order.append(key)
            unrecovered[key] += 1
        else:
            recovered_per_tool[tool] += 1

    total_unrecovered = sum(unrecovered.values())
    total_recovered = sum(recovered_per_tool.values())

    # Dominant unrecovered (tool, category): most occurrences, ties broken by
    # first occurrence order for full determinism.
    dominant_tool: Optional[str] = None
    dominant_category: Optional[str] = None
    if unrecovered:
        dominant_key = max(
            first_seen_order,
            key=lambda k: unrecovered[k],
        )
        dominant_tool, dominant_category = dominant_key

    # ── Strong failure signals (persisted, high-confidence markers only) ──
    final_assistant = ""
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            final_assistant = _message_text(msg).strip()
            break
    hit_max_iterations = any(
        m.get("role") == "user" and _MAX_ITER_MARKER in _message_text(m)
        for m in messages
    )

    success: Optional[bool] = None
    outcome_source = ""
    confidence = "low"
    if final_assistant.startswith(_LOOP_GUARD_PREFIX):
        success, confidence = False, "high"
        outcome_source = "heuristic:loop_guard_hard_stop"
        terminal_reason = "loop_guard_hard_stop"
    elif final_assistant.startswith(_APOLOGY_PREFIX):
        success, confidence = False, "high"
        outcome_source = "heuristic:unhandled_exception"
        terminal_reason = "unhandled_exception"
    elif hit_max_iterations:
        success, confidence = False, "high"
        outcome_source = "heuristic:max_iterations_exhausted"
        terminal_reason = "iteration_exhausted"
    else:
        terminal_reason = "completed" if ended else "interrupted"
        if ended and final_assistant and total_unrecovered == 0:
            # Clean completion heuristic: ended session, final assistant
            # reply, zero unrecovered tool errors.
            success = True
            outcome_source = "heuristic:clean_completion"

    # ── Honest-null dimension attribution (dominant unrecovered error) ──
    primary_dimension: Optional[str] = None
    secondary_dimensions: List[str] = []
    if dominant_category in _UNAMBIGUOUS_CATEGORIES:
        primary_dimension = CATEGORY_TO_DIMENSION.get(dominant_category)
    elif dominant_category in _AMBIGUOUS_SECONDARY:
        secondary_dimensions = list(_AMBIGUOUS_SECONDARY[dominant_category])
    # "unknown" (and no unrecovered errors) -> both stay empty/None.

    # ── Controlled-vocabulary analysis field ──
    # Built ONLY from scrubbed tool names, FailureCategory value strings and
    # integer counts — raw user/model/tool text never reaches the entry.
    # tool/category name the DOMINANT unrecovered pair (none when there are
    # no unrecovered errors); the counts are session-wide totals.
    analysis = (
        f"tool={dominant_tool or 'none'} "
        f"category={dominant_category or 'none'} "
        f"unrecovered={total_unrecovered} "
        f"recovered={total_recovered}"
    )

    return {
        "success": success,
        "outcome_source": outcome_source,
        "confidence": confidence,
        "terminal_reason": terminal_reason,
        "primary_dimension": primary_dimension,
        "secondary_dimensions": secondary_dimensions,
        "failure_category": dominant_category,
        "tool": dominant_tool,
        "analysis": analysis,
        "stats": {
            "tool_calls": len(tool_msgs),
            "tool_errors": len(failures),
            "iterations": sum(1 for m in messages if m.get("role") == "assistant"),
            "unrecovered_errors": total_unrecovered,
            "recovered_errors": total_recovered,
        },
        "has_unrecovered": total_unrecovered > 0,
    }


# ---------------------------------------------------------------------------
# Harvest
# ---------------------------------------------------------------------------

def _maybe_distill() -> Optional[Dict[str, Any]]:
    """Invoke distillation inline (sibling script).  Never raises."""
    try:
        from evolution_experience_distill import run_distillation

        result = run_distillation()
        return result if isinstance(result, dict) else {"result": str(result)}
    except Exception as exc:
        print(
            f"[experience-harvest] distillation failed (non-fatal): {exc}",
            file=sys.stderr,
        )
        return None


def run_harvest(
    db_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """Run one harvest pass.  Returns the summary dict main() prints.

    Import-safe and unit-testable: *db_path* / *now* inject the seams;
    distillation is invoked inline when at least one entry was appended.
    """
    from hermes_state import SessionDB

    now = time.time() if now is None else float(now)
    db_path = db_path or _default_db_path()

    summary: Dict[str, Any] = {
        "sessions_scanned": 0,
        "sessions_harvested": 0,
        "entries_appended": 0,
        "distill": None,
    }
    if not db_path.exists():
        return summary

    state = get_harvest_state()
    try:
        cursor_ts = float(state.get("cursor_ts", 0.0) or 0.0)
    except (TypeError, ValueError):
        cursor_ts = 0.0
    seen_ids: List[str] = [
        str(s) for s in (state.get("seen_ids") or []) if isinstance(s, str)
    ]
    seen_set = set(seen_ids)

    db = SessionDB(db_path=db_path, read_only=True)
    try:
        rows = _iter_session_rows(db)

        processed_max_ts = cursor_ts
        new_seen: List[str] = []
        for row in rows:
            summary["sessions_scanned"] += 1
            session_id = str(row.get("id") or "")
            if not session_id:
                continue
            last_active = _last_active(row)
            # Skip possibly-active sessions — never harvest in-progress work.
            if last_active > now - _ACTIVE_SESSION_GRACE_S:
                continue
            # Skip sessions at/before the previous cursor window.
            if last_active <= cursor_ts - _CURSOR_OVERLAP_S:
                continue
            # Skip sessions already harvested.
            if session_id in seen_set:
                continue

            summary["sessions_harvested"] += 1
            processed_max_ts = max(processed_max_ts, last_active)
            new_seen.append(session_id)

            messages = db.get_messages(session_id)
            verdict = analyze_session(messages, ended=row.get("ended_at") is not None)

            # Nothing-to-learn skip: no unrecovered errors AND no verdict
            # either way (success=None).  Clean successes (True) and every
            # failure verdict (False) ARE appended.
            if not verdict["has_unrecovered"] and verdict["success"] is None:
                continue

            entry = ExperienceEntry(
                ts=now,
                session_id=session_id,
                platform=str(row.get("source") or ""),
                model=str(row.get("model") or ""),
                success=verdict["success"],
                outcome_source=verdict["outcome_source"],
                confidence=verdict["confidence"],
                terminal_reason=verdict["terminal_reason"],
                primary_dimension=verdict["primary_dimension"],
                secondary_dimensions=verdict["secondary_dimensions"],
                failure_category=verdict["failure_category"],
                tool=verdict["tool"],
                analysis=verdict["analysis"],
                stats=verdict["stats"],
            )
            append_entry(entry)
            summary["entries_appended"] += 1

        # Persist the dedup cursor: advance to the newest processed session,
        # extend seen_ids and cap to the most recent ids.
        if new_seen:
            merged_seen = seen_ids + [s for s in new_seen if s not in seen_set]
            set_harvest_state(
                {
                    "cursor_ts": processed_max_ts,
                    "seen_ids": merged_seen[-_SEEN_IDS_CAP:],
                }
            )
    finally:
        try:
            db.close()
        except Exception:
            pass

    if summary["entries_appended"] > 0:
        summary["distill"] = _maybe_distill()

    return summary


# ---------------------------------------------------------------------------
# Lock + main
# ---------------------------------------------------------------------------

def _acquire_lock(lock_path: Path):
    """Non-blocking flock.  Returns the open file handle, or None if held."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def main(argv: List[str]) -> int:
    lock_path = get_hermes_home() / "evolution" / "experience" / ".harvest.lock"
    lock_fh = _acquire_lock(lock_path)
    if lock_fh is None:
        print("[experience-harvest] another harvest is running; exiting")
        return 0
    try:
        summary = run_harvest()
    finally:
        try:
            fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fh.close()
    # Deterministic no_agent job: one compact JSON summary line so the run
    # log shows what happened; empty work still prints zeros.
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
