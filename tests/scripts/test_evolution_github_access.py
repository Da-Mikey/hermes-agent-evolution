"""Pure-function tests for scripts/evolution_github_access.py."""

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "evolution_github_access.py"


def _mod():
    spec = importlib.util.spec_from_file_location("evolution_github_access", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_classify_http_write():
    m = _mod()
    body = '{"permissions": {"push": true, "pull": true}}'
    assert m.classify_http(200, body) == m.WRITE


def test_classify_http_denied_on_200_without_push():
    m = _mod()
    body = '{"permissions": {"push": false, "pull": true}}'
    assert m.classify_http(200, body) == m.DENIED


def test_classify_http_denied_on_403():
    m = _mod()
    assert m.classify_http(403, '{"message": "Must have push access"}') == m.DENIED
    # Error payloads must never be parsed as write, even if they mention push.
    assert m.classify_http(401, '{"permissions": {"push": true}}') == m.DENIED
    assert m.classify_http(404, '{"permissions": {"push": true}}') == m.DENIED


def test_classify_http_inconclusive_on_timeout_and_5xx():
    m = _mod()
    assert m.classify_http(None, "") == m.INCONCLUSIVE
    assert m.classify_http(500, "boom") == m.INCONCLUSIVE
    assert m.classify_http(429, "rate limit") == m.INCONCLUSIVE


def test_has_any_credentials_uses_stored_gh_auth_not_network(monkeypatch):
    m = _mod()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_TOKEN", raising=False)
    monkeypatch.setattr(m.shutil, "which", lambda _: "/usr/bin/gh")
    monkeypatch.setattr(m, "_gh_has_stored_auth", lambda: True)
    assert m.has_any_credentials() is True


def test_has_any_credentials_false_without_token_or_gh(monkeypatch):
    m = _mod()
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_PRIVATE_TOKEN", raising=False)
    monkeypatch.setattr(m.shutil, "which", lambda _: None)
    assert m.has_any_credentials() is False
