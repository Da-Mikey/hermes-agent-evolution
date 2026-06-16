"""Outbound A2A client — delegate a task to a peer A2A agent.

This is the client half of issue #227's plan step 3: serialize a request into
the canonical A2A ``message/send`` JSON-RPC 2.0 envelope, POST it to a peer's
endpoint, and parse the ``result`` (a :class:`~a2a_bridge.models.Task` or a
bare :class:`~a2a_bridge.models.Message`) into typed objects the delegating
agent can act on.

Design notes:

* Transport is injectable (``transport=`` callable). The default uses
  ``httpx`` (already a core dependency). Tests pass a fake transport so the
  whole request/response round-trip is exercised offline, with no network and
  no running peer.
* No import-time side effects and nothing wired into the live tool dispatcher,
  so importing this module cannot affect MCP tool-call latency. Callers must
  gate use behind :func:`a2a_bridge.is_enabled`.
* Errors map to a single :class:`A2AClientError` so callers don't have to know
  about httpx or JSON-RPC error codes.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Callable, Optional

from a2a_bridge.models import (
    AgentCard,
    Message,
    ROLE_USER,
    Task,
)

# A transport takes (url, json_body, headers, timeout) and returns the decoded
# JSON-RPC response dict. Kept as a plain callable so tests can inject a fake.
Transport = Callable[[str, dict[str, Any], dict[str, str], float], dict[str, Any]]

DEFAULT_TIMEOUT = 60.0
# Canonical A2A JSON-RPC method for sending a message (spec >= 0.2).
METHOD_MESSAGE_SEND = "message/send"
# Well-known path for agent discovery (spec >= 0.3).
AGENT_CARD_PATH = "/.well-known/agent-card.json"


class A2AClientError(RuntimeError):
    """Raised when an A2A request fails (transport, protocol, or peer error)."""


def _default_transport(
    url: str, body: dict[str, Any], headers: dict[str, str], timeout: float
) -> dict[str, Any]:
    """httpx-backed POST that returns the decoded JSON body.

    httpx is imported lazily so this module stays importable in minimal
    environments and import time never pays for the HTTP stack.
    """
    import httpx

    try:
        resp = httpx.post(url, json=body, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise A2AClientError(f"A2A transport error contacting {url}: {exc}") from exc
    if resp.status_code >= 400:
        raise A2AClientError(
            f"A2A peer {url} returned HTTP {resp.status_code}: {resp.text[:500]}"
        )
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise A2AClientError(f"A2A peer {url} returned non-JSON response: {exc}") from exc


def _default_card_fetch(url: str, headers: dict[str, str], timeout: float) -> dict[str, Any]:
    import httpx

    try:
        resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise A2AClientError(f"A2A card fetch error for {url}: {exc}") from exc
    if resp.status_code >= 400:
        raise A2AClientError(
            f"A2A card endpoint {url} returned HTTP {resp.status_code}"
        )
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError) as exc:
        raise A2AClientError(f"A2A card endpoint {url} returned non-JSON: {exc}") from exc


class A2AClient:
    """A thin synchronous client for delegating tasks to one A2A peer."""

    def __init__(
        self,
        endpoint_url: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        headers: Optional[dict[str, str]] = None,
        transport: Optional[Transport] = None,
        card_fetch: Optional[Callable[[str, dict[str, str], float], dict[str, Any]]] = None,
    ) -> None:
        if not endpoint_url or not str(endpoint_url).strip():
            raise ValueError("endpoint_url is required")
        self.endpoint_url = str(endpoint_url).strip()
        self.timeout = float(timeout)
        self.headers = {"content-type": "application/json", **(headers or {})}
        self._transport = transport or _default_transport
        self._card_fetch = card_fetch or _default_card_fetch

    # -- discovery ---------------------------------------------------------

    def fetch_agent_card(self, *, card_url: Optional[str] = None) -> AgentCard:
        """Fetch and parse the peer's AgentCard.

        Defaults to the well-known discovery path relative to the endpoint's
        origin. A peer that serves its card elsewhere can be reached by passing
        ``card_url`` explicitly.
        """
        target = card_url or _well_known_card_url(self.endpoint_url)
        raw = self._card_fetch(target, self.headers, self.timeout)
        if not isinstance(raw, dict):
            raise A2AClientError(f"A2A card at {target} was not a JSON object")
        return AgentCard.from_dict(raw)

    # -- task delegation ---------------------------------------------------

    def send_message(self, message: Message, *, request_id: Optional[Any] = None) -> Task | Message:
        """Send a Message and return the peer's result.

        Returns a :class:`Task` when the peer tracks the work as a task (the
        common delegation case), or a bare :class:`Message` when the peer
        answers inline. Raises :class:`A2AClientError` on any transport or
        JSON-RPC error.
        """
        rpc_id = request_id if request_id is not None else uuid.uuid4().hex
        body = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": METHOD_MESSAGE_SEND,
            "params": {"message": message.to_dict()},
        }
        response = self._transport(self.endpoint_url, body, self.headers, self.timeout)
        return self._parse_rpc_result(response)

    def delegate_text(
        self, text: str, *, context_id: Optional[str] = None, request_id: Optional[Any] = None
    ) -> Task | Message:
        """Convenience: delegate a plain-text task and get the result back."""
        message = Message.from_text(text, role=ROLE_USER, message_id=uuid.uuid4().hex)
        if context_id is not None:
            message.context_id = context_id
        return self.send_message(message, request_id=request_id)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _parse_rpc_result(response: dict[str, Any]) -> Task | Message:
        if not isinstance(response, dict):
            raise A2AClientError("A2A response was not a JSON-RPC object")

        error = response.get("error")
        if error is not None:
            if isinstance(error, dict):
                code = error.get("code")
                msg = error.get("message")
                raise A2AClientError(f"A2A peer returned JSON-RPC error {code}: {msg}")
            raise A2AClientError(f"A2A peer returned JSON-RPC error: {error}")

        result = response.get("result")
        if not isinstance(result, dict):
            raise A2AClientError("A2A response missing a result object")

        # A2A nests the payload under "task"/"message"; some peers (and the
        # spec's REST binding) return the Task/Message object directly. Handle
        # both: explicit nesting wins, then fall back to shape detection.
        if isinstance(result.get("task"), dict):
            return Task.from_dict(result["task"])
        if isinstance(result.get("message"), dict):
            return Message.from_dict(result["message"])

        kind = result.get("kind")
        if kind == "message" or ("role" in result and "parts" in result and "status" not in result):
            return Message.from_dict(result)
        # Default: treat as a Task (it has id/status in the common case).
        return Task.from_dict(result)


def _well_known_card_url(endpoint_url: str) -> str:
    """Derive the well-known AgentCard URL from a peer endpoint's origin."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(endpoint_url)
    if not parts.scheme or not parts.netloc:
        raise A2AClientError(f"Cannot derive card URL from invalid endpoint {endpoint_url!r}")
    return urlunsplit((parts.scheme, parts.netloc, AGENT_CARD_PATH, "", ""))
