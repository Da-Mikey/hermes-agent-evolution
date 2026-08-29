"""Tests for the #3300 write-guard increments: destructive default-deny,
require_confirmation enforcement, and the decision audit log."""
from __future__ import annotations

import json
from pathlib import Path

from agent.write_guard import (
    WriteGuardPolicy,
    WriteRisk,
    audit_write_operation,
    classify_tool_write_risk,
)


def test_destructive_defaults_to_deny_when_enabled():
    """#3300: with the guard enabled and no explicit allow, a destructive op
    is denied — destructive MCP ops must never fail open."""
    policy = WriteGuardPolicy.from_mapping({
        "enabled": True,
        "mode": "enforce",
    })
    assert policy.enabled is True

    result = policy.evaluate("mcp_filesystem_delete_file", {"path": "/tmp/x"})
    assert result.action == "deny"
    assert "destructive operation denied by default" in result.message

    # A native destructive tool too
    assert policy.evaluate("drop_database").action == "deny"


def test_destructive_explicitly_allowed_passes():
    """An explicit allow entry unblocks the destructive op."""
    policy = WriteGuardPolicy.from_mapping({
        "enabled": True,
        "mode": "enforce",
        "allow": ["mcp_filesystem_delete_file"],
    })
    result = policy.evaluate("mcp_filesystem_delete_file")
    assert result.action == "allow"


def test_destructive_audit_mode_still_allows():
    """Audit mode never blocks; it only records."""
    policy = WriteGuardPolicy.from_mapping({
        "enabled": True,
        "mode": "audit",
    })
    result = policy.evaluate("mcp_db__drop_table")
    assert result.action == "allow"


def test_destructive_denied_in_audit_mode_still_logged(tmp_path):
    """Audit-mode decisions reach the audit log with the right shape."""
    log = tmp_path / "audit.jsonl"
    policy = WriteGuardPolicy.from_mapping({
        "enabled": True,
        "mode": "audit",
        "_audit_path": log,  # not a real option; ensure default path is used
    })
    # Patch the module-level path resolver instead.
    import agent.write_guard as wg

    orig = wg._audit_path
    wg._audit_path = lambda: log
    try:
        policy.evaluate("mcp_db__drop_table")
        policy.evaluate("write_file", {"path": "a.txt", "content": "x"})
    finally:
        wg._audit_path = orig

    lines = [json.loads(l) for l in log.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    drop = next(e for e in lines if e["tool"] == "mcp_db__drop_table")
    assert drop["decision"] == "deny"
    assert drop["reason"] == "destructive default-deny"
    assert drop["risk"] == WriteRisk.DESTRUCTIVE
    assert "ts" in drop
    wf = next(e for e in lines if e["tool"] == "write_file")
    assert wf["decision"] == "allow"


def test_require_confirmation_denies_with_instruction():
    """#3300: a tool listed in require_confirmation is denied with an
    instruction to ask the user first."""
    policy = WriteGuardPolicy.from_mapping({
        "enabled": True,
        "mode": "enforce",
        "require_confirmation": ["mcp_github__create_issue"],
    })
    result = policy.evaluate("mcp_github__create_issue", {"title": "x"})
    assert result.action == "deny"
    assert "human" in result.message
    assert "confirmation" in result.message

    # Not listed → allowed (write, not destructive)
    assert policy.evaluate("mcp_github__update_issue").action == "allow"


def test_read_only_ops_not_logged(tmp_path):
    """Read-only decisions stay out of the audit log (only write ops audit)."""
    log = tmp_path / "audit.jsonl"
    import agent.write_guard as wg

    orig = wg._audit_path
    wg._audit_path = lambda: log
    try:
        policy = WriteGuardPolicy.from_mapping({"enabled": True, "mode": "enforce"})
        policy.evaluate("read_file", {"path": "a.txt"})
        policy.evaluate("web_search", {"query": "x"})
    finally:
        wg._audit_path = orig
    assert not log.exists() or not log.read_text().strip()


def test_audit_write_operation_fail_open(tmp_path):
    """A logging failure never raises — the gate decision is unaffected."""
    audit_write_operation(
        "mcp_x__delete_thing",
        {"path": "/etc/passwd"},
        decision="deny",
        reason="test",
        risk="destructive",
        path=Path("/proc/definitely/not/writable/audit.jsonl"),
    )  # no raise == pass


def test_mcp_write_ops_still_pass_without_allowlist():
    """Sanity: ordinary MCP writes keep working when the guard is on with no
    allowlist configured (back-compat with #3276 behavior)."""
    policy = WriteGuardPolicy.from_mapping({"enabled": True, "mode": "enforce"})
    assert policy.evaluate("mcp_filesystem_write_file").action == "allow"
    assert policy.evaluate("terminal", {"command": "ls"}).action == "allow"
