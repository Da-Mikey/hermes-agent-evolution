"""OFF is the no-token state — PUBLIC must not claim create_issues."""

import os

from evolution.detect_mode import detect_mode, get_github_config


def test_no_token_is_off(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_TOKEN", raising=False)
    assert detect_mode() == "OFF"
    perms = get_github_config()["permissions"]
    assert perms["create_issues"] is False
    assert perms["merge"] is False


def test_public_token(monkeypatch):
    monkeypatch.delenv("GITHUB_PRIVATE_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_test")
    assert detect_mode() == "PUBLIC"
    assert get_github_config()["permissions"]["create_issues"] is True
    assert get_github_config()["permissions"]["merge"] is False


def test_private_token(monkeypatch):
    monkeypatch.setenv("GITHUB_PRIVATE_TOKEN", "ghs_owner")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_other")
    assert detect_mode() == "PRIVATE"
    assert get_github_config()["permissions"]["merge"] is True
