"""#384 — an empty cause must never be published, and a timeout must not be
stamped non-retryable.

The bug: ``_dispatch`` built the cause with ``str(e)`` alone. Exception classes
with an empty ``str()`` — the whole asyncio timeout family — produced
``"Failed to call automation.trigger: "``. With nothing after the colon the
failure classifier fell through every rule to ``persistent_error``
(non-retryable), so a transient timeout was reported as a permanent defect.

Every check here is both-directional: the old payload shape must classify as
non-retryable ``persistent_error`` (the breaking fixture, proving the classifier
really is the thing that changed) and the new shape as retryable ``timeout``.
"""

import asyncio
import json

import pytest

from tools import homeassistant_tool
from tools.homeassistant_tool import (
    _describe_exception,
    _dispatch,
    _failure_reason,
    _handle_get_state,
    _is_timeout_exception,
)
from tools.tool_failure_classifier import ToolFailureCategory, classify_tool_failure

FAIL_MSG = "Failed to call automation.trigger"


async def _raise_timeout():
    raise asyncio.TimeoutError()


async def _raise_empty_non_timeout():
    raise RuntimeError()


async def _raise_informative():
    raise RuntimeError("HTTP 500 from Home Assistant")


async def _raise_typed_timeout():
    class ServerTimeoutError(Exception):
        """Mimics aiohttp's aiohttp.ServerTimeoutError: no message of its own."""

    raise ServerTimeoutError()


def _error_text(payload: str) -> str:
    parsed = json.loads(payload)
    assert "error" in parsed, payload
    return parsed["error"]


# ---------------------------------------------------------------------------
# The bug itself: a published cause is never empty
# ---------------------------------------------------------------------------


class TestCauseIsNeverEmpty:
    def test_old_shape_left_nothing_after_the_colon(self):
        # The breaking fixture: this is what ``f"{fail_msg}: {e}"`` produced.
        # It is here to pin the failure mode, not to be fixed.
        assert f"{FAIL_MSG}: {asyncio.TimeoutError()}" == f"{FAIL_MSG}: "

    def test_timeout_payload_carries_a_cause(self):
        text = _error_text(_dispatch(_raise_timeout(), "ha_call_service", FAIL_MSG))
        assert text != f"{FAIL_MSG}: "
        assert text.strip() != FAIL_MSG.rstrip(":")
        # The class name is always named, and a timeout is always spelled out.
        assert "TimeoutError" in text
        assert "timed out" in text

    def test_class_name_typed_timeout_is_recognised(self):
        # aiohttp's ServerTimeoutError is a lazily-imported subclass, matched here
        # on the class name rather than by importing aiohttp.
        text = _error_text(
            _dispatch(_raise_typed_timeout(), "ha_call_service", FAIL_MSG)
        )
        assert "ServerTimeoutError" in text
        assert "timed out" in text

    def test_non_timeout_exception_with_no_message_still_names_the_class(self):
        text = _error_text(
            _dispatch(_raise_empty_non_timeout(), "ha_call_service", FAIL_MSG)
        )
        assert "RuntimeError" in text
        assert text != f"{FAIL_MSG}: "

    def test_informative_message_is_preserved_verbatim(self):
        text = _error_text(_dispatch(_raise_informative(), "ha_call_service", FAIL_MSG))
        assert text == f"{FAIL_MSG}: RuntimeError: HTTP 500 from Home Assistant"

    def test_ha_get_state_path_also_never_publishes_an_empty_cause(self, monkeypatch):
        # The same ``str(e)`` bug lived in the generic except of _handle_get_state.
        def _boom(_entity_id):
            raise asyncio.TimeoutError()

        monkeypatch.setattr(homeassistant_tool, "_async_get_state", _boom)
        payload = json.loads(_handle_get_state({"entity_id": "light.bedroom"}))
        assert payload["error"] != "Failed to get state for light.bedroom: "
        assert "timed out" in payload["error"]
        assert payload.get("failure_reason") == "timeout"


# ---------------------------------------------------------------------------
# Classification: one mapping, both directions
# ---------------------------------------------------------------------------


class TestClassificationBothDirections:
    def test_old_empty_cause_is_non_retryable(self):
        # Breaking fixture — proves the assertion below is not vacuous: an empty
        # cause really does land in the non-retryable catch-all.
        verdict = classify_tool_failure(
            "ha_call_service", json.dumps({"error": f"{FAIL_MSG}: "})
        )
        assert verdict.category is ToolFailureCategory.persistent_error
        assert verdict.should_retry is False

    def test_timeout_payload_is_retryable_timeout(self):
        payload = _dispatch(_raise_timeout(), "ha_call_service", FAIL_MSG)
        verdict = classify_tool_failure("ha_call_service", payload)
        assert verdict.category is ToolFailureCategory.timeout
        assert verdict.should_retry is True

    def test_envelope_and_classifier_agree(self):
        # The envelope's failure_reason tag and the classifier's category must be
        # the same decision, so the two cannot tell the model different stories.
        payload = json.loads(_dispatch(_raise_timeout(), "ha_call_service", FAIL_MSG))
        verdict = classify_tool_failure("ha_call_service", json.dumps(payload))
        assert payload["failure_reason"] == verdict.category.value == "timeout"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelperUnits:
    @pytest.mark.parametrize(
        "exc, expected",
        [
            (asyncio.TimeoutError(), True),
            (TimeoutError("slow"), True),
        ],
    )
    def test_timeout_family_detected(self, exc, expected):
        assert _is_timeout_exception(exc) is expected

    def test_empty_str_timeout_is_described_not_blank(self):
        described = _describe_exception(asyncio.TimeoutError())
        assert described.startswith("TimeoutError")
        assert "timed out" in described

    def test_non_timeout_keeps_its_own_text(self):
        assert _describe_exception(ValueError("bad domain")) == "ValueError: bad domain"
        assert _describe_exception(ValueError()) == "ValueError raised with no message"

    def test_failure_reason_tags_only_the_timeout_family(self):
        assert _failure_reason(asyncio.TimeoutError()) == "timeout"
        assert _failure_reason(ValueError("nope")) is None
