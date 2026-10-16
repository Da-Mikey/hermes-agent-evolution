# -*- coding: utf-8 -*-
"""Unit tests for tools.tool_circuit_breaker (#3241)."""

import pytest
from tools.tool_circuit_breaker import (
    _ToolCircuitBreakerTracker,
    classify_tool_error,
    get_strategy_recommendation,
    FAILURE_CLASS_NOT_FOUND,
    FAILURE_CLASS_PERMISSION,
    FAILURE_CLASS_INVALID_ARG,
    FAILURE_CLASS_TIMEOUT,
    FAILURE_CLASS_PROVIDER,
    FAILURE_CLASS_TRANSIENT,
    CALLER_ERROR_CLASSES,
)


class TestClassifyToolError:
    def test_not_found(self):
        assert classify_tool_error("FileNotFoundError: [Errno 2] No such file or directory: 'foo.txt'") == FAILURE_CLASS_NOT_FOUND
        assert classify_tool_error("Cannot find path /bar") == FAILURE_CLASS_NOT_FOUND

    def test_permission_denied(self):
        assert classify_tool_error("PermissionError: [Errno 13] Permission denied") == FAILURE_CLASS_PERMISSION

    def test_invalid_arg(self):
        assert classify_tool_error("ValueError: invalid literal for int()") == FAILURE_CLASS_INVALID_ARG

    def test_timeout(self):
        assert classify_tool_error("TimeoutError: command timed out after 30s") == FAILURE_CLASS_TIMEOUT

    def test_provider_error(self):
        assert classify_tool_error("API rate limit exceeded (429)") == FAILURE_CLASS_PROVIDER

    def test_transient_fallback(self):
        assert classify_tool_error("Unexpected unknown anomaly") == FAILURE_CLASS_TRANSIENT
        assert classify_tool_error(None) == FAILURE_CLASS_TRANSIENT


class TestStrategyRecommendation:
    def test_patch_recommendation(self):
        rec = get_strategy_recommendation("patch", FAILURE_CLASS_NOT_FOUND)
        assert "read_file" in rec

    def test_read_file_recommendation(self):
        rec = get_strategy_recommendation("read_file", FAILURE_CLASS_NOT_FOUND)
        assert "target path exists" in rec

    def test_terminal_recommendation(self):
        rec = get_strategy_recommendation("terminal", FAILURE_CLASS_TIMEOUT)
        assert "background task" in rec or "prerequisites" in rec


class TestToolCircuitBreakerTracker:
    def test_single_failure_does_not_trip(self):
        tracker = _ToolCircuitBreakerTracker()
        event = tracker.record_result("sess1", "read_file", "error", "file not found", budget=3)
        assert event is None

    def test_exhausted_budget_trips_circuit_breaker(self):
        # A tool-health class (provider/transport) still spends the budget.
        tracker = _ToolCircuitBreakerTracker()
        tracker.record_result("sess1", "read_file", "error", "503 service unavailable", budget=3)
        tracker.record_result("sess1", "read_file", "error", "503 service unavailable", budget=3)
        event = tracker.record_result("sess1", "read_file", "error", "503 service unavailable", budget=3)
        assert event is not None
        assert event["circuit_breaker_tripped"] is True
        assert event["consecutive_failures"] == 3
        assert event["failure_class"] == FAILURE_CLASS_PROVIDER
        assert "target path exists" in event["strategy_recommendation"]

    def test_success_resets_consecutive_failures(self):
        tracker = _ToolCircuitBreakerTracker()
        tracker.record_result("sess1", "patch", "error", "patch reject", budget=3)
        tracker.record_result("sess1", "patch", "error", "patch reject", budget=3)
        # Success resets
        tracker.record_result("sess1", "patch", "ok")
        event = tracker.record_result("sess1", "patch", "error", "patch reject", budget=3)
        assert event is None  # Only 1 consecutive failure now

    def test_session_isolation(self):
        tracker = _ToolCircuitBreakerTracker()
        for _ in range(3):
            tracker.record_result("sess1", "terminal", "error", "command failed", budget=3)
        stats1 = tracker.get_session_stats("sess1")
        stats2 = tracker.get_session_stats("sess2")
        assert stats1["terminal"]["consecutive_failures"] == 3
        assert "terminal" not in stats2


