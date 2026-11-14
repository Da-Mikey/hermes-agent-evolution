"""Tests for gateway code-skew detection (stale-checkout guard).

Companion to ``tests/test_stale_utils_module_import.py``: that test proves the
crash; these prove the guard that turns it into a clear "restart the gateway"
message before a model switch can hit it.  The dashboard mirror (#86207) lives
here too: ``web_server_config._dashboard_code_skew_guard`` and the
``/api/model/options`` 503 guard, which protect the Models page from the same
stale-module ImportError after ``hermes update``.
"""

import asyncio
import contextlib

import pytest

from gateway import code_skew
import hermes_cli.web_routers.models as _rt_models
import hermes_cli.web_server_config as _web_server_config
import hermes_cli.web_server_profiles as _web_server_profiles


@pytest.fixture(autouse=True)
def _reset_boot_fingerprint(monkeypatch):
    """Each test starts with no recorded boot fingerprint."""
    monkeypatch.setattr(code_skew, "_boot_fingerprint", None)


class TestDetectCodeSkew:
    def test_no_boot_fingerprint_means_no_skew(self, monkeypatch):
        # Nothing recorded (e.g. non-git install) -> never a false positive.
        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:def456")
        assert code_skew.detect_code_skew() is None


    def test_drift_is_detected_with_short_revs(self, monkeypatch):
        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:abc1234567890")
        code_skew.record_boot_fingerprint()

        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:def4567890123")
        skew = code_skew.detect_code_skew()
        assert skew == ("abc1234567", "def4567890")

    def test_same_commit_on_a_new_branch_is_not_skew(self, monkeypatch):
        # #393: `git checkout -b evolution/issue-N` keeps the commit, reloads
        # nothing, and must not request a gateway restart.
        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:dbbdd9727621")
        code_skew.record_boot_fingerprint()

        monkeypatch.setattr(
            code_skew,
            "_fingerprint",
            lambda: "git:refs/heads/evolution/issue-393-code-skew:dbbdd9727621",
        )
        assert code_skew.detect_code_skew() is None

    def test_new_commit_on_a_new_branch_is_skew(self, monkeypatch):
        # The ref changed AND the commit changed: a restart is genuinely wanted.
        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:dbbdd9727621")
        code_skew.record_boot_fingerprint()

        monkeypatch.setattr(
            code_skew,
            "_fingerprint",
            lambda: "git:refs/heads/evolution/issue-393:abc1234567890",
        )
        skew = code_skew.detect_code_skew()
        assert skew == ("dbbdd97276", "abc1234567")

    def test_unresolved_sha_still_compares_the_whole_fingerprint(self, monkeypatch):
        # No commit identity to compare -> stay conservative, keep the old
        # full-fingerprint behaviour rather than swallowing a real drift.
        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/main:unresolved")
        code_skew.record_boot_fingerprint()

        monkeypatch.setattr(code_skew, "_fingerprint", lambda: "git:refs/heads/other:unresolved")
        assert code_skew.detect_code_skew() == ("unresolved", "unresolved")


class TestCommitIdentity:

    def test_git_fingerprint_yields_its_sha(self):
        assert code_skew._commit_identity("git:refs/heads/main:abc123") == "abc123"

    def test_non_git_fingerprint_falls_back_to_the_whole_string(self):
        fp = "skills:0.23.0:20261014:1750000000000:4096"
        assert code_skew._commit_identity(fp) == fp

    def test_non_git_drift_with_an_unchanged_size_still_trips(self, monkeypatch):
        # A non-git install's identity is the whole string: the size field alone
        # would hide an edit that kept the byte count identical.
        monkeypatch.setattr(
            code_skew, "_fingerprint", lambda: "skills:0.23.0:20261014:1750000000000:4096"
        )
        code_skew.record_boot_fingerprint()

        monkeypatch.setattr(
            code_skew, "_fingerprint", lambda: "skills:0.23.0:20261014:1750000999999:4096"
        )
        assert code_skew.detect_code_skew() is not None




class TestShort:

    def test_keeps_unresolved_marker(self):
        assert code_skew._short("git:refs/heads/main:unresolved") == "unresolved"



class TestModelSwitchSkewGuard:

    def test_guard_message_names_revs_and_restart(self, monkeypatch):
        from gateway import slash_commands_model as slash_commands

        monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("abc1234567", "def4567890"))
        msg = slash_commands._model_switch_skew_guard()
        assert msg is not None
        assert "abc1234567" in msg
        assert "def4567890" in msg
        assert "hermes gateway restart" in msg


class TestDashboardCodeSkewGuard:
    """Dashboard mirror of the gateway's model-switch skew guard (#86207)."""


    def test_dashboard_guard_message_names_revs_and_restart(self, monkeypatch):

        monkeypatch.delenv("HERMES_SERVE_HEADLESS", raising=False)
        monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("abc1234567", "def4567890"))
        msg = _web_server_config._dashboard_code_skew_guard()
        assert msg is not None
        assert "abc1234567" in msg
        assert "def4567890" in msg
        assert "restart" in msg.lower()
        # Browser-dashboard path: never hardcode a Linux-only unit (#97046).
        assert "systemctl" not in msg

    def test_serve_guard_message_points_at_desktop_backend(self, monkeypatch):

        monkeypatch.setenv("HERMES_SERVE_HEADLESS", "1")
        monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("abc1234567", "def4567890"))
        msg = _web_server_config._dashboard_code_skew_guard()
        assert msg is not None
        assert "Desktop-owned backend" in msg
        assert "systemctl" not in msg
        assert "hermes-dashboard" not in msg


class TestModelOptionsSkewGuard:
    """/api/model/options must refuse with a clear 503 when the dashboard is stale.

    Regression for #86207: a dashboard kept alive across ``hermes update``
    serves stale modules, so the picker's lazy import of names the update added
    (``agent.model_metadata.is_grok_46_family``) raised ImportError and the
    handler collapsed it into a generic 500.  With the guard, the stale process
    surfaces "Restart required" (503) and never reaches the payload build.
    """

    def test_stale_dashboard_returns_503_and_skips_payload_build(self, monkeypatch):
        from fastapi import HTTPException

        monkeypatch.setattr(code_skew, "detect_code_skew", lambda: ("abc1234567", "def4567890"))

        payload_calls: list = []
        monkeypatch.setattr(
            "hermes_cli.inventory.build_model_options_payload",
            lambda *a, **k: payload_calls.append(1) or {"providers": []},
        )

        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(_rt_models.get_model_options())

        assert excinfo.value.status_code == 503
        assert "restart" in str(excinfo.value.detail).lower()
        # The stale-import crash site (the payload build) is never reached.
        assert payload_calls == []

    def test_fresh_dashboard_builds_payload_unchanged(self, monkeypatch):

        monkeypatch.setattr(code_skew, "detect_code_skew", lambda: None)

        async def _fake_run_in_threadpool(func):
            return func()

        monkeypatch.setattr(_rt_models, "run_in_threadpool", _fake_run_in_threadpool)
        monkeypatch.setattr(
            _web_server_profiles, "_profile_scope", lambda profile: contextlib.nullcontext()
        )
        monkeypatch.setattr("hermes_cli.inventory.load_picker_context", lambda: {})

        payload_calls: list = []
        expected = {"providers": [], "model": {}, "provider": None}
        monkeypatch.setattr(
            "hermes_cli.inventory.build_model_options_payload",
            lambda *a, **k: payload_calls.append(1) or expected,
        )

        result = asyncio.run(_rt_models.get_model_options())

        assert result == expected
        assert payload_calls == [1]
