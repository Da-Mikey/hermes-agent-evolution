"""Tests that browser_console(expression=...) cannot bypass the SSRF guard.

browser_snapshot / browser_vision re-check the page URL before returning
content, but ``_browser_eval`` returns arbitrary JS results directly. Two
sub-paths could read private content without ever touching snapshot/vision:

  1. Direct fetch:  ``fetch('http://127.0.0.1/secret').then(r => r.text())``
     — the page URL stays public, so the post-eval recheck can't see it.
     Closed by a pre-scan of the expression for private-host URL literals.
  2. Navigate-then-read:  ``location.href = 'http://127.0.0.1/'`` then a later
     eval reads ``document.body.innerText`` — closed by re-checking the page
     URL after the eval runs.

This is the sibling fix for the eval return-value path of issue #44731.
"""

import json

import pytest

from tools import browser_tool
from tools import browser_tool_eval_policy as bt_eval_policy
from tools import browser_tool_cloud as bt_cloud
from tools import browser_tool_session as bt_session


PRIVATE_URL = "http://127.0.0.1:8080/secret"
PUBLIC_URL = "https://example.com/page"
METADATA_URL = "http://169.254.169.254/latest/meta-data/"


@pytest.fixture(autouse=True)
def _no_camofox(monkeypatch):
    monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: False)
    # No supervisor — force the subprocess fallback path by default.
    monkeypatch.setattr(browser_tool, "_last_session_key", lambda key: key)


def _eval(expression, task_id="test"):
    return json.loads(browser_tool._browser_eval(expression, task_id=task_id))


# ---------------------------------------------------------------------------
# Sub-path 1: direct private-host fetch literal in the expression (pre-scan)
# ---------------------------------------------------------------------------


class TestExpressionPreScan:
    def _guard_on(self, monkeypatch):
        monkeypatch.setattr(bt_cloud, "_is_local_backend", lambda: False)
        monkeypatch.setattr(browser_tool, "_is_local_sidecar_key", lambda key: False)
        monkeypatch.setattr(bt_cloud, "_allow_private_urls", lambda: False)

    def test_blocks_private_fetch_literal(self, monkeypatch):
        self._guard_on(monkeypatch)
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: False)
        monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda url: False)

        called = {"n": 0}

        def _run(task_id, command, args=None, **kwargs):
            called["n"] += 1
            return {"success": True, "data": {"result": "leaked-content"}}

        monkeypatch.setattr(bt_session, "_run_browser_command", _run)

        result = _eval(f"fetch('{PRIVATE_URL}').then(r => r.text())")
        assert result["success"] is False
        assert "private or internal address" in result["error"]
        assert PRIVATE_URL in result["error"]
        # Expression never executed — blocked before any browser command.
        assert called["n"] == 0

    def test_blocks_metadata_fetch_literal(self, monkeypatch):
        self._guard_on(monkeypatch)
        # Public-safe to is_safe_url, but the always-blocked floor catches IMDS.
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: True)
        monkeypatch.setattr(
            browser_tool, "_is_always_blocked_url",
            lambda url: "169.254.169.254" in url,
        )
        monkeypatch.setattr(
            bt_session, "_run_browser_command",
            lambda *a, **k: {"success": True, "data": {"result": "creds"}},
        )

        result = _eval(f"fetch('{METADATA_URL}')")
        assert result["success"] is False
        assert "private or internal address" in result["error"]

    def test_allows_public_fetch_literal(self, monkeypatch):
        self._guard_on(monkeypatch)
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: True)
        monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda url: False)
        # After the (public) eval, the page-URL recheck must also see a public URL.
        monkeypatch.setattr(
            bt_session, "_run_browser_command",
            lambda task_id, command, args=None, **k: (
                {"success": True, "data": {"result": PUBLIC_URL}}
                if args == ["window.location.href"]
                else {"success": True, "data": {"result": "ok"}}
            ),
        )

        result = _eval(f"fetch('{PUBLIC_URL}').then(r => r.text())")
        assert result["success"] is True
        assert result["result"] == "ok"


    def test_skips_prescan_when_allow_private(self, monkeypatch):
        monkeypatch.setattr(bt_cloud, "_is_local_backend", lambda: False)
        monkeypatch.setattr(browser_tool, "_is_local_sidecar_key", lambda key: False)
        monkeypatch.setattr(bt_cloud, "_allow_private_urls", lambda: True)
        monkeypatch.setattr(
            bt_session, "_run_browser_command",
            lambda *a, **k: {"success": True, "data": {"result": "allowed"}},
        )
        result = _eval(f"fetch('{PRIVATE_URL}')")
        assert result["success"] is True


# ---------------------------------------------------------------------------
# Sub-path 2: navigate-then-read (post-eval page-URL recheck)
# ---------------------------------------------------------------------------


