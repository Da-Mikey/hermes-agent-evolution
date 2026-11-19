"""#359 — one tool call is ONE observation, however the two producers interleave.

Every tool call is recorded by two producers:

* ``model_tools.handle_function_call`` (``producer="model_tools"``), and
* ``agent.tool_executor._record_tool_circuit_breaker`` (``producer="tool_executor"``).

In the concurrent path (``_dispatch_worker`` on a ``DaemonThreadPoolExecutor``)
they record from separate threads, so a single ``last_call_id`` cannot dedupe
them: the recordings of three calls can arrive as
``c1/mt, c2/mt, c3/mt, c1/te, c2/te, c3/te`` — interleaved, not paired. The
tracker therefore keeps a bounded ``call_id -> set(producers)`` window per
(session, tool). A call already recorded by a *different* producer is deduped;
a repeat from the *same* producer still spends the budget.
"""

from __future__ import annotations

import json

import pytest

from tools.tool_circuit_breaker import (
    RECORD_DEDUPE_WINDOW,
    _ToolCircuitBreakerTracker,
)

_COUNTER_KEYS = {
    "consecutive_failures",
    "total_calls",
    "total_failures",
    "last_error",
    "last_failure_class",
}


@pytest.fixture()
def tracker():
    t = _ToolCircuitBreakerTracker()
    yield t
    t.reset()


def _record(
    tracker: _ToolCircuitBreakerTracker,
    call_id,
    producer,
    status: str = "error",
    tool: str = "terminal",
    sid: str = "s1",
):
    return tracker.record_result(
        session_id=sid,
        tool_name=tool,
        status=status,
        error_message="boom" if status == "error" else None,
        call_id=call_id,
        producer=producer,
    )


def _counters(tracker: _ToolCircuitBreakerTracker, sid: str = "s1", tool: str = "terminal"):
    return tracker.get_session_stats(sid)[tool]


def test_interleaved_producers_count_one_observation_per_call(tracker):
    """The ordering measured on the closed PR: 3 real failures must read 3, not 6."""
    for cid in ("c1", "c2", "c3"):
        _record(tracker, cid, "model_tools")
    for cid in ("c1", "c2", "c3"):
        _record(tracker, cid, "tool_executor")

    stats = _counters(tracker)
    assert stats["consecutive_failures"] == 3
    assert stats["total_calls"] == 3
    assert stats["total_failures"] == 3


def test_paired_producers_count_once_per_call(tracker):
    """The sequential path interleaves per call: mt, te, mt, te, ..."""
    for cid in ("c1", "c2", "c3"):
        _record(tracker, cid, "model_tools")
        _record(tracker, cid, "tool_executor")

    stats = _counters(tracker)
    assert stats["consecutive_failures"] == 3
    assert stats["total_calls"] == 3


def test_same_producer_repeat_still_spends_budget(tracker):
    """Dedupe is cross-producer only: a genuine second record must count."""
    _record(tracker, "c1", "model_tools")
    _record(tracker, "c1", "model_tools")

    assert _counters(tracker)["consecutive_failures"] == 2


def test_third_producer_on_a_known_call_is_also_deduped(tracker):
    _record(tracker, "c1", "model_tools")
    _record(tracker, "c1", "tool_executor")
    _record(tracker, "c1", "some_future_producer")

    assert _counters(tracker)["consecutive_failures"] == 1


def test_missing_call_id_is_never_deduped(tracker):
    """Callers that cannot supply an id keep the old, undeduped behaviour."""
    _record(tracker, None, "model_tools")
    _record(tracker, None, "tool_executor")

    assert _counters(tracker)["consecutive_failures"] == 2


def test_unattributed_first_record_still_dedupes_a_later_producer(tracker):
    """An early record with no producer cannot be distinguished, so it is kept
    as the observation and the attributed repeat is deduped."""
    _record(tracker, "c1", None)
    _record(tracker, "c1", "tool_executor")

    assert _counters(tracker)["consecutive_failures"] == 1