class TestCallerErrorsDoNotTrip:
    """#218 — a served negative result is not an unhealthy tool.

    Live evidence the old accounting produced: ``[CIRCUIT_BREAKER] Tool
    'ha_get_state' has failed 3 consecutive times ... Class: not_found`` after two
    probes of entity ids that no longer exist. The lookup worked; the entities were
    absent. Repeated `not_found` errors must never trip the breaker, and must still be
    recorded so the spread stays visible in session stats.
    """

    NOT_FOUND_MSG = (
        "Failed to get state for sensor.uptime: 404, message='Not Found', "
        "url='http://10.0.0.14:8123/api/states/sensor.uptime'"
    )

    def test_repeated_not_found_never_trips(self):
        tracker = _ToolCircuitBreakerTracker()
        for _ in range(10):
            assert tracker.record_result(
                "sess218", "ha_get_state", "error", self.NOT_FOUND_MSG, budget=3
            ) is None
        rec = tracker.get_session_stats("sess218")["ha_get_state"]
        assert rec["consecutive_failures"] == 0
        assert rec["total_failures"] == 10  # still recorded, just not a strike
        assert rec["total_calls"] == 10
        assert rec["last_failure_class"] == FAILURE_CLASS_NOT_FOUND

    def test_invalid_argument_still_counts_toward_the_budget(self):
        """Deliberate boundary: ``invalid_argument`` also swallows tool-side TypeError /
        schema texts, so it is NOT excluded — a real malfunction must still trip."""
        tracker = _ToolCircuitBreakerTracker()
        for _ in range(2):
            assert tracker.record_result(
                "sess218",
                "ha_call_service",
                "error",
                "ValueError: invalid argument 'brightness'",
                budget=3,
            ) is None
        event = tracker.record_result(
            "sess218",
            "ha_call_service",
            "error",
            "ValueError: invalid argument 'brightness'",
            budget=3,
        )
        assert event is not None
        assert event["consecutive_failures"] == 3
        assert event["failure_class"] == FAILURE_CLASS_INVALID_ARG

    def test_membership_is_the_not_found_class_only(self):
        assert CALLER_ERROR_CLASSES == frozenset({FAILURE_CLASS_NOT_FOUND})
        for health_class in (
            FAILURE_CLASS_INVALID_ARG,
            FAILURE_CLASS_PROVIDER,
            FAILURE_CLASS_TIMEOUT,
            FAILURE_CLASS_PERMISSION,
            FAILURE_CLASS_TRANSIENT,
        ):
            assert health_class not in CALLER_ERROR_CLASSES

    def test_caller_error_clears_a_stale_health_streak(self):
        """An answered call proves the tool is up, so earlier strikes are stale."""
        tracker = _ToolCircuitBreakerTracker()
        tracker.record_result("sess218", "read_file", "error", "503 service unavailable", budget=3)
        tracker.record_result("sess218", "read_file", "error", "503 service unavailable", budget=3)
        tracker.record_result(
            "sess218", "read_file", "error", "No such file or directory: '/tmp/gone'", budget=3
        )
        assert tracker.get_session_stats("sess218")["read_file"]["consecutive_failures"] == 0
        # The next real failure is 1/3, not the third strike.
        assert tracker.record_result(
            "sess218", "read_file", "error", "503 service unavailable", budget=3
        ) is None

    def test_health_streak_trips_again_after_a_caller_error(self):
        """Excluding caller errors must not disarm the breaker for real failures."""
        tracker = _ToolCircuitBreakerTracker()
        tracker.record_result("sess218", "terminal", "error", "503 service unavailable", budget=2)
        tracker.record_result("sess218", "terminal", "error", "no such file", budget=2)
        assert tracker.get_session_stats("sess218")["terminal"]["consecutive_failures"] == 0
        tracker.record_result("sess218", "terminal", "error", "502 bad gateway", budget=2)
        event = tracker.record_result("sess218", "terminal", "error", "502 bad gateway", budget=2)
        assert event is not None
        assert event["consecutive_failures"] == 2
        assert event["budget"] == 2
