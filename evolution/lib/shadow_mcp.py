# -*- coding: utf-8 -*-
"""Shadow-MCP governance — outbound endpoint visibility + approval (#90).

The local counterpart to Cloudflare's 2026-08-14 Gateway ``shadow-MCP``
selector (``experimental.is_mcp``). Enterprise gateways can now see and block
*unapproved* MCP connections at the network layer; the local agent has no such
view of what its MCP/tool traffic actually touches. This module provides that
visibility without a gateway:

1. **Contact log** — every outbound MCP/tool endpoint contact is recorded
   (endpoint, owning server, contact count, first/last-seen).
2. **Config-driven allow/deny** — a ``shadow_mcp`` policy in ``config.yaml``
   declares which endpoints are approved, blocked, or (by default) simply
   observed.
3. **Alert on unapproved contact** — a contact outside the allow list raises
   a warning-level alert; a contact on the deny list is a denial.
4. **Audit digest** — the contact log and its unapproved subset are exposed
   for the audit channel (see ``scripts/shadow_mcp_audit.py``).

Policy (``config.yaml``)::

    shadow_mcp:
      enabled: true          # default true — log every outbound endpoint contact
      allow: []              # empty = allow-all (observe only, never block)
      deny: []               # explicit blocklist (deny verdict)

Verbs returned by :meth:`ShadowMcpGovernor.record_contact`:

* ``allow``      — approved (allow list empty, or endpoint matches it; and not denied).
* ``alert``      — unapproved (allow list non-empty and endpoint not on it). Logged, not blocked.
* ``deny``       — blocked (endpoint on the deny list). The caller should refuse the connection.

The default (empty allow/deny) is **fail-open**: it observes everything and
blocks nothing, so enabling the feature never breaks existing MCP servers.
Tightening to ``allow: [ ... ]`` opts into alerting; ``deny: [ ... ]`` opts
into blocking.

This module is import-safe and dependency-light (stdlib only) so the MCP
dispatch path can import it without pulling in the agent core.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

__all__ = [
    "ALLOW",
    "ALERT",
    "DENY",
    "EndpointContact",
    "ShadowMcpPolicy",
    "ShadowMcpGovernor",
    "endpoint_host",
    "endpoint_matches",
    "default_log_path",
    "get_governor",
]

ALLOW = "allow"
ALERT = "alert"
DENY = "deny"

# Endpoints that never need shadow governance: loopback is local, and a stdio
# command has no network endpoint to approve.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def endpoint_host(endpoint: str) -> str:
    """Return the ``host[:port]`` (netloc) of a URL, or the raw string.

    Non-URL strings (e.g. a bare hostname) are returned unchanged and lower-
    cased so callers can still match them against policy entries.
    """
    if not endpoint:
        return ""
    parsed = urlparse(endpoint)
    if parsed.netloc:
        return parsed.netloc.lower()
    return endpoint.lower()


def endpoint_matches(entry: str, endpoint: str) -> bool:
    """True when a policy *entry* matches an *endpoint* URL or host.

    Match rules, in order:

    * full-URL entry (contains ``://``) — prefix match against the endpoint URL.
    * ``*.example.com`` — wildcard: matches ``example.com`` and any subdomain
      (``api.example.com``, ``www.deep.example.com``) and their ``host:port``
      forms.
    * ``example.com`` — exact host match, ignoring any port and scheme.
    """
    entry = (entry or "").strip().lower()
    endpoint = (endpoint or "").strip()
    if not entry or not endpoint:
        return False

    if "://" in entry:
        return endpoint.lower().startswith(entry)

    host = endpoint_host(endpoint)
    # Strip a port from the entry if present, then compare host without port.
    entry_host = entry.split(":")[0] if ":" in entry else entry
    host_no_port = host.split(":")[0] if ":" in host else host

    if entry_host.startswith("*."):
        suffix = entry_host[2:]
        if not suffix:
            return False
        if host_no_port == suffix:
            return True
        return host_no_port.endswith("." + suffix)
    return host_no_port == entry_host


def default_log_path() -> Optional[Path]:
    """Best-effort default on-disk contact-log path, or ``None`` if unavailable."""
    try:
        from hermes_constants import get_hermes_home  # noqa: PLC0415

        return get_hermes_home() / "logs" / "shadow_mcp.jsonl"
    except Exception:
        return None


@dataclass
class EndpointContact:
    """Aggregate of contacts to a single (server, endpoint) pair."""

    server: str
    endpoint: str
    host: str = ""
    count: int = 0
    first_seen: str = ""
    last_seen: str = ""
    last_verdict: str = ALLOW

    def __post_init__(self) -> None:
        self.host = self.host or endpoint_host(self.endpoint)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EndpointContact":
        return cls(
            server=str(d.get("server", "")),
            endpoint=str(d.get("endpoint", "")),
            host=str(d.get("host", "")),
            count=int(d.get("count", 0)),
            first_seen=str(d.get("first_seen", "")),
            last_seen=str(d.get("last_seen", "")),
            last_verdict=str(d.get("last_verdict", ALLOW)),
        )


@dataclass
class ShadowMcpPolicy:
    """Config-driven allow/deny policy for outbound endpoints.

    ``allow`` and ``deny`` are lists of endpoint entries (host, wildcard
    ``*.host``, or full-URL prefix). Empty ``allow`` means allow-all
    (observe-only). Entries are matched via :func:`endpoint_matches`.
    """

    allow: List[str] = field(default_factory=list)
    deny: List[str] = field(default_factory=list)
    enabled: bool = True

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "ShadowMcpPolicy":
        """Build a policy from a ``shadow_mcp`` config mapping (or ``None``)."""
        if not isinstance(config, dict):
            return cls()
        allow = config.get("allow") or []
        deny = config.get("deny") or []
        enabled = bool(config.get("enabled", True))
        return cls(
            allow=[str(a) for a in allow] if isinstance(allow, (list, tuple)) else [],
            deny=[str(d) for d in deny] if isinstance(deny, (list, tuple)) else [],
            enabled=enabled,
        )

    def evaluate(self, endpoint: str) -> str:
        """Return ``allow`` / ``alert`` / ``deny`` for an endpoint."""
        if any(endpoint_matches(entry, endpoint) for entry in self.deny):
            return DENY
        if not self.allow:
            return ALLOW
        if any(endpoint_matches(entry, endpoint) for entry in self.allow):
            return ALLOW
        return ALERT


class ShadowMcpGovernor:
    """Runtime governor: records contacts, applies policy, alerts, and audits.

    Keeps an in-memory aggregate keyed by ``(server, endpoint)`` and, when
    ``log_path`` is set, appends each contact as a JSONL line for the audit
    channel. The caller (the MCP dispatch path) is responsible for *acting* on
    a ``deny`` verdict (refusing the connection); this class only reports.
    """

    def __init__(
        self,
        policy: Optional[ShadowMcpPolicy] = None,
        log_path: Optional[Path | str] = None,
        alerter: Optional[Callable[[str, EndpointContact], None]] = None,
    ) -> None:
        self.policy = policy or ShadowMcpPolicy()
        self.log_path = Path(log_path) if log_path else None
        self._alerter = alerter
        self._contacts: Dict[str, EndpointContact] = {}

    def record_contact(self, server: str, endpoint: str) -> str:
        """Record a contact, return its verdict (``allow``/``alert``/``deny``).

        Always logs the contact when the policy is enabled. Emits a
        warning-level alert (and invokes the optional alerter) when the
        verdict is ``alert`` or ``deny``.
        """
        key = f"{server}\x00{endpoint}"
        contact = self._contacts.get(key)
        now = _now_iso()

        if contact is None:
            verdict = self.policy.evaluate(endpoint) if self.policy.enabled else ALLOW
            contact = EndpointContact(
                server=str(server),
                endpoint=str(endpoint),
                count=1,
                first_seen=now,
                last_seen=now,
                last_verdict=verdict,
            )
            self._contacts[key] = contact
        else:
            verdict = self.policy.evaluate(endpoint) if self.policy.enabled else ALLOW
            contact.count += 1
            contact.last_seen = now
            contact.last_verdict = verdict

        if self.policy.enabled:
            self._append_disk(contact)
            if verdict in (ALERT, DENY):
                self._alert(verdict, contact)
        return verdict

    def _alert(self, verdict: str, contact: EndpointContact) -> None:
        logger.warning(
            "shadow-mcp: %s outbound endpoint contact with %s (%s) via server '%s'",
            verdict,
            contact.endpoint,
            contact.host,
            contact.server,
        )
        if self._alerter is not None:
            try:
                self._alerter(verdict, contact)
            except Exception:  # pragma: no cover - alerting must never break dispatch
                logger.debug("shadow-mcp alerter failed", exc_info=True)

    def _append_disk(self, contact: EndpointContact) -> None:
        path = self.log_path
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(contact.to_dict()) + "\n")
        except OSError as exc:
            logger.debug("shadow-mcp disk append failed: %s", exc)

    def audit_digest(self) -> List[Dict[str, Any]]:
        """All recorded contacts, most-recently-seen first."""
        contacts = list(self._contacts.values())
        contacts.sort(key=lambda c: c.last_seen, reverse=True)
        return [c.to_dict() for c in contacts]

    def unapproved(self) -> List[Dict[str, Any]]:
        """Contacts whose last verdict was ``alert`` or ``deny``."""
        return [
            c.to_dict()
            for c in self._contacts.values()
            if c.last_verdict in (ALERT, DENY)
        ]

    def clear(self) -> None:
        """Drop all in-memory contacts (session boundary)."""
        self._contacts.clear()


_governor: Optional[ShadowMcpGovernor] = None


def get_governor() -> ShadowMcpGovernor:
    """Return the process-wide governor, lazily built from the active config."""
    global _governor
    if _governor is None:
        policy = ShadowMcpPolicy()
        try:
            from hermes_cli.config import load_config  # noqa: PLC0415

            policy = ShadowMcpPolicy.from_config(load_config().get("shadow_mcp"))
        except Exception:
            logger.debug("shadow-mcp config load failed; using default policy", exc_info=True)
        _governor = ShadowMcpGovernor(policy=policy, log_path=default_log_path())
    return _governor


# Retained for reference: a caller can reset the singleton (e.g. tests).
def _reset_governor() -> None:
    global _governor
    _governor = None
