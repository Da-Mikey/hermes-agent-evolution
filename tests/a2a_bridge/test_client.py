"""Tests for the outbound A2A client — JSON-RPC envelope, result parsing, errors.

A fake transport exercises the whole request/response round-trip offline: no
network, no running peer.
"""

from __future__ import annotations

import pytest

import a2a_bridge
from a2a_bridge.client import (
    METHOD_MESSAGE_SEND,
    A2AClient,
    A2AClientError,
    _well_known_card_url,
)
from a2a_bridge.models import (
    ROLE_USER,
    TASK_STATE_COMPLETED,
    Message,
    Task,
)


class _RecordingTransport:
    """Captures the outbound request and returns a canned response."""

    def __init__(self, response):
        self.response = response
        self.calls = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        return self.response


# ── envelope construction ─────────────────────────────────────────────────────


def test_send_message_builds_jsonrpc_envelope():
    transport = _RecordingTransport(
        {"jsonrpc": "2.0", "id": "fixed", "result": {"task": {"id": "t1", "status": {"state": "completed"}}}}
    )
    client = A2AClient("https://peer.example.com/a2a", transport=transport)
    client.send_message(Message.from_text("hi", message_id="m1"), request_id="fixed")

    assert len(transport.calls) == 1
    body = transport.calls[0]["body"]
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == "fixed"
    assert body["method"] == METHOD_MESSAGE_SEND
    assert body["params"]["message"]["parts"] == [{"kind": "text", "text": "hi"}]
    assert transport.calls[0]["url"] == "https://peer.example.com/a2a"
    assert transport.calls[0]["headers"]["content-type"] == "application/json"


def test_delegate_text_round_trips_to_task():
    transport = _RecordingTransport(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "task": {
                    "id": "task-abc",
                    "contextId": "ctx-xyz",
                    "status": {"state": "completed"},
                    "artifacts": [{"artifactId": "a1", "parts": [{"text": "the answer"}]}],
                }
            },
        }
    )
    client = A2AClient("https://peer.example.com/a2a", transport=transport)
    result = client.delegate_text("compute the answer", context_id="ctx-xyz")

    assert isinstance(result, Task)
    assert result.id == "task-abc"
    assert result.state == TASK_STATE_COMPLETED
    assert result.result_text() == "the answer"
    # The context id must have ridden along on the outbound message.
    assert transport.calls[0]["body"]["params"]["message"]["contextId"] == "ctx-xyz"
    assert transport.calls[0]["body"]["params"]["message"]["role"] == ROLE_USER


# ── result parsing variants ────────────────────────────────────────────────────


def test_parse_result_bare_message():
    transport = _RecordingTransport(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"message": {"role": "agent", "parts": [{"text": "inline reply"}]}},
        }
    )
    client = A2AClient("https://peer.example.com/a2a", transport=transport)
    result = client.delegate_text("ping")
    assert isinstance(result, Message)
    assert result.text() == "inline reply"


def test_parse_result_unnested_task_shape():
    # REST-style peers may return the Task object directly under result.
    transport = _RecordingTransport(
        {"jsonrpc": "2.0", "id": 1, "result": {"id": "t9", "status": {"state": "working"}}}
    )
    client = A2AClient("https://peer.example.com/a2a", transport=transport)
    result = client.delegate_text("go")
    assert isinstance(result, Task)
    assert result.id == "t9"


def test_parse_result_unnested_message_shape():
    transport = _RecordingTransport(
        {"jsonrpc": "2.0", "id": 1, "result": {"role": "agent", "parts": [{"text": "hey"}]}}
    )
    client = A2AClient("https://peer.example.com/a2a", transport=transport)
    result = client.delegate_text("go")
    assert isinstance(result, Message)
    assert result.text() == "hey"


# ── error handling ─────────────────────────────────────────────────────────────


def test_jsonrpc_error_raises_client_error():
    transport = _RecordingTransport(
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "Method not found"}}
    )
    client = A2AClient("https://peer.example.com/a2a", transport=transport)
    with pytest.raises(A2AClientError, match="-32601"):
        client.delegate_text("go")


def test_missing_result_raises_client_error():
    transport = _RecordingTransport({"jsonrpc": "2.0", "id": 1})
    client = A2AClient("https://peer.example.com/a2a", transport=transport)
    with pytest.raises(A2AClientError, match="missing a result"):
        client.delegate_text("go")


def test_empty_endpoint_url_rejected():
    with pytest.raises(ValueError):
        A2AClient("")


# ── agent card discovery ───────────────────────────────────────────────────────


def test_fetch_agent_card_uses_well_known_path():
    captured = {}

    def fake_card_fetch(url, headers, timeout):
        captured["url"] = url
        return {
            "name": "Peer Agent",
            "description": "d",
            "version": "1.0.0",
            "url": "https://peer.example.com/a2a/v1",
            "skills": [],
        }

    client = A2AClient("https://peer.example.com/a2a/v1", card_fetch=fake_card_fetch)
    card = client.fetch_agent_card()
    assert card.name == "Peer Agent"
    assert captured["url"] == "https://peer.example.com/.well-known/agent-card.json"


def test_fetch_agent_card_accepts_explicit_url():
    def fake_card_fetch(url, headers, timeout):
        assert url == "https://elsewhere.example.com/card.json"
        return {"name": "P", "description": "", "version": "1", "url": "u", "skills": []}

    client = A2AClient("https://peer.example.com/a2a", card_fetch=fake_card_fetch)
    client.fetch_agent_card(card_url="https://elsewhere.example.com/card.json")


def test_well_known_card_url_derivation():
    assert (
        _well_known_card_url("https://host.example.com:8080/a2a/v1?x=1")
        == "https://host.example.com:8080/.well-known/agent-card.json"
    )


def test_well_known_card_url_rejects_invalid_endpoint():
    with pytest.raises(A2AClientError):
        _well_known_card_url("not-a-url")


# ── feature flag gating ─────────────────────────────────────────────────────────


def test_is_enabled_off_by_default(monkeypatch):
    monkeypatch.delenv("HERMES_A2A_BRIDGE", raising=False)
    assert a2a_bridge.is_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE"])
def test_is_enabled_truthy_values(monkeypatch, value):
    monkeypatch.setenv("HERMES_A2A_BRIDGE", value)
    assert a2a_bridge.is_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_is_enabled_falsy_values(monkeypatch, value):
    monkeypatch.setenv("HERMES_A2A_BRIDGE", value)
    assert a2a_bridge.is_enabled() is False
