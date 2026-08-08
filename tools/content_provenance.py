#!/usr/bin/env python3
"""Content provenance tagging — trust-level metadata for external content
(#1799, parent #1659).

Every piece of content entering the agent's context from an external source
(web search results, fetched pages, MCP tool outputs) carries a trust-level
tag so downstream consumers (the LLM, safety filters, the Task Shield) can
distinguish trusted from untrusted content and apply appropriate scrutiny.

Three trust levels, in decreasing order:

  - ``user``         — typed by the user (most trusted; can carry instructions)
  - ``tool-invoked`` — fetched via a tool the agent deliberately invoked
                       (search results, file reads, MCP outputs the agent
                       chose to call)
  - ``external``     — embedded inside untrusted content (e.g., text from a
                       web page the agent fetched — could contain prompt
                       injection, cross-site data, etc.)

The tagging is applied at content entry points. This module provides:

  1. ``ProvenanceTag`` — immutable metadata for a content chunk.
  2. ``tag_content()`` — wrap raw content with its provenance.
  3. ``wrap_external()`` — convenience to delimit external content so the
     LLM and safety filters can recognize the trust boundary.
  4. ``TaggingRegistry`` — context-level registry that accumulates tags for
     a conversation turn (supports the multi-skill composition tracer and
     other consumers that need to audit what content entered the context).

Import-safe — no side effects on import. All functions are pure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Trust levels, highest → lowest.
TRUST_USER = "user"
TRUST_TOOL = "tool-invoked"
TRUST_EXTERNAL = "external"

_TRUST_RANK = {
    TRUST_USER: 3,
    TRUST_TOOL: 2,
    TRUST_EXTERNAL: 1,
}

# Sources that map to each trust level.
_USER_SOURCES = frozenset({"user", "human", "prompt"})
_TOOL_SOURCES = frozenset({
    "web_search", "web_extract", "file_read", "mcp_tool", "terminal",
    "browser", "search", "fetch", "tool",
})


@dataclass(frozen=True)
class ProvenanceTag:
    """Immutable provenance metadata for a content chunk."""

    trust_level: str
    source: str          # e.g. "web_search", "mcp_tool:filesystem", "user"
    url: str = ""        # original URL for fetched content
    fetched_at: str = "" # ISO timestamp when content was retrieved
    tool_call_id: str = ""  # associates with a tool invocation, if any

    @property
    def is_external(self) -> bool:
        return self.trust_level == TRUST_EXTERNAL

    @property
    def rank(self) -> int:
        return _TRUST_RANK.get(self.trust_level, 0)


def resolve_trust_level(source: str) -> str:
    """Map a source identifier to a trust level.

    User-typed content is always ``user``. Content from agent-invoked tools
    (web_search, web_extract, file reads, MCP tools) is ``tool-invoked``.
    Content embedded *within* fetched external content (text from a web page)
    is ``external`` — the lowest trust, because it may contain injected
    instructions. Unknown sources default to ``external`` (fail-safe).
    """
    if not source:
        return TRUST_EXTERNAL
    normalized = source.lower().strip()
    for s in _USER_SOURCES:
        if normalized == s or normalized.startswith(s + ":"):
            return TRUST_USER
    for s in _TOOL_SOURCES:
        if normalized == s or normalized.startswith(s + ":"):
            return TRUST_TOOL
    return TRUST_EXTERNAL


def tag_content(
    content: str,
    source: str,
    *,
    url: str = "",
    tool_call_id: str = "",
    trust_level: Optional[str] = None,
) -> "TaggedContent":
    """Wrap raw content with its provenance tag.

    If ``trust_level`` is not provided, it is resolved from ``source``.
    """
    level = trust_level or resolve_trust_level(source)
    tag = ProvenanceTag(
        trust_level=level,
        source=source,
        url=url,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        tool_call_id=tool_call_id,
    )
    return TaggedContent(content=content, provenance=tag)


@dataclass
class TaggedContent:
    """Content paired with its provenance metadata."""

    content: str
    provenance: ProvenanceTag

    def render(self) -> str:
        """Render content with a trust-boundary delimiter for external content.

        External content is wrapped in ``<untrusted-content>`` markers so the
        LLM and safety filters can distinguish it from trusted sources.
        Higher-trust content is returned as-is.
        """
        if self.provenance.is_external:
            src = self.provenance.source
            url_part = f" url={self.provenance.url}" if self.provenance.url else ""
            return (
                f"<untrusted-content source=\"{src}\"{url_part}>\n"
                f"{self.content}\n"
                f"</untrusted-content>"
            )
        return self.content


def wrap_external(content: str, source: str, url: str = "") -> str:
    """Convenience: tag and render external content in one call."""
    return tag_content(content, source, url=url, trust_level=TRUST_EXTERNAL).render()


class TaggingRegistry:
    """Accumulates provenance tags for a conversation turn.

    Used by content-entry-point wrappers to record what external content
    entered the context, so downstream consumers (composition tracer, safety
    filters, audit logs) can inspect the full set of trust tags.
    """

    def __init__(self) -> None:
        self._entries: List[TaggedContent] = []

    def add(self, content: str, source: str, **kwargs: Any) -> TaggedContent:
        """Tag and register a content chunk. Returns the TaggedContent."""
        tc = tag_content(content, source, **kwargs)
        self._entries.append(tc)
        return tc

    @property
    def entries(self) -> List[TaggedContent]:
        return list(self._entries)

    def external_sources(self) -> List[str]:
        """Distinct source identifiers of all external content seen."""
        return sorted({
            tc.provenance.source for tc in self._entries
            if tc.provenance.is_external
        })

    def has_external_content(self) -> bool:
        return any(tc.provenance.is_external for tc in self._entries)

    def min_trust_level(self) -> Optional[str]:
        """Lowest trust level among all registered content (or None)."""
        if not self._entries:
            return None
        return min(self._entries, key=lambda tc: tc.provenance.rank).provenance.trust_level

    def clear(self) -> None:
        self._entries.clear()
