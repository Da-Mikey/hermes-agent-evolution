"""Tests for the bundled observability/opentelemetry plugin.

Covers manifest layout, opt-in discovery, runtime gating, redaction,
JSONL export, and OTLP export shape.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_DIR = REPO_ROOT / "plugins" / "observability" / "opentelemetry"


# ---------------------------------------------------------------------------
# Manifest + layout
# ---------------------------------------------------------------------------


class TestManifest:
    def test_plugin_directory_exists(self):
        assert PLUGIN_DIR.is_dir()
        assert (PLUGIN_DIR / "plugin.yaml").exists()
        assert (PLUGIN_DIR / "__init__.py").exists()

    def test_manifest_fields(self):
        data = yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text())
        assert data["name"] == "opentelemetry"
        assert data["version"]
        assert "opentelemetry" in data["description"].lower()
        assert set(data["hooks"]) >= {
            "pre_api_request",
            "post_api_request",
            "api_request_error",
            "pre_llm_call",
            "post_llm_call",
            "pre_tool_call",
            "post_tool_call",
            "on_session_start",
            "on_session_end",
            "subagent_start",
            "subagent_stop",
        }


# ---------------------------------------------------------------------------
# Opt-in discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_plugin_is_discovered_as_standalone_opt_in(self, tmp_path, monkeypatch):
        """Scanner finds the plugin but does NOT load it by default."""
        from hermes_cli import plugins as plugins_mod

        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        manager = plugins_mod.PluginManager()
        manager.discover_and_load()

        loaded = manager._plugins.get("observability/opentelemetry")
        assert loaded is not None, "plugin not discovered"
        assert loaded.enabled is False
        assert "not enabled" in (loaded.error or "").lower()


# ---------------------------------------------------------------------------
# Runtime gate
# ---------------------------------------------------------------------------


def _fresh_plugin(tmp_path=None):
    mod_name = "plugins.observability.opentelemetry"
    sys.modules.pop(mod_name, None)
    sys.modules.pop("hermes_cli.config", None)
    sys.modules.pop("hermes_constants", None)
    mod = importlib.import_module(mod_name)
    return mod


class TestRuntimeGate:
    def test_tracer_disabled_by_default(self, monkeypatch, tmp_path):
        """Without opt-in config/env, _get_tracer() returns None."""
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text("model:\n  default: test\n")
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("HERMES_OTEL_ENABLED", raising=False)

        mod = _fresh_plugin()
        assert mod._get_tracer() is None

    def test_tracer_enabled_via_env(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text("model:\n  default: test\n")
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_OTEL_ENABLED", "true")

        mod = _fresh_plugin()
        tracer = mod._get_tracer()
        assert tracer is not None
        assert tracer.settings.exporter == "jsonl"

    def test_tracer_enabled_via_config(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "model:\n  default: test\nobservability:\n  opentelemetry:\n    enabled: true\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.delenv("HERMES_OTEL_ENABLED", raising=False)

        mod = _fresh_plugin()
        tracer = mod._get_tracer()
        assert tracer is not None


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


class TestRedaction:
    def test_redact_string_censors_secret(self):
        mod = _fresh_plugin()
        raw = "MY_API_KEY=sk-12345deadbeef"
        out = mod._redact_string(raw, 1000)
        assert "sk-12345deadbeef" not in out
        assert "redacted" in out

    def test_redact_payload_censors_dict_secret_keys(self):
        mod = _fresh_plugin()
        out = mod._redact_payload({"secret": "s", "password": "p", "ok": "visible"})
        assert out["secret"] == "<redacted>"
        assert out["password"] == "<redacted>"
        assert out["ok"] == "visible"

    def test_redact_payload_omits_file_content(self):
        mod = _fresh_plugin()
        out = mod._redact_payload({
            "file": {
                "path": "/home/user/.hermes/.env",
                "content": "GITHUB_TOKEN=ghp_secret",
                "total_lines": 5,
                "file_size": 100,
                "is_binary": False,
                "is_image": False,
            }
        })
        assert "content" not in out["file"]
        assert "ghp_secret" not in str(out)

    def test_redact_payload_respects_max_length(self):
        mod = _fresh_plugin()
        out = mod._redact_payload("x" * 20000, max_len=100)
        assert out.endswith("[19900 chars omitted]")
        assert len(out) < 150


# ---------------------------------------------------------------------------
# Tracing + export
# ---------------------------------------------------------------------------


class TestTracingAndExport:
    def test_session_start_end_writes_jsonl(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "model:\n  default: test\nobservability:\n  opentelemetry:\n    enabled: true\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setenv("HERMES_OTEL_ENABLED", "true")

        mod = _fresh_plugin()
        assert mod._get_tracer() is not None

        mod.on_session_start(session_id="s1", platform="cli")
        mod.on_session_end(session_id="s1", completed=True)

        output_dir = home / "telemetry"
        jsonl = list(output_dir.glob("*.jsonl"))
        assert jsonl, "expected JSONL output file"
        lines = jsonl[0].read_text(encoding="utf-8").strip().splitlines()
        assert lines
        record = json.loads(lines[-1])
        assert record["schema_version"] == mod._SCHEMA_VERSION
        assert record["session_id"] == "s1"
        assert record["service_name"] == "hermes-agent"
        spans_by_name = {s["name"]: s for s in record["spans"]}
        assert "hermes.session" in spans_by_name

    def test_tool_span_parented_under_api_span(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "model:\n  default: test\nobservability:\n  opentelemetry:\n    enabled: true\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))

        mod = _fresh_plugin()
        mod.on_session_start(session_id="s2")
        mod.pre_llm_call(session_id="s2", turn_id="t1")
        mod.pre_api_request(session_id="s2", turn_id="t1", api_request_id="r1")
        mod.pre_tool_call(
            session_id="s2",
            turn_id="t1",
            api_request_id="r1",
            tool_name="read_file",
            tool_call_id="tc1",
        )
        mod.post_tool_call(
            session_id="s2",
            turn_id="t1",
            api_request_id="r1",
            tool_name="read_file",
            tool_call_id="tc1",
            result="ok",
        )
        mod.post_api_request(session_id="s2", turn_id="t1", api_request_id="r1")
        mod.post_llm_call(session_id="s2", turn_id="t1")
        mod.on_session_end(session_id="s2")

        output_dir = home / "telemetry"
        jsonl = list(output_dir.glob("*.jsonl"))
        record = json.loads(jsonl[0].read_text().strip().splitlines()[-1])
        spans_by_id = {s["span_id"]: s for s in record["spans"]}
        tool_span = next(
            s for s in record["spans"] if s["name"] == "hermes.tool:read_file"
        )
        api_span = spans_by_id.get(tool_span["parent_id"])
        assert api_span is not None
        assert api_span["name"] == "hermes.llm_request"

    def test_sampling_zero_drops_trace(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "model:\n  default: test\nobservability:\n  opentelemetry:\n    enabled: true\n    sample_rate: 0\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))

        mod = _fresh_plugin()
        mod.on_session_start(session_id="s3")
        mod.on_session_end(session_id="s3")
        output_dir = home / "telemetry"
        assert not list(output_dir.glob("*.jsonl"))

    def test_otlp_exporter_shape(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "model:\n  default: test\nobservability:\n  opentelemetry:\n    enabled: true\n    exporter: otlp\n    endpoint: http://localhost:4318\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))

        mod = _fresh_plugin()
        tracer = mod._get_tracer()
        assert isinstance(tracer._exporter, mod._OtlpHttpExporter)

        trace = tracer.start_trace("s4", "")
        trace.close("done")
        otlp = mod._trace_to_otlp(trace)
        assert otlp["resource"]["attributes"]
        assert otlp["scopeSpans"]
        assert otlp["scopeSpans"][0]["spans"]


# ---------------------------------------------------------------------------
# Helpers exposed for other subsystems
# ---------------------------------------------------------------------------


class TestPublicHelpers:
    def test_get_current_trace_id_returns_trace_id(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "model:\n  default: test\nobservability:\n  opentelemetry:\n    enabled: true\n"
        )
        monkeypatch.setenv("HERMES_HOME", str(home))

        mod = _fresh_plugin()
        mod.on_session_start(session_id="s5")
        trace_id = mod.get_current_trace_id(session_id="s5")
        assert trace_id
        assert mod.format_trace_link(trace_id) == trace_id

    def test_add_event_safe_when_disabled(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text("model:\n  default: test\n")
        monkeypatch.setenv("HERMES_HOME", str(home))

        mod = _fresh_plugin()
        # Must not raise when telemetry is disabled and no trace exists.
        mod.add_event("evolution_stage", {"stage": "analysis"})


# ---------------------------------------------------------------------------
# Hooks are inert when disabled
# ---------------------------------------------------------------------------


class TestHooksInert:
    def test_all_hooks_noop_when_disabled(self, monkeypatch, tmp_path):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text("model:\n  default: test\n")
        monkeypatch.setenv("HERMES_HOME", str(home))

        mod = _fresh_plugin()
        mod.on_session_start(session_id="s6")
        mod.on_session_end(session_id="s6")
        mod.on_session_finalize(session_id="s6")
        mod.on_session_reset(old_session_id="s6", new_session_id="s7")
        mod.pre_llm_call(session_id="s7", turn_id="t1", user_message="hi")
        mod.post_llm_call(session_id="s7", turn_id="t1", assistant_response="hello")
        mod.pre_api_request(session_id="s7", turn_id="t1", api_request_id="r1")
        mod.post_api_request(session_id="s7", turn_id="t1", api_request_id="r1")
        mod.api_request_error(session_id="s7", api_request_id="r1", reason="boom")
        mod.pre_tool_call(session_id="s7", tool_name="test", tool_call_id="tc1")
        mod.post_tool_call(
            session_id="s7", tool_name="test", tool_call_id="tc1", result="ok"
        )
        mod.pre_approval_request(command="rm -rf /")
        mod.post_approval_response(command="rm -rf /", choice="deny")
        mod.subagent_start(parent_session_id="s7", child_session_id="c1")
        mod.subagent_stop(child_session_id="c1")
