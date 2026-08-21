"""Tests for the shadow-MCP wiring in tools/mcp_tool.py (issue #90)."""

from __future__ import annotations

import pytest

import evolution.lib.shadow_mcp as sm
from tools.mcp_tool import _govern_outbound_endpoint


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


def test_deny_path_raises_connection_error():
    sm._governor = sm.ShadowMcpGovernor(
        policy=sm.ShadowMcpPolicy(deny=["example.com"])
    )
    with pytest.raises(ConnectionError):
        _govern_outbound_endpoint("srv", "https://example.com/x")


def test_alert_path_logs_but_does_not_raise():
    sm._governor = sm.ShadowMcpGovernor(
        policy=sm.ShadowMcpPolicy(allow=["ok.com"])
    )
    # Unapproved endpoint: alert verdict, but fail-open (no raise).
    _govern_outbound_endpoint("srv", "https://unapproved.com/x")
    assert sm._governor.unapproved()[0]["endpoint"] == "https://unapproved.com/x"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
