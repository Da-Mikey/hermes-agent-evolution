"""Unit tests for harness recovery strategies and durable audit persistence (Issues #3301 / #3304)."""

import json
import tempfile
from pathlib import Path

from agent.runtime_harness import (
    AgentRuntimeHarness,
    HarnessAction,
    HarnessPolicy,
)


def test_harness_recover_tool_failure():
    policy = HarnessPolicy(max_tool_failures_before_recovery=3)
    harness = AgentRuntimeHarness("sess-recover", policy=policy)

    # Failure 1: within budget
    dec1 = harness.recover_tool_failure("patch", "Hunk failed", consecutive_failures=1)
    assert dec1.action == HarnessAction.PROCEED

    # Failure 2: within budget
    dec2 = harness.recover_tool_failure("patch", "Hunk failed", consecutive_failures=2)
    assert dec2.action == HarnessAction.PROCEED

    # Failure 3: threshold reached -> initiate recovery
    dec3 = harness.recover_tool_failure("patch", "Hunk failed", consecutive_failures=3)
    assert dec3.action == HarnessAction.RECOVER
    assert "Exceeded failure threshold" in dec3.reason


def test_harness_durable_audit_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        audit_file = Path(tmpdir) / "audit.jsonl"
        policy = HarnessPolicy(audit_file_path=str(audit_file))
        harness = AgentRuntimeHarness("sess-audit", policy=policy)

        harness.pause("User request")
        harness.resume()
        harness.recover_tool_failure("read_file", "FileNotFoundError", consecutive_failures=1)
        harness.kill("Stop command")

        assert audit_file.exists()
        lines = [json.loads(line) for line in audit_file.read_text().strip().split("\n")]
        assert len(lines) == 4

        event_types = [item["event_type"] for item in lines]
        assert event_types == ["harness.pause", "harness.resume", "harness.recover", "harness.kill"]

        recover_entry = lines[2]
        assert recover_entry["details"]["tool_name"] == "read_file"
        assert "args_summary" in recover_entry["details"]
        assert "reason" in recover_entry["details"]