def test_two_unattributed_records_both_count(tracker):
    """With no producer on either side there is nothing to compare — no dedupe."""
    _record(tracker, "c1", None)
    _record(tracker, "c1", None)

    assert _counters(tracker)["consecutive_failures"] == 2


def test_dedupe_window_is_bounded(tracker):
    """Only the last RECORD_DEDUPE_WINDOW ids are remembered, and no more."""
    for i in range(RECORD_DEDUPE_WINDOW):
        _record(tracker, f"c{i}", "model_tools")
    _record(tracker, "overflow", "model_tools")

    # c0 has been evicted: its second producer is a fresh observation again.
    before = _counters(tracker)["consecutive_failures"]
    _record(tracker, "c0", "tool_executor")
    assert _counters(tracker)["consecutive_failures"] == before + 1

    # An id inside the window is still deduped.
    inside = _counters(tracker)["consecutive_failures"]
    _record(tracker, "overflow", "tool_executor")
    assert _counters(tracker)["consecutive_failures"] == inside


def test_window_state_does_not_leak_into_session_stats(tracker):
    _record(tracker, "c1", "model_tools")
    assert set(_counters(tracker)) == _COUNTER_KEYS


def test_reset_clears_the_dedupe_window(tracker):
    _record(tracker, "c1", "model_tools")
    tracker.reset("s1")
    _record(tracker, "c1", "tool_executor")

    assert _counters(tracker)["consecutive_failures"] == 1


def test_executor_records_its_producer_and_call_id(monkeypatch):
    """Wiring guard: drop the kwarg and the dedupe silently stops working."""
    from agent.tool_executor import _record_tool_circuit_breaker

    captured = {}

    class _FakeTracker:
        def record_result(self, **kwargs):
            captured.update(kwargs)
            return None

    monkeypatch.setattr("tools.tool_circuit_breaker.TOOL_CIRCUIT_BREAKER", _FakeTracker())

    class _Agent:
        session_id = "s1"

    _record_tool_circuit_breaker(_Agent(), "terminal", "boom", True, "t1", call_id="call-9")

    assert captured["call_id"] == "call-9"
    assert captured["producer"] == "tool_executor"


def test_model_tools_records_its_producer_and_call_id(monkeypatch):
    import model_tools

    captured = {}

    class _FakeTracker:
        def record_result(self, **kwargs):
            captured.update(kwargs)
            return None

    monkeypatch.setattr("tools.tool_circuit_breaker.TOOL_CIRCUIT_BREAKER", _FakeTracker())
    monkeypatch.setattr(
        "model_tools.registry.dispatch", lambda *a, **kw: json.dumps({"ok": True})
    )

    model_tools.handle_function_call(
        "read_file",
        {"path": "nowhere.txt"},
        task_id="t1",
        tool_call_id="call-7",
        session_id="s1",
    )

    assert captured["call_id"] == "call-7"
    assert captured["producer"] == "model_tools"


def test_end_to_end_one_call_costs_one_unit(monkeypatch):
    """Both real producers, one real call: the budget moves by exactly one."""
    import model_tools
    from agent.tool_executor import _record_tool_circuit_breaker

    tracker = _ToolCircuitBreakerTracker()
    monkeypatch.setattr("tools.tool_circuit_breaker.TOOL_CIRCUIT_BREAKER", tracker)
    monkeypatch.setattr(
        "model_tools.registry.dispatch", lambda *a, **kw: json.dumps({"ok": True})
    )

    class _Agent:
        session_id = "s1"

    model_tools.handle_function_call(
        "read_file",
        {"path": "nowhere.txt"},
        task_id="t1",
        tool_call_id="call-3",
        session_id="s1",
    )
    _record_tool_circuit_breaker(_Agent(), "read_file", "boom", True, "t1", call_id="call-3")

    stats = tracker.get_session_stats("s1")["read_file"]
    assert stats["total_calls"] == 1
