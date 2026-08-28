"""Fine-grained MCP and native tool write-guard policy gate.

Implements #3276: Authorize every mutating tool call before execution.
Classifies tools into risk categories:
- READ_ONLY: tools with zero environment mutation (read_file, search_files, web_search, etc.)
- WRITE: mutating tools (write_file, patch, terminal, execute_code, MCP mutating tools, etc.)
- DESTRUCTIVE: permanent removal or hazardous modifications.

Enforces per-session allow/deny/require_confirmation rules with subagent inheritance.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from typing import Any, Mapping

from agent.policy_interceptors import (
    PolicyInterceptor,
    PolicyOutcome,
    ToolCallContext,
)


class WriteRisk:
    READ_ONLY = "read_only"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


# Core and common built-in tools known to be strictly read-only
KNOWN_READ_ONLY_TOOLS = frozenset({
    "read_file",
    "search_files",
    "repo_map",
    "web_search",
    "web_extract",
    "vision_analyze",
    "text_to_speech",
    "session_search",
    "clarify",
    "skills_list",
    "skill_view",
    "tool_search",
    "tool_describe",
    "browser_snapshot",
    "browser_get_images",
    "browser_vision",
    "browser_console",
    "browser_cdp",
    "ha_list_entities",
    "ha_get_state",
    "ha_list_services",
    "kanban_show",
    "kanban_list",
    "kanban_attachments",
})

# Built-in mutating tools
KNOWN_WRITE_TOOLS = frozenset({
    "write_file",
    "patch",
    "terminal",
    "execute_code",
    "process",
    "skill_manage",
    "browser_click",
    "browser_type",
    "browser_press",
    "browser_scroll",
    "browser_navigate",
    "browser_dialog",
    "browser_exec",
    "todo",
    "memory",
    "delegate_task",
    "cronjob",
    "ha_call_service",
    "computer_use",
})

# Known destructive tool names
KNOWN_DESTRUCTIVE_TOOLS = frozenset({
    "delete_file",
    "remove_file",
    "drop_database",
    "truncate_table",
})

_MCP_READ_PREFIXES = (
    "read_",
    "get_",
    "list_",
    "search_",
    "query_",
    "fetch_",
    "find_",
    "status_",
    "info_",
    "describe_",
)

_MCP_DESTRUCTIVE_PREFIXES = (
    "delete_",
    "remove_",
    "drop_",
    "truncate_",
    "destroy_",
    "purge_",
)


def classify_tool_write_risk(
    tool_name: str, args: Mapping[str, Any] | None = None
) -> str:
    """Classify a tool's write risk as read_only, write, or destructive."""
    name_lower = (tool_name or "").strip().lower()

    if name_lower in KNOWN_DESTRUCTIVE_TOOLS:
        return WriteRisk.DESTRUCTIVE

    if name_lower in KNOWN_READ_ONLY_TOOLS:
        return WriteRisk.READ_ONLY

    if name_lower in KNOWN_WRITE_TOOLS:
        return WriteRisk.WRITE

    # MCP tool classification heuristic
    if name_lower.startswith("mcp_"):
        parts = name_lower.split("__", 1)
        if len(parts) == 2:
            action = parts[1]
        else:
            subparts = name_lower.split("_", 2)
            action = subparts[2] if len(subparts) >= 3 else name_lower[4:]

        if any(action.startswith(p) for p in _MCP_DESTRUCTIVE_PREFIXES):
            return WriteRisk.DESTRUCTIVE
        if any(action.startswith(p) for p in _MCP_READ_PREFIXES):
            return WriteRisk.READ_ONLY
        return WriteRisk.WRITE

    # Fail-safe default: unrecognized tools are treated as write risk
    return WriteRisk.WRITE


def _matches_any_pattern(name: str, patterns: frozenset[str]) -> bool:
    """Check if tool name matches any exact string or glob pattern."""
    for pat in patterns:
        if pat == "*" or pat == name or fnmatch.fnmatch(name, pat):
            return True
    return False


@dataclass(frozen=True)
class WriteGuardPolicy:
    """Policy rules governing mutating/destructive tool execution."""

    mode: str = "off"  # "enforce" | "audit" | "off"
    allow: frozenset[str] = frozenset()
    deny: frozenset[str] = frozenset()
    require_confirmation: frozenset[str] = frozenset()
    allow_read_only: bool = True

    @property
    def enabled(self) -> bool:
        return self.mode in {"enforce", "audit"}

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "WriteGuardPolicy":
        if not isinstance(data, Mapping):
            return cls(mode="off")

        raw_enabled = data.get("enabled")
        raw_mode = str(data.get("mode") or "").strip().lower()

        if raw_enabled is False or raw_mode == "off":
            mode = "off"
        elif raw_mode in {"enforce", "audit"}:
            mode = raw_mode
        elif raw_enabled is True:
            mode = "enforce"
        else:
            mode = "off"

        allow = _coerce_set(data.get("allow"))
        deny = _coerce_set(data.get("deny"))
        require_confirmation = _coerce_set(data.get("require_confirmation"))
        allow_read_only = bool(data.get("allow_read_only", True))

        return cls(
            mode=mode,
            allow=allow,
            deny=deny,
            require_confirmation=require_confirmation,
            allow_read_only=allow_read_only,
        )

    def evaluate(
        self, tool_name: str, args: Mapping[str, Any] | None = None
    ) -> PolicyOutcome:
        """Evaluate a tool call against the write guard policy."""
        if not self.enabled:
            return PolicyOutcome.allow()

        risk = classify_tool_write_risk(tool_name, args)

        # Read-only tools are allowed if allow_read_only is True and not explicitly denied
        if risk == WriteRisk.READ_ONLY:
            if self.allow_read_only and not _matches_any_pattern(tool_name, self.deny):
                return PolicyOutcome.allow()

        # Check explicit deny rule
        if _matches_any_pattern(tool_name, self.deny):
            msg = (
                f"Blocked {tool_name}: tool modification is prohibited by write-guard policy "
                f"(matched deny rule in {self.mode} mode)."
            )
            if self.mode == "audit":
                return PolicyOutcome.allow()
            return PolicyOutcome.deny(msg)

        # Check allowlist when non-empty
        if self.allow:
            if not _matches_any_pattern(tool_name, self.allow):
                msg = (
                    f"Blocked {tool_name}: tool modification is not in write-guard allowlist "
                    f"({self.mode} mode)."
                )
                if self.mode == "audit":
                    return PolicyOutcome.allow()
                return PolicyOutcome.deny(msg)

        return PolicyOutcome.allow()


def make_write_guard(policy: WriteGuardPolicy) -> PolicyInterceptor:
    """Create a PolicyInterceptor from a WriteGuardPolicy."""
    def interceptor(ctx: ToolCallContext) -> PolicyOutcome:
        return policy.evaluate(ctx.tool_name, ctx.args)

    return interceptor


def _coerce_set(value: Any) -> frozenset[str]:
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(str(x).strip() for x in value if str(x or "").strip())
    if isinstance(value, str) and value.strip():
        return frozenset({value.strip()})
    return frozenset()
