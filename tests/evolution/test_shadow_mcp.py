# -*- coding: utf-8 -*-
"""Tests for shadow-MCP outbound endpoint governance (issue #90)."""

from __future__ import annotations

import json

import pytest

from evolution.lib.shadow_mcp import (
    ALERT,
    ALLOW,
    DENY,
    EndpointContact,
    ShadowMcpGovernor,
    ShadowMcpPolicy,
    endpoint_host,
    endpoint_matches,
)


# --- endpoint normalization / matching ------------------------------------


def test_endpoint_host_parses_netloc():
    assert endpoint_host("https://api.example.com:443/v1/x") == "api.example.com:443"
    assert endpoint_host("http://EXAMPLE.com") == "example.com"
    assert endpoint_host("bare.host.name") == "bare.host.name"


def test_endpoint_matches_exact_host():
    assert endpoint_matches("example.com", "https://example.com/x")
    assert endpoint_matches("example.com", "http://example.com:8080/x")
    assert not endpoint_matches("example.com", "https://evil.com/x")


def test_endpoint_matches_wildcard():
    assert endpoint_matches("*.example.com", "https://api.example.com/x")
    assert endpoint_matches("*.example.com", "https://example.com/x")
    assert endpoint_matches("*.example.com", "https://www.deep.example.com/x")
    assert not endpoint_matches("*.example.com", "https://evil.com/x")


def test_endpoint_matches_url_prefix():
    assert endpoint_matches("https://api.example.com/v1", "https://api.example.com/v1/x")
    assert not endpoint_matches("https://api.example.com/v1", "https://api.example.com/v2/x")


def test_endpoint_matches_empty_and_garbage():
    assert not endpoint_matches("", "https://example.com")
    assert not endpoint_matches("example.com", "")


# --- policy ----------------------------------------------------------------


def test_policy_default_is_allow_all():
    policy = ShadowMcpPolicy()
    assert policy.evaluate("https://anything.example.com/x") == ALLOW


def test_policy_allowlist_alerts_unapproved():
    policy = ShadowMcpPolicy(allow=["approved.com"])
    assert policy.evaluate("https://approved.com/x") == ALLOW
    assert policy.evaluate("https://other.com/x") == ALERT


def test_policy_denylist_blocks():
    policy = ShadowMcpPolicy(deny=["*.evil.com"])
    assert policy.evaluate("https://phish.evil.com/x") == DENY
    assert policy.evaluate("https://good.com/x") == ALLOW


def test_policy_deny_wins_over_allow():
    policy = ShadowMcpPolicy(allow=["example.com"], deny=["example.com"])
    assert policy.evaluate("https://example.com/x") == DENY


def test_policy_from_config():
    policy = ShadowMcpPolicy.from_config(
        {"enabled": False, "allow": ["a.com"], "deny": ["b.com"], "junk": [1, 2]}
    )
    assert policy.enabled is False
    assert policy.allow == ["a.com"]
    assert policy.deny == ["b.com"]


def test_policy_from_config_none_or_garbage():
    assert ShadowMcpPolicy.from_config(None).enabled is True
    assert ShadowMcpPolicy.from_config("nope").allow == []


# --- governor --------------------------------------------------------------


def test_governor_records_and_counts(tmp_path):
    gov = ShadowMcpGovernor(policy=ShadowMcpPolicy())
    assert gov.record_contact("srv", "https://a.com/x") == ALLOW
    assert gov.record_contact("srv", "https://a.com/x") == ALLOW
    assert gov.record_contact("srv", "https://b.com/x") == ALLOW

    digest = gov.audit_digest()
    assert len(digest) == 2
    by_endpoint = {d["endpoint"]: d for d in digest}
    assert by_endpoint["https://a.com/x"]["count"] == 2
    assert by_endpoint["https://b.com/x"]["count"] == 1
    assert gov.unapproved() == []


def test_governor_alerts_unapproved():
    gov = ShadowMcpGovernor(policy=ShadowMcpPolicy(allow=["ok.com"]))
    assert gov.record_contact("srv", "https://ok.com/x") == ALLOW
    assert gov.record_contact("srv", "https://unapproved.com/x") == ALERT
    assert [d["endpoint"] for d in gov.unapproved()] == ["https://unapproved.com/x"]


def test_governor_deny_verdict():
    gov = ShadowMcpGovernor(policy=ShadowMcpPolicy(deny=["bad.com"]))
    assert gov.record_contact("srv", "https://bad.com/x") == DENY
    assert gov.unapproved()[0]["last_verdict"] == DENY


def test_governor_disabled_policy_never_blocks_or_alerts():
    gov = ShadowMcpGovernor(policy=ShadowMcpPolicy(allow=["ok.com"], enabled=False))
    assert gov.record_contact("srv", "https://whatever.com/x") == ALLOW
    assert gov.unapproved() == []


def test_governor_writes_jsonl(tmp_path):
    log_path = tmp_path / "shadow.jsonl"
    gov = ShadowMcpGovernor(policy=ShadowMcpPolicy(), log_path=log_path)
    gov.record_contact("srv", "https://a.com/x")
    gov.record_contact("srv", "https://a.com/x")

    lines = log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["server"] == "srv"
    assert rec["endpoint"] == "https://a.com/x"
    assert rec["host"] == "a.com"
    assert rec["count"] == 1


def test_governor_alerter_called_on_alert():
    seen = []

    def _alerter(verdict, contact):
        seen.append((verdict, contact.endpoint))

    gov = ShadowMcpGovernor(
        policy=ShadowMcpPolicy(allow=["ok.com"]), alerter=_alerter
    )
    gov.record_contact("srv", "https://ok.com/x")
    gov.record_contact("srv", "https://nope.com/x")
    assert seen == [(ALERT, "https://nope.com/x")]


def test_governor_clear_drops_contacts():
    gov = ShadowMcpGovernor()
    gov.record_contact("srv", "https://a.com/x")
    gov.clear()
    assert gov.audit_digest() == []


def test_endpoint_contact_roundtrip():
    c = EndpointContact(server="srv", endpoint="https://a.com/x", count=3)
    d = c.to_dict()
    assert EndpointContact.from_dict(d).to_dict() == d


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
