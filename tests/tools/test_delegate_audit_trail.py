"""Tests for structured audit trail recording during delegate_task execution (issue #3065)."""

from __future__ import annotations

import json
import threading
from unittest.mock import MagicMock, patch
import pytest

from agent import audit_trail
from tools.delegate_tool import delegate_task


@pytest.fixture(autouse=True)
def isolate_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))


def _make_mock_parent(session_id="parent-session-123", depth=0):
    parent = MagicMock()
    parent.session_id = session_id
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


def test_delegate_task_records_audit_event():
    parent = _make_mock_parent(session_id="session-subagent-test")

    mock_child_result = {
        "task_index": 0,
        "status": "completed",
        "summary": "Generated test report at report.md",
        "api_calls": 2,
        "duration_seconds": 1.5,
        "live_transcript": "/tmp/test.log",
    }

    with patch("tools.delegate_tool._run_single_child", return_value=mock_child_result):
        res_str = delegate_task(
            goal="Generate a comprehensive test report",
            parent_agent=parent,
        )
        res = json.loads(res_str)
        assert res["results"][0]["status"] == "completed"

    events = audit_trail.query_trail(session_id="session-subagent-test")
    assert len(events) >= 1
    delegation_event = events[0]["payload"]
    assert delegation_event["event_type"] == "delegation"
    assert delegation_event["session_id"] == "session-subagent-test"
    assert delegation_event["tool_name"] == "delegate_task"
    assert delegation_event["status"] == "success"
    assert any("task-0.log" in ref for ref in delegation_event["artifact_refs"])
    assert delegation_event["metadata"]["api_calls"] == 2

    # Verify DAG reconstruction
    recon = audit_trail.reconstruct_run("session-subagent-test")
    assert recon["event_count"] == len(events)
    assert recon["valid_chain"] is True
    assert len(recon["delegations"]) == 1
    assert any("task-0.log" in art for art in recon["artifacts"])

