"""Safety refusals remain attributable and never invite bypass (#3475)."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("dispatch", ["sequential", "concurrent"])
def test_safety_refusal_stops_dispatch_and_survives_recovery(tmp_path, monkeypatch, dispatch):
    from agent import tool_executor as te
    from tools.tool_failure_classifier import classify_tool_failure
    from scripts.introspection_extract import _classify_failure_reason
    import model_tools
    from run_agent import AIAgent
    from agent.tool_guardrails import ToolCallGuardrailController
    from tools.recovery_strategy_dispatcher import recover_from_failure, RecoveryStrategy

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    report = home / "report.md"
    handler = Mock(side_effect=AssertionError("blocked call reached registry"))
    monkeypatch.setattr(model_tools, "handle_function_call", handler)
    agent = SimpleNamespace(
        _context_engine_tool_names=set(), _memory_manager=None, quiet_mode=False,
        _should_emit_quiet_tool_messages=lambda: False, _invoke_tool=Mock(),
    )
    for name, args in (
        ("write_file", {"path": str(report), "content": "Audit: protect .ssh/id_rsa"}),
        ("terminal", {"command": "cat .ssh/id_rsa"}),
    ):
        ref = te._ToolCallRef(name, args, "task", "call", [])
        if dispatch == "sequential":
            result = te._resolve_sequential_dispatch(agent, ref, []).execute(args)
        else:
            result = te._harm_gated_invoke(agent, ref, args, [])
        payload = json.loads(result)
        assert payload["status"] == "blocked"
        assert payload["harm_gate"] == "agent_process_bench"
        assert payload["reason"] == "safety-gate-blocked"
        assert "credential-access" in payload["error"]
        assert _classify_failure_reason(result) == payload["reason"]
        legacy = {key: payload[key] for key in ("error", "status", "harm_gate")}
        assert _classify_failure_reason(legacy) == payload["reason"]
        assert payload["retryable"] is False
        assert "approval" in payload["recovery_hint"]
        assert "Do not" in payload["recovery_hint"]
        observer = SimpleNamespace(
            _tool_guardrails=ToolCallGuardrailController(),
            _stall_guards_enabled=lambda: False,
            _failure_recovery_enabled=True,
        )
        observed = AIAgent._append_guardrail_observation(
            observer, name, args, result, failed=True,
        )
        assert "Non-retryable: safety_gate_blocked" in observed
        assert "Recovery strategy: surface_blocker" in observed
        assert "persistent_error" not in observed
        assert _classify_failure_reason(observed) == payload["reason"]
        classification = classify_tool_failure(name, result)
        assert classification.category.value == "safety_gate_blocked"
        assert classification.should_retry is False
        for streak in (0, 3, 5):
            recovery = recover_from_failure(name, result, consecutive_count=streak)
            assert recovery.strategy == RecoveryStrategy.surface_blocker
            assert not recovery.should_retry
            assert "approval" in recovery.directive
        assert not report.exists()
    agent._invoke_tool.assert_not_called()
    handler.assert_not_called()
    assert te._harm_gate_block_reason("write_file", {
        "path": str(report), "content": "Audit completed without findings",
    }) is None
    assert classify_tool_failure("write_file", "connection timed out").should_retry

