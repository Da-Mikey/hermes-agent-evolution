"""#353 — the executor's [CIRCUIT_BREAKER] diagnostic must actually be appended.

``agent/tool_executor.py::_record_tool_circuit_breaker`` read
``_breaker_event['suggested_action']`` while the tracker publishes
``strategy_recommendation``. The resulting ``KeyError`` landed in a
debug-level ``except Exception``, so it was indistinguishable from a benign
``record_result`` failure: the tracker tripped correctly, the tool result came
back unchanged, and the model was never told to stop retrying. Corpus evidence
in the issue: 243 breaker diagnostics over 60 days, **none** produced by this
branch.

These tests call the real function against the real tracker — no mock of the
code under test — and assert on the appended text, which is what the existing
``tests/tools/test_tool_circuit_breaker.py`` suite never did.
"""

import uuid

import pytest

from agent.tool_executor import _DEFAULT_BREAKER_ACTION, _record_tool_circuit_breaker
from tools.tool_circuit_breaker import TOOL_CIRCUIT_BREAKER, get_strategy_recommendation


class _FakeAgent:
    """Only the attribute ``_record_tool_circuit_breaker`` reads."""

    def __init__(self, session_id):
        self.session_id = session_id


def _fresh_session_id():
    sid = f"issue353-{uuid.uuid4().hex[:8]}"
    TOOL_CIRCUIT_BREAKER.reset(sid)
    return sid


def _trip_the_breaker(sid, tool_name="terminal", error="Connection reset by peer", attempts=10):
    """Feed consecutive failures until the tracker trips; return that result."""
    agent = _FakeAgent(sid)
    for _ in range(attempts):
        result = _record_tool_circuit_breaker(agent, tool_name, "boom", True, "task-1")
        if "[CIRCUIT_BREAKER]" in result:
            return result
    pytest.fail(f"breaker never tripped within {attempts} consecutive failures")


class TestDiagnosticIsAppended:
    def test_trip_appends_the_diagnostic_to_the_tool_result(self):
        result = _trip_the_breaker(_fresh_session_id())

        assert "boom" in result, "the original tool result must be preserved"
        assert "[CIRCUIT_BREAKER]" in result, "the diagnostic the model reads was dropped"
        assert "Suggested action:" in result

    def test_appended_action_is_the_trackers_recommendation(self):
        """The fix reads the key the tracker actually publishes."""
        result = _trip_the_breaker(_fresh_session_id())

        recommendation = get_strategy_recommendation("terminal", "transient", "Connection reset by peer")
        assert recommendation in result
        assert _DEFAULT_BREAKER_ACTION not in result

    def test_diagnostic_names_the_budget_and_class(self):
        result = _trip_the_breaker(_fresh_session_id())

        assert "consecutive times" in result
        assert "budget:" in result
        assert "Class: transient" in result

    def test_a_missing_recommendation_falls_back_instead_of_dropping_the_block(self):
        """A tracker event without ``strategy_recommendation`` used to raise
        KeyError and lose the whole diagnostic; the builder must be total."""
        sid = _fresh_session_id()
        agent = _FakeAgent(sid)

        appended = []
        for _ in range(10):
            result = _record_tool_circuit_breaker(agent, "terminal", "boom", True, "task-1")
            if "[CIRCUIT_BREAKER]" in result:
                appended.append(result)
                break

        if not appended:  # pragma: no cover - guard: the tracker must trip
            pytest.fail("breaker never tripped")

        # Now the same event shape minus the recommendation key.
        event = {
            "circuit_breaker_tripped": True,
            "consecutive_failures": 3,
            "budget": 3,
            "failure_class": "transient",
            "tool_name": "terminal",
        }
        assert "strategy_recommendation" not in event
        import tools.tool_circuit_breaker as breaker_mod

        class _StubBreaker:
            def record_result(self, **kwargs):
                return dict(event)

        real = breaker_mod.TOOL_CIRCUIT_BREAKER
        breaker_mod.TOOL_CIRCUIT_BREAKER = _StubBreaker()
        try:
            out = _record_tool_circuit_breaker(agent, "terminal", "boom", True, "task-1")
        finally:
            breaker_mod.TOOL_CIRCUIT_BREAKER = real

        assert "[CIRCUIT_BREAKER]" in out
        assert _DEFAULT_BREAKER_ACTION in out


class TestNonTrippingPathsAreUntouched:
    def test_a_clean_result_is_returned_unchanged(self):
        sid = _fresh_session_id()
        agent = _FakeAgent(sid)

        result = _record_tool_circuit_breaker(agent, "terminal", "all good", False, "task-1")

        assert result == "all good"

    def test_an_existing_diagnostic_is_not_duplicated(self):
        sid = _fresh_session_id()
        agent = _FakeAgent(sid)
        pre_marked = "[CIRCUIT_BREAKER] already here"

        for _ in range(10):
            result = _record_tool_circuit_breaker(agent, "terminal", pre_marked, True, "task-1")
            assert result == pre_marked, "the block must be appended at most once"