class TestCamofoxEvalGuard:
    def _guard_on(self, monkeypatch):
        monkeypatch.setattr(browser_tool, "_is_camofox_mode", lambda: True)
        monkeypatch.setattr(bt_cloud, "_is_local_backend", lambda: False)
        monkeypatch.setattr(browser_tool, "_is_local_sidecar_key", lambda key: False)
        monkeypatch.setattr(bt_cloud, "_allow_private_urls", lambda: False)

    def test_camofox_blocks_private_fetch_literal_before_request(self, monkeypatch):
        self._guard_on(monkeypatch)
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: False)
        monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda url: False)

        import tools.browser_camofox as camofox

        def fail_session(*_args, **_kwargs):
            raise AssertionError("Camofox request should not run for a private URL literal")

        monkeypatch.setattr(camofox, "_ensure_tab", fail_session)

        result = _eval(f"fetch('{PRIVATE_URL}').then(r => r.text())")

        assert result["success"] is False
        assert "private or internal address" in result["error"]
        assert PRIVATE_URL in result["error"]

    def test_camofox_blocks_when_current_page_is_private(self, monkeypatch):
        self._guard_on(monkeypatch)
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: False)
        monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda url: False)

        import tools.browser_camofox as camofox

        monkeypatch.setattr(camofox, "_ensure_tab", lambda task_id: {"tab_id": "tab-1", "user_id": "user-1"})

        def fake_post(path, body=None, **_kwargs):
            if body and body.get("expression") == "window.location.href":
                return {"result": PRIVATE_URL}
            return {"result": "secret DOM text"}

        monkeypatch.setattr(camofox, "_post", fake_post)

        result = _eval("document.body.innerText")

        assert result["success"] is False
        assert "private or internal address" in result["error"]
        assert PRIVATE_URL in result["error"]
        assert "secret DOM text" not in json.dumps(result)

    def test_camofox_uses_raw_task_id_not_resolved_session_key(self, monkeypatch):
        # Camofox keeps its own raw-task_id-keyed session map; eval must pass the
        # raw task_id (like every sibling Camofox tool), NOT the agent-browser
        # _last_session_key-resolved key, or it can hit a different/new tab and
        # skip the pre-scan via a mismatched _is_local_sidecar_key check.
        self._guard_on(monkeypatch)
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: True)
        monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda url: False)
        monkeypatch.setattr(
            browser_tool, "_last_session_key", lambda task_id: "resolved-agent-browser-key"
        )

        import tools.browser_camofox as camofox

        seen = {}

        def record_tab(task_id):
            seen["task_id"] = task_id
            return {"tab_id": "tab-1", "user_id": "user-1"}

        monkeypatch.setattr(camofox, "_ensure_tab", record_tab)
        monkeypatch.setattr(
            camofox, "_post", lambda path, body=None, **_kw: {"result": "https://example.com"}
        )

        result = _eval("document.title", task_id="test")

        assert result["success"] is True
        assert seen["task_id"] == "test"


class TestPostEvalPageRecheck:
    def _guard_on(self, monkeypatch):
        monkeypatch.setattr(bt_cloud, "_is_local_backend", lambda: False)
        monkeypatch.setattr(browser_tool, "_is_local_sidecar_key", lambda key: False)
        monkeypatch.setattr(bt_cloud, "_allow_private_urls", lambda: False)

    def test_blocks_when_page_navigated_private(self, monkeypatch):
        self._guard_on(monkeypatch)
        # Expression itself has no URL literal (reads the DOM), so the pre-scan
        # passes; the danger is that the page was navigated to a private URL by
        # an earlier eval. The recheck must catch it.
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: False)
        monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda url: False)
        monkeypatch.setattr(
            bt_session, "_run_browser_command",
            lambda task_id, command, args=None, **k: (
                {"success": True, "data": {"result": PRIVATE_URL}}
                if args == ["window.location.href"]
                else {"success": True, "data": {"result": "secret DOM text"}}
            ),
        )

        result = _eval("document.body.innerText")
        assert result["success"] is False
        assert "private or internal address" in result["error"]
        assert PRIVATE_URL in result["error"]

    def test_allows_when_page_public(self, monkeypatch):
        self._guard_on(monkeypatch)
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: True)
        monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda url: False)
        monkeypatch.setattr(
            bt_session, "_run_browser_command",
            lambda task_id, command, args=None, **k: (
                {"success": True, "data": {"result": PUBLIC_URL}}
                if args == ["window.location.href"]
                else {"success": True, "data": {"result": "public DOM text"}}
            ),
        )

        result = _eval("document.body.innerText")
        assert result["success"] is True
        assert result["result"] == "public DOM text"

    def test_fail_open_when_url_probe_fails(self, monkeypatch):
        """If the window.location.href probe errors, don't block (fail-open)."""
        self._guard_on(monkeypatch)
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: False)
        monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda url: False)

        def _run(task_id, command, args=None, **k):
            if args == ["window.location.href"]:
                return {"success": False, "error": "CDP probe failed"}
            return {"success": True, "data": {"result": "dom text"}}

        monkeypatch.setattr(bt_session, "_run_browser_command", _run)

        result = _eval("document.body.innerText")
        assert result["success"] is True
        assert result["result"] == "dom text"


