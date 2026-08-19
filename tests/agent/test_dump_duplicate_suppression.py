"""Tests for duplicate API request dump suppression (issue #367)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.agent_runtime_helpers import dump_api_request_debug
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": n,
                "description": f"{n} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for n in names
    ]


def _make_agent(tmp_path: Path) -> AIAgent:
    with (
        patch(
            "run_agent.get_tool_definitions", return_value=_make_tool_defs("web_search")
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-12345678",
            base_url="https://my-llm.example.com/v1",
            provider="custom",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = MagicMock()
        agent.client.api_key = "sk-test"
        agent.logs_dir = tmp_path
        return agent


def test_duplicate_dump_suppressed_within_window(tmp_path: Path):
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

    err = ValueError("boom")
    err.status_code = 401  # type: ignore[attr-defined]

    first = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err)
    assert first is not None
    assert first.exists()

    second = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err)
    assert second is None


def test_duplicate_dump_allowed_after_window(tmp_path: Path):
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    err = ValueError("boom")
    err.status_code = 401  # type: ignore[attr-defined]

    first = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err)
    assert first is not None

    # Simulate cache entry aging out by poking the cache directly.
    cache = agent._request_dump_cache
    assert cache is not None
    key = next(iter(cache.keys()))
    cache[key] = time.time() - 61

    second = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err)
    assert second is not None
    assert second.exists()


def test_different_failure_category_not_suppressed(tmp_path: Path):
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

    err1 = ValueError("boom")
    err1.status_code = 401  # type: ignore[attr-defined]
    err2 = ValueError("boom2")
    err2.status_code = 429  # type: ignore[attr-defined]

    first = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err1)
    assert first is not None

    second = dump_api_request_debug(agent, api_kwargs, reason="rate_limit", error=err2)
    assert second is not None


def test_preflight_dump_not_suppressed_against_error_dump(tmp_path: Path):
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

    pre = dump_api_request_debug(agent, api_kwargs, reason="preflight")
    assert pre is not None

    err = ValueError("boom")
    err.status_code = 401  # type: ignore[attr-defined]
    post = dump_api_request_debug(agent, api_kwargs, reason="auth", error=err)
    assert post is not None


def test_dump_payload_includes_failure_category(tmp_path: Path):
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
    err = ValueError("paid subscription required")
    err.status_code = 403  # type: ignore[attr-defined]

    dump_file = dump_api_request_debug(agent, api_kwargs, reason="billing", error=err)
    assert dump_file is not None
    data = json.loads(dump_file.read_text())
    assert data["error"]["failure_category"] == "billing"
    assert data["error"]["retryable"] is False


def test_preflight_duplicate_suppressed_within_window(tmp_path: Path):
    """#73: preflight dumps (no error) previously bypassed dedup entirely and
    fired on every API call when HERMES_DUMP_REQUESTS is set. Identical
    preflight payloads within the window must now be suppressed."""
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

    first = dump_api_request_debug(agent, api_kwargs, reason="preflight")
    assert first is not None
    assert first.exists()

    second = dump_api_request_debug(agent, api_kwargs, reason="preflight")
    assert second is None


def test_preflight_duplicate_allowed_after_window(tmp_path: Path):
    """#73: a preflight dump is re-allowed once the dedup window expires."""
    agent = _make_agent(tmp_path)
    api_kwargs = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}

    first = dump_api_request_debug(agent, api_kwargs, reason="preflight")
    assert first is not None

    cache = agent._request_dump_cache
    assert cache is not None
    key = next(iter(cache.keys()))
    cache[key] = time.time() - 61

    second = dump_api_request_debug(agent, api_kwargs, reason="preflight")
    assert second is not None
    assert second.exists()


def test_per_session_cap_suppresses_after_limit(tmp_path: Path):
    """#73: a long-lived session is capped at request_dump.max_dumps_per_session
    (default 8) total dumps. Distinct bodies avoid dedup so each of the first
    eight writes, and the ninth trips the cap."""
    agent = _make_agent(tmp_path)

    for i in range(8):
        api_kwargs = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": f"msg {i}"}],
        }
        dumped = dump_api_request_debug(agent, api_kwargs, reason="preflight")
        assert dumped is not None, f"dump {i} should write (cap not yet reached)"

    ninth = dump_api_request_debug(
        agent,
        {"model": "gpt-4", "messages": [{"role": "user", "content": "msg 9"}]},
        reason="preflight",
    )
    assert ninth is None
