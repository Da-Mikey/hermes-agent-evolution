# -*- coding: utf-8 -*-
"""#359 — one agent tool call must cost ONE circuit-breaker budget step.

A dispatched tool result reached ``TOOL_CIRCUIT_BREAKER.record_result()`` twice:
once inside ``model_tools.handle_function_call`` (the inner record) and once in
``agent/tool_executor.py::_record_tool_circuit_breaker`` (the outer record).
``consecutive_failures`` and ``total_calls`` therefore advanced by two per call,
so ``terminal`` — budget 5 — tripped after 3 real failures and told the model it
had failed "5 consecutive times".

Both halves are asserted here: the executor's ownership context silences the
inner record, and without it the inner record still fires (the non-executor
callers — MCP transport, connectors dispatch, code kernel, TUI gateway — depend
on it).
"""

import pytest

import model_tools
from agent import tool_executor
from tools.tool_circuit_breaker import _ToolCircuitBreakerTracker


@pytest.fixture()
def breaker(monkeypatch):
    """Give each test a fresh tracker and make both layers use it."""
    tracker = _ToolCircuitBreakerTracker()
    import tools.tool_circuit_breaker as tcb

    monkeypatch.setattr(tcb, "TOOL_CIRCUIT_BREAKER", tracker)
    return tracker


def _dispatch_failing_call(
    monkeypatch, *, owned: bool, tool_name: str = "dummy_tool"
) -> None:
    """Drive one tool call whose result reads as a failure."""
    from tools.registry import registry

    monkeypatch.setattr(
        registry, "dispatch", lambda name, args, **kw: "[TOOL_ERROR] boom: timed out"
    )
    monkeypatch.setattr(model_tools, "_READ_SEARCH_TOOLS", frozenset())

    if owned:
        with model_tools.outer_owns_tool_result():
            model_tools.handle_function_call(
                tool_name,
                {},
                task_id="t1",
                session_id="s1",
                tool_call_id="tc1",
                skip_pre_tool_call_hook=True,
            )
    else:
        model_tools.handle_function_call(
            tool_name,
            {},
            task_id="t1",
            session_id="s1",
            tool_call_id="tc1",
            skip_pre_tool_call_hook=True,
        )


def test_inner_record_stands_down_when_executor_owns(breaker, monkeypatch):
    _dispatch_failing_call(monkeypatch, owned=True)
    assert breaker.get_session_stats("s1") == {}


def test_inner_record_still_fires_for_non_executor_callers(breaker, monkeypatch):
    # The MCP transport / connectors / code kernel never set the ownership
    # flag: the inner record is their only one and must keep working.
    _dispatch_failing_call(monkeypatch, owned=False)
    stats = breaker.get_session_stats("s1")
    assert stats["dummy_tool"]["total_calls"] == 1


class _Agent:
    session_id = "s1"


def test_one_call_costs_one_budget_step_end_to_end(breaker, monkeypatch):
    """Reproduce the executor dispatch order: inner record (silenced) + outer."""
    for _ in range(3):
        _dispatch_failing_call(monkeypatch, owned=True, tool_name="dummy_tool")
        tool_executor._record_tool_circuit_breaker(
            _Agent(), "dummy_tool", "[TOOL_ERROR] exit 1", True, "t1"
        )
    stats = breaker.get_session_stats("s1")
    # 3 real failures == 3 recorded failures, not 6.
    assert stats["dummy_tool"]["total_calls"] == 3
    assert stats["dummy_tool"]["consecutive_failures"] == 3


def test_double_count_is_what_tripped_the_breaker(breaker, monkeypatch):
    """The old shape — both layers recording — doubles one call's cost."""
    _dispatch_failing_call(monkeypatch, owned=False, tool_name="dummy_tool")  # inner
    tool_executor._record_tool_circuit_breaker(
        _Agent(), "dummy_tool", "[TOOL_ERROR] exit 1", True, "t1"
    )  # outer records again
    stats = breaker.get_session_stats("s1")
    assert stats["dummy_tool"]["consecutive_failures"] == 2  # one call, two steps

    # Under the fix the same single call advances the streak by exactly one.
    breaker.reset("s1")
    _dispatch_failing_call(monkeypatch, owned=True, tool_name="dummy_tool")
    tool_executor._record_tool_circuit_breaker(
        _Agent(), "dummy_tool", "[TOOL_ERROR] exit 1", True, "t1"
    )
    assert breaker.get_session_stats("s1")["dummy_tool"]["consecutive_failures"] == 1
