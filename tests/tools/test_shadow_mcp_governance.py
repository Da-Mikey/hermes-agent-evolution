"""Tests for the shadow-MCP wiring in tools/mcp_tool.py (issue #90, rework r2)."""

from __future__ import annotations

import pytest

import evolution.lib.shadow_mcp as sm
from tools.mcp_tool import _classify_mcp_failure, _govern_outbound_endpoint


@pytest.fixture(autouse=True)
def _isolate_governor():
    """Each test starts from a clean governor singleton, then resets it."""
    sm._reset_governor()
    yield
    sm._reset_governor()


def test_empty_url_is_a_noop():
    # Must not raise and must not instantiate a governor for a bare/empty URL.
    _govern_outbound_endpoint("srv", "")
    assert sm._governor is None


def test_allow_path_does_not_raise():
    sm._governor = sm.ShadowMcpGovernor(policy=sm.ShadowMcpPolicy())
    # Default (allow-all) policy: contact is logged, connection is not blocked.
    _govern_outbound_endpoint("srv", "https://example.com/x")
    assert sm._governor.audit_digest()[0]["endpoint"] == "https://example.com/x"


def test_deny_path_raises_distinct_denied_error():
    sm._governor = sm.ShadowMcpGovernor(policy=sm.ShadowMcpPolicy(deny=["example.com"]))
    with pytest.raises(sm.ShadowMcpDeniedError):
        _govern_outbound_endpoint("srv", "https://example.com/x")


def test_deny_path_is_still_a_connection_error():
    # Subclasses ConnectionError so broad exception handlers keep working.
    sm._governor = sm.ShadowMcpGovernor(policy=sm.ShadowMcpPolicy(deny=["example.com"]))
    with pytest.raises(ConnectionError):
        _govern_outbound_endpoint("srv", "https://example.com/x")


def test_deny_verdict_classified_permanent_no_backoff():
    # A deny verdict must terminate the server task immediately — the reconnect
    # loop classifies ShadowMcpDeniedError as permanent (no backoff/park).
    exc = sm.ShadowMcpDeniedError("blocked by deny policy")
    assert _classify_mcp_failure(exc) == "permanent"
    # And the generic ConnectionError (e.g. a real network blip) stays transient.
    assert _classify_mcp_failure(ConnectionError("boom")) == "transient"


def test_deny_verdict_redacts_secret_in_error_and_log(tmp_path):
    log_path = tmp_path / "shadow.jsonl"
    sm._governor = sm.ShadowMcpGovernor(
        policy=sm.ShadowMcpPolicy(deny=["example.com"]), log_path=log_path
    )
    with pytest.raises(sm.ShadowMcpDeniedError):
        _govern_outbound_endpoint("srv", "https://example.com/x?token=SECRET")
    # The contact was logged (redacted) before the deny was raised.
    logged = log_path.read_text(encoding="utf-8")
    assert "SECRET" not in logged
    assert "token=" not in logged


def test_alert_path_logs_but_does_not_raise():
    sm._governor = sm.ShadowMcpGovernor(policy=sm.ShadowMcpPolicy(allow=["ok.com"]))
    # Unapproved endpoint: alert verdict, but fail-open (no raise).
    _govern_outbound_endpoint("srv", "https://unapproved.com/x")
    assert sm._governor.unapproved()[0]["endpoint"] == "https://unapproved.com/x"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