# ---------------------------------------------------------------------------
# Helper-level unit tests
# ---------------------------------------------------------------------------


class TestExpressionScanHelper:
    def test_returns_first_private_literal(self, monkeypatch):
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: "127.0.0.1" not in url)
        monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda url: False)
        out = bt_eval_policy._expression_targets_private_url(
            "fetch('https://example.com'); fetch('http://127.0.0.1/x')"
        )
        assert out == "http://127.0.0.1/x"


    def test_strips_trailing_punctuation(self, monkeypatch):
        monkeypatch.setattr(browser_tool, "_is_safe_url", lambda url: False)
        monkeypatch.setattr(browser_tool, "_is_always_blocked_url", lambda url: False)
        out = bt_eval_policy._expression_targets_private_url("location.href='http://10.0.0.1/';")
        assert out == "http://10.0.0.1/"


# ---------------------------------------------------------------------------
# Raw CDP: the cloud-metadata floor survives the private-URL guard being OFF
# ---------------------------------------------------------------------------


def test_raw_cdp_keeps_the_metadata_floor_when_the_private_guard_is_off(monkeypatch):
    """``allow_private_urls`` / ``trust_private_hosts`` must never unblock 169.254.169.254.

    ``_browser_cdp_private_guard`` short-circuits when eval-driven private-network access is
    unguarded (local backend, local sidecar, ``allow_private_urls``) — which used to skip the
    whole guard, leaving raw CDP the one path that could open the cloud-metadata endpoint
    while ``browser_exec`` refuses it unconditionally. The metadata floor is now the
    unconditional piece; ordinary private URLs stay allowed (that is the escape hatch).
    """
    from tools import browser_cdp_tool

    monkeypatch.setattr(bt_eval_policy, "_eval_ssrf_guard_active", lambda _tid: False)
    guard = browser_cdp_tool._browser_cdp_private_guard
    for method in ("Page.navigate", "Target.createTarget"):
        blocked = guard(task_id="t-1", method=method, params={"url": METADATA_URL})
        assert blocked is not None, f"{method} opened the cloud-metadata endpoint"
        assert "cloud metadata" in str(blocked)
        assert guard(task_id="t-1", method=method, params={"url": PRIVATE_URL}) is None
    # methods that carry no request target are untouched
    assert guard(task_id="t-1", method="Page.captureScreenshot", params={}) is None


def test_raw_cdp_metadata_floor_covers_url_parameters_and_string_bodies(monkeypatch):
    """The floor is not just a ``url`` param: fetches in evaluated bodies and network
    preloads reach the metadata endpoint too (upstream 1fdfdecbcf / f4aa77a467)."""
    from tools import browser_cdp_tool

    monkeypatch.setattr(bt_eval_policy, "_eval_ssrf_guard_active", lambda _tid: False)
    guard = browser_cdp_tool._browser_cdp_private_guard

    fetch = guard(task_id="t-1", method="Runtime.evaluate",
                  params={"expression": "fetch('http://169.254.169.254/latest/meta-data/')"})
    assert fetch is not None and "cloud metadata" in str(fetch)

    preload = guard(task_id="t-1", method="Network.loadNetworkResource",
                    params={"url": METADATA_URL, "frameId": "f-1"})
    assert preload is not None and "cloud metadata" in str(preload)

    continued = guard(task_id="t-1", method="Fetch.continueRequest",
                      params={"requestId": "r-1", "url": METADATA_URL})
    assert continued is not None and "cloud metadata" in str(continued)


def test_raw_cdp_typing_a_metadata_url_is_not_refused(monkeypatch):
    """``Input.*`` inserts text / dispatches events; it fetches nothing (upstream 9a0bbf03af)."""
    from tools import browser_cdp_tool

    monkeypatch.setattr(bt_eval_policy, "_eval_ssrf_guard_active", lambda _tid: False)
    assert browser_cdp_tool._browser_cdp_private_guard(
        task_id="t-1", method="Input.insertText", params={"text": METADATA_URL}) is None


def test_raw_cdp_blocks_metadata_for_unlisted_methods_even_with_the_guard_on(monkeypatch):
    """Guard ON used to check only the page URL, so an unlisted method could preload a
    metadata host from a public page (upstream f4aa77a467). The floor is unconditional.

    The floor fires before the page-URL probe, so no live browser is needed here.
    """
    from tools import browser_cdp_tool

    monkeypatch.setattr(bt_eval_policy, "_eval_ssrf_guard_active", lambda _tid: True)
    blocked = browser_cdp_tool._browser_cdp_private_guard(
        task_id="t-1", method="Network.loadNetworkResource",
        params={"url": METADATA_URL, "frameId": "f-1"})
    assert blocked is not None and "cloud metadata" in str(blocked)
