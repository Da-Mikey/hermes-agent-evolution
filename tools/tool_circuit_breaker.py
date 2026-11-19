# -*- coding: utf-8 -*-
"""Retry-spiral circuit breaker and diagnostic fallback for core tool invocations (#3241).

Prevents infinite retry spirals on core tools (read_file, patch, terminal, search_files)
by tracking consecutive failures per session, classifying the failure mode, and
tripping with structured diagnostic guidance once the per-tool budget is exceeded.

Only failures that indicate an unhealthy *tool* spend that budget. A caller-side class
(`not_found`) is a served negative result — the tool answered, the target named in the
call does not exist — so it is recorded but never trips the breaker (#218).
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_TOOL_RETRY_BUDGETS = {
    "read_file": 3,
    "write_file": 3,
    "patch": 3,
    "search_files": 3,
    "terminal": 5,
}

FAILURE_CLASS_NOT_FOUND = "not_found"
FAILURE_CLASS_PERMISSION = "permission_denied"
FAILURE_CLASS_INVALID_ARG = "invalid_argument"
FAILURE_CLASS_TIMEOUT = "timeout"

# #359 — how many recent tool-call ids the producer dedupe remembers per
# (session, tool). A single ``last_call_id`` is not enough: the sequential and
# concurrent dispatch paths record the SAME call from two threads, so the two
# recordings can interleave with a second call's inner recording. A small window
# of ids, each with the set of producers already seen for it, dedupes a call
# however the interleaving falls out.
RECORD_DEDUPE_WINDOW = 8
FAILURE_CLASS_PROVIDER = "provider_error"
FAILURE_CLASS_TRANSIENT = "transient"

# #218 — classes that describe the CALLER'S ARGUMENT rather than the tool's health.
# `not_found` means the tool ran and answered: the target named in the call does not
# exist. That is a negative result, not a malfunction. Counting it toward the
# retry-spiral budget tripped the breaker on a demonstrably healthy tool and told the
# model that a working tool had failed N times in a row — observed live as
# `[CIRCUIT_BREAKER] Tool 'ha_get_state' has failed 3 consecutive times ... Class:
# not_found` after a couple of probes of stale entity ids.
#
# Deliberately NOT included: `invalid_argument`. That bucket also swallows
# `typeerror` / `schema` texts, which are tool-side defects rather than caller
# mistakes, so excluding the whole class would disarm the breaker for real
# malfunctions. It joins this set only once the classifier separates a caller-side
# bad argument from a tool-side bad implementation.
CALLER_ERROR_CLASSES = frozenset({FAILURE_CLASS_NOT_FOUND})


def classify_tool_error(error_text: Optional[str]) -> str:
    """Deterministically classify a tool error string into a standard failure class."""
    if not error_text or not isinstance(error_text, str):
        return FAILURE_CLASS_TRANSIENT
    txt = error_text.lower()
    if any(s in txt for s in ("not found", "no such file", "enoent", "404", "cannot find")):
        return FAILURE_CLASS_NOT_FOUND
    if any(s in txt for s in ("permission denied", "access denied", "eacces", "403", "unauthorized", "operation not permitted")):
        return FAILURE_CLASS_PERMISSION
    if any(s in txt for s in ("invalid argument", "valueerror", "typeerror", "schema", "required argument", "unexpected keyword")):
        return FAILURE_CLASS_INVALID_ARG
    if any(s in txt for s in ("timeout", "timed out", "deadline exceeded", "econnreset")):
        return FAILURE_CLASS_TIMEOUT
    if any(s in txt for s in ("rate limit", "429", "500", "502", "503", "service unavailable", "quota")):
        return FAILURE_CLASS_PROVIDER
    return FAILURE_CLASS_TRANSIENT


def get_strategy_recommendation(tool_name: str, failure_class: str, last_error: Optional[str] = None) -> str:
    """Return targeted diagnostic recommendations for exhausted tool retry budgets."""
    t = (tool_name or "").lower()
    if t == "patch":
        return "Re-read the target file with read_file to inspect current line numbers and exact content before attempting another patch."
    if t == "read_file":
        return "Verify the target path exists and check permissions before reading again."
    if t == "search_files":
        return "Simplify search query or regex, or check the search directory path."
    if t == "terminal":
        return "For long-running or failing commands, verify prerequisites, arguments, or consider running as a background task."
    return "Inspect tool arguments and preconditions before retrying."


class _ToolCircuitBreakerTracker:
    """Thread-safe tracker for tool invocation success rates and retry spirals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, Dict[str, Dict[str, Any]]] = {}
        # #359 — per (session, tool): recent call_id order + which producers have
        # already recorded each id. Kept out of ``_state`` so ``get_session_stats``
        # stays a pure view of the counters.
        self._seen_calls: Dict[Any, Dict[str, Any]] = {}

    def _already_recorded(
        self, sid: str, tname: str, call_id: Optional[str], producer: Optional[str]
    ) -> bool:
        """True when this call was already recorded by a DIFFERENT producer.

        A repeat from the same producer is not deduped — it spends the budget,
        because the same producer recording twice is a real second observation.
        Callers must hold ``self._lock``.
        """
        if not call_id:
            return False
        cid = str(call_id)
        entry = self._seen_calls.get((sid, tname))
        if entry is None:
            entry = {"order": [], "producers": {}}
            self._seen_calls[(sid, tname)] = entry
        order: list = entry["order"]
        producers: Dict[str, set] = entry["producers"]

        seen = producers.get(cid)
        if seen is not None:
            if producer and producer not in seen:
                # Same call, second producer -> one observation, dedupe it.
                producers[cid] = seen | {producer}
                return True
            # Same producer again (or an unattributed repeat): spend the budget.
            if producer:
                producers[cid] = seen | {producer}
            return False

        producers[cid] = {producer} if producer else set()
        order.append(cid)
        while len(order) > RECORD_DEDUPE_WINDOW:
            producers.pop(order.pop(0), None)
        return False

    def record_result(
        self,
        session_id: str,
        tool_name: str,
        status: str,
        error_message: Optional[str] = None,
        budget: Optional[int] = None,
        call_id: Optional[str] = None,
        producer: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Record a tool result and return a diagnostic event if the circuit breaker trips."""
        with self._lock:
            sid = str(session_id or "default").strip()
            tname = str(tool_name or "").strip()
            if not sid or not tname:
                return None

            # #359 — one tool call is recorded by both the dispatcher
            # (model_tools) and the executor. Count it once.
            if self._already_recorded(sid, tname, call_id, producer):
                return None

            sess_map = self._state.setdefault(sid, {})
            tool_rec = sess_map.setdefault(
                tname,
                {
                    "consecutive_failures": 0,
                    "total_calls": 0,
                    "total_failures": 0,
                    "last_error": None,
                    "last_failure_class": None,
                },
            )

            tool_rec["total_calls"] += 1

            if status == "error":
                tool_rec["total_failures"] += 1
                tool_rec["last_error"] = error_message
                fclass = classify_tool_error(error_message)
                tool_rec["last_failure_class"] = fclass

                # #218 — a caller-side error is a served negative result: the call
                # reached the tool and came back with "that target does not exist". It
                # must not spend the tool-health budget, and it clears a stale streak
                # because it proves the tool answered (earlier transport failures are no
                # longer evidence).
                if fclass in CALLER_ERROR_CLASSES:
                    tool_rec["consecutive_failures"] = 0
                    return None

                tool_rec["consecutive_failures"] += 1
                eff_budget = budget or DEFAULT_TOOL_RETRY_BUDGETS.get(tname, 3)
                consecutive = tool_rec["consecutive_failures"]

                if consecutive >= eff_budget:
                    recommendation = get_strategy_recommendation(tname, fclass, error_message)
                    return {
                        "circuit_breaker_tripped": True,
                        "consecutive_failures": consecutive,
                        "budget": eff_budget,
                        "failure_class": fclass,
                        "tool_name": tname,
                        "last_error": error_message,
                        "strategy_recommendation": recommendation,
                    }
                return None
            else:
                tool_rec["consecutive_failures"] = 0
                return None

    def get_session_stats(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            sid = str(session_id or "default").strip()
            return {k: dict(v) for k, v in self._state.get(sid, {}).items()}

    def reset(self, session_id: Optional[str] = None) -> None:
        with self._lock:
            if session_id:
                sid = str(session_id).strip()
                self._state.pop(sid, None)
                for key in [k for k in self._seen_calls if isinstance(k, tuple) and k[0] == sid]:
                    self._seen_calls.pop(key, None)
            else:
                self._state.clear()
                self._seen_calls.clear()


TOOL_CIRCUIT_BREAKER = _ToolCircuitBreakerTracker()
