"""#447 — the MCP tool-definition pin guard must actually be reached at runtime.

``agent/mcp_tool_pinning`` shipped with #944 and then lost its only call site in
the 2026-09-30 split of ``tools/mcp_tool.py``. Nothing failed: the module still
imported, its own tests still passed, and no pin was written or verified for 112
days. A test suite that only exercises the pinning module cannot catch that, so
these tests drive the two registration entry points and assert the guard is
reached — and that its trust semantics stay asymmetric.
"""

import logging
from types import SimpleNamespace

import pytest

import hermes_constants
from agent.mcp_tool_pinning import load_pins
from tools import mcp_tool_registration as reg
from tools import mcp_schema_cache


@pytest.fixture
def pins_home(tmp_path, monkeypatch):
    """Isolate the pins directory (``_pins_dir`` derives from ``HERMES_HOME``)."""
    home = tmp_path / "hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(hermes_constants, "_hermes_home_cache", None, raising=False)
    return home


def _tool(name, description="does a thing", schema=None):
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema=schema or {"type": "object", "properties": {}},
    )


def _widened(tool_name="a"):
    """The same tool name with a WIDER argument set — the rug-pull shape."""
    return _tool(
        tool_name,
        schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
        },
    )


# --- trust semantics of the helper ------------------------------------------


def test_live_registration_establishes_a_baseline(pins_home):
    """A live connect is allowed to pin what came off the wire."""
    assert load_pins("srv") is None

    reg._verify_tool_pins("srv", [_tool("a"), _tool("b")])

    pins = load_pins("srv")
    assert pins is not None
    assert sorted(pins) == ["a", "b"]


def test_changed_schema_is_detected_and_warned(pins_home, caplog):
    """A compatible-looking schema change must be reported, not silently accepted."""
    reg._verify_tool_pins("srv", [_tool("a")])
    before = load_pins("srv")

    with caplog.at_level(logging.WARNING, logger="agent.mcp_tool_pinning"):
        reg._verify_tool_pins("srv", [_widened("a")])

    assert any("tool definitions changed" in r.getMessage() for r in caplog.records)
    # detection is not acceptance: the baseline must not move on its own
    assert load_pins("srv") == before


def test_cache_replay_never_establishes_a_baseline(pins_home):
    """Pinning a cached manifest would launder an already-poisoned cache into trust."""
    reg._verify_tool_pins("srv", [_tool("a")], establish_baseline=False)

    assert load_pins("srv") is None


def test_cache_replay_still_compares_against_a_live_baseline(pins_home, caplog):
    """The lazy path must not become a blind spot once a baseline exists."""
    reg._verify_tool_pins("srv", [_tool("a")])

    with caplog.at_level(logging.WARNING, logger="agent.mcp_tool_pinning"):
        reg._verify_tool_pins("srv", [_widened("a")], establish_baseline=False)

    assert any("tool definitions changed" in r.getMessage() for r in caplog.records)


def test_a_pinning_failure_never_breaks_registration(pins_home, monkeypatch):
    """Pinning is a detector, not a gate — it must fail open, loudly enough only in debug."""

    def explode(*args, **kwargs):
        raise RuntimeError("pins directory is on fire")

    monkeypatch.setattr("agent.mcp_tool_pinning.verify_tools", explode)

    reg._verify_tool_pins("srv", [_tool("a")])  # must not raise


# --- the wiring itself ------------------------------------------------------


def _stub_registration(monkeypatch, captured):
    """Neutralise everything in the registration path except the pin call."""
    monkeypatch.setattr(reg, "_record_tool_trust_metadata", lambda *a, **k: None)
    monkeypatch.setattr(reg, "_server_key_for_task", lambda server: "key")
    monkeypatch.setattr(reg, "_make_tool_filter", lambda name, config: lambda _n: True)
    monkeypatch.setattr(reg, "_select_utility_schemas", lambda *a, **k: [])
    monkeypatch.setattr(reg, "_tool_candidates", lambda *a, **k: [])
    monkeypatch.setattr(reg, "_utility_candidates", lambda *a, **k: [])
    monkeypatch.setattr(reg, "_resolve_durability_backend", lambda: None)
    monkeypatch.setattr(reg, "_register_candidates", lambda *a, **k: [])
    monkeypatch.setattr(reg, "_write_schema_cache", lambda *a, **k: None)

    def capture(name, tools, **kwargs):
        captured.append((name, list(tools), kwargs))

    monkeypatch.setattr(reg, "_verify_tool_pins", capture)


def test_live_registration_reaches_the_pin_guard(monkeypatch):
    """The exact call site #447 reported missing: ``_register_server_tools``."""
    captured = []
    _stub_registration(monkeypatch, captured)
    tools = [_tool("a"), _tool("b")]
    server = SimpleNamespace(_tools=tools, tool_timeout=5)

    reg._register_server_tools("srv", server, {})

    assert len(captured) == 1
    name, seen_tools, kwargs = captured[0]
    assert name == "srv"
    assert seen_tools == tools
    # live discovery may establish the baseline
    assert kwargs.get("establish_baseline", True) is True


def test_cache_replay_reaches_the_pin_guard_without_baseline(monkeypatch):
    """The lazy path registers with no child process, so it must not establish trust."""
    captured = []
    _stub_registration(monkeypatch, captured)
    monkeypatch.setattr(mcp_schema_cache, "config_fingerprint", lambda config: "fp")
    monkeypatch.setattr(
        mcp_schema_cache,
        "tools_from_cache_entry",
        lambda entry: [{"name": "a", "description": "d", "inputSchema": {}}],
    )
    monkeypatch.setattr(
        mcp_schema_cache, "utility_tools_from_cache_entry", lambda entry: []
    )

    reg._register_from_cache_sync("srv", {}, {"tools": []})

    assert len(captured) == 1
    name, seen_tools, kwargs = captured[0]
    assert name == "srv"
    assert [t.name for t in seen_tools] == ["a"]
    assert kwargs.get("establish_baseline") is False
