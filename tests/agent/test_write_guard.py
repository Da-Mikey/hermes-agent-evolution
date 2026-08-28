"""Tests for fine-grained MCP and native tool write-guard policy gate (Issue #3276)."""

from agent.policy_interceptors import (
    PolicyInterceptorRegistry,
    RegisteredPolicy,
    build_registry_from_config,
)
from agent.write_guard import (
    WriteGuardPolicy,
    WriteRisk,
    classify_tool_write_risk,
    make_write_guard,
)


def test_classify_tool_write_risk_builtins():
    # Read-only tools
    assert classify_tool_write_risk("read_file") == WriteRisk.READ_ONLY
    assert classify_tool_write_risk("search_files") == WriteRisk.READ_ONLY
    assert classify_tool_write_risk("web_search") == WriteRisk.READ_ONLY
    assert classify_tool_write_risk("browser_snapshot") == WriteRisk.READ_ONLY

    # Mutating write tools
    assert classify_tool_write_risk("write_file") == WriteRisk.WRITE
    assert classify_tool_write_risk("patch") == WriteRisk.WRITE
    assert classify_tool_write_risk("terminal") == WriteRisk.WRITE
    assert classify_tool_write_risk("execute_code") == WriteRisk.WRITE
    assert classify_tool_write_risk("process") == WriteRisk.WRITE

    # Destructive tools
    assert classify_tool_write_risk("delete_file") == WriteRisk.DESTRUCTIVE
    assert classify_tool_write_risk("truncate_table") == WriteRisk.DESTRUCTIVE


def test_classify_tool_write_risk_mcp_tools():
    # MCP read-only conventions
    assert classify_tool_write_risk("mcp_filesystem_read_file") == WriteRisk.READ_ONLY
    assert classify_tool_write_risk("mcp_db__list_tables") == WriteRisk.READ_ONLY
    assert classify_tool_write_risk("mcp_server_query_metrics") == WriteRisk.READ_ONLY

    # MCP mutating conventions
    assert classify_tool_write_risk("mcp_filesystem_write_file") == WriteRisk.WRITE
    assert classify_tool_write_risk("mcp_db__update_record") == WriteRisk.WRITE
    assert classify_tool_write_risk("mcp_api_post_data") == WriteRisk.WRITE

    # MCP destructive conventions
    assert classify_tool_write_risk("mcp_filesystem_delete_file") == WriteRisk.DESTRUCTIVE
    assert classify_tool_write_risk("mcp_db__drop_table") == WriteRisk.DESTRUCTIVE
    assert classify_tool_write_risk("mcp_storage_purge_cache") == WriteRisk.DESTRUCTIVE


def test_write_guard_mode_off_allows_all():
    policy = WriteGuardPolicy.from_mapping({
        "enabled": False,
        "deny": ["terminal", "write_file"],
    })
    assert policy.enabled is False
    assert policy.evaluate("terminal").action == "allow"
    assert policy.evaluate("write_file").action == "allow"


def test_write_guard_deny_blocks_matching_mutating_tools():
    policy = WriteGuardPolicy.from_mapping({
        "enabled": True,
        "mode": "enforce",
        "deny": ["terminal", "mcp_filesystem_delete*"],
    })
    assert policy.enabled is True

    # Denied tool
    blocked = policy.evaluate("terminal")
    assert blocked.action == "deny"
    assert "Blocked terminal" in blocked.message

    # Denied glob pattern
    blocked_mcp = policy.evaluate("mcp_filesystem_delete_file")
    assert blocked_mcp.action == "deny"
    assert "Blocked mcp_filesystem_delete_file" in blocked_mcp.message

    # Non-denied mutating tool
    allowed_write = policy.evaluate("patch")
    assert allowed_write.action == "allow"

    # Read-only tool is allowed
    allowed_read = policy.evaluate("read_file")
    assert allowed_read.action == "allow"


def test_write_guard_allowlist_restricts_mutating_tools():
    policy = WriteGuardPolicy.from_mapping({
        "enabled": True,
        "mode": "enforce",
        "allow": ["patch", "write_file"],
    })

    # In allowlist
    assert policy.evaluate("patch").action == "allow"
    assert policy.evaluate("write_file").action == "allow"

    # Mutating tool not in allowlist is blocked
    blocked = policy.evaluate("terminal")
    assert blocked.action == "deny"
    assert "not in write-guard allowlist" in blocked.message

    # Read-only tool not in allowlist is still allowed by default
    assert policy.evaluate("read_file").action == "allow"


def test_write_guard_audit_mode_does_not_block():
    policy = WriteGuardPolicy.from_mapping({
        "enabled": True,
        "mode": "audit",
        "deny": ["terminal"],
    })
    assert policy.enabled is True
    # Audit mode allows execution
    assert policy.evaluate("terminal").action == "allow"


def test_write_guard_policy_interceptor_registry_integration():
    cfg = {
        "enabled": True,
        "policies": [
            {
                "name": "mcp-write-guard",
                "policy": "write_guard",
                "options": {
                    "mode": "enforce",
                    "deny": ["terminal", "execute_code"],
                },
            }
        ],
    }
    registry = build_registry_from_config(cfg)
    assert registry.enabled is True

    decision = registry.evaluate("terminal", {"command": "ls"})
    assert decision.allows_execution is False
    assert decision.action == "block"
    assert decision.code == "policy_deny:mcp-write-guard"

    allowed_decision = registry.evaluate("read_file", {"path": "a.txt"})
    assert allowed_decision.allows_execution is True
    assert allowed_decision.action == "allow"
