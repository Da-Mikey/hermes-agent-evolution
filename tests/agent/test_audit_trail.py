"""Tests for the tamper-evident audit trail (issue #1719)."""

import json

import pytest

from agent import audit_trail


@pytest.fixture(autouse=True)
def isolate_hermes_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))


def test_append_creates_chained_entries(tmp_path):
    log = tmp_path / "audit.jsonl"
    first = audit_trail.append({"action": "read"}, path=log)
    second = audit_trail.append({"action": "write"}, path=log)

    assert first["prev_hash"] == audit_trail._GENESIS
    assert second["prev_hash"] == first["hash"]
    assert first["hash"] != second["hash"]
    assert audit_trail.verify(log) == (True, 2)


def test_verify_detects_tamper(tmp_path):
    log = tmp_path / "audit.jsonl"
    audit_trail.append({"action": "read"}, path=log)

    first = json.loads(log.read_text().splitlines()[0])
    first["payload"] = json.dumps({"action": "EVIL"})
    log.write_text(json.dumps(first, sort_keys=True) + "\n")

    assert audit_trail.verify(log)[0] is False


def test_prune_drops_old_and_reanchors(tmp_path):
    log = tmp_path / "audit.jsonl"
    prev = audit_trail._GENESIS
    lines = []
    for ts, action in ((1000000, "old"), (99999999999, "new")):
        payload = json.dumps({"ts": ts, "action": action}, sort_keys=True)
        entry = {
            "ts": ts,
            "prev_hash": prev,
            "payload": payload,
            "hash": audit_trail._hash(prev, payload),
        }
        lines.append(json.dumps(entry, sort_keys=True))
        prev = entry["hash"]
    log.write_text("\n".join(lines) + "\n")

    assert audit_trail.prune(now=99999999999, path=log) == 1
    kept = json.loads(log.read_text().splitlines()[0])
    assert kept["prev_hash"] == audit_trail._GENESIS and audit_trail.verify(log) == (
        True,
        1,
    )


def test_retention_days_default_and_config(tmp_path, monkeypatch):
    assert audit_trail.retention_days() == audit_trail.DEFAULT_RETENTION_DAYS

    (tmp_path / ".hermes" / "config.yaml").write_text(
        "security:\n  audit:\n    retention_days: 30\n"
    )
    import hermes_cli.config as cfg

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    assert audit_trail.retention_days() == 30


def test_audit_event_hash_inputs_and_serialization():
    evt = audit_trail.AuditEvent(
        event_id="evt-123",
        event_type="action",
        session_id="session-abc",
        task_id="task-1",
        tool_name="write_file",
        inputs_digest=audit_trail.AuditEvent.hash_inputs({"file": "src/app.py", "content": "print(1)"}),
        artifact_refs=["file://src/app.py"],
        validation_refs=["test://pytest:passed"],
        status="success",
        metadata={"duration_ms": 42},
    )
    rec = evt.to_record()
    assert rec["event_id"] == "evt-123"
    assert rec["inputs_digest"] is not None
    assert "file://src/app.py" in rec["artifact_refs"]

    restored = audit_trail.AuditEvent.from_record(rec)
    assert restored.event_id == evt.event_id
    assert restored.inputs_digest == evt.inputs_digest
    assert restored.artifact_refs == evt.artifact_refs


def test_extract_artifact_refs():
    # File write args
    refs = audit_trail.extract_artifact_refs("write_to_file", {"target_file": "src/main.py"})
    assert "file://src/main.py" in refs

    # Git commit in terminal output
    git_res = "[main abc1234] feat: add new feature\n 1 file changed"
    refs_git = audit_trail.extract_artifact_refs("terminal", {"command": "git commit -m 'feat'"}, result=git_res)
    assert "git://abc1234" in refs_git

    # Browser navigate URL
    refs_url = audit_trail.extract_artifact_refs("browser_navigate", {"url": "https://example.com/docs"})
    assert "https://example.com/docs" in refs_url


def test_extract_validation_refs():
    pytest_res = "====== 10 passed in 0.5s ======"
    refs = audit_trail.extract_validation_refs("terminal", {"command": "pytest tests/"}, result=pytest_res)
    assert len(refs) == 1
    assert "test://pytest tests/:passed" == refs[0]

    pytest_fail = "====== 1 failed, 9 passed in 0.5s ======"
    refs_fail = audit_trail.extract_validation_refs("terminal", {"command": "pytest tests/"}, result=pytest_fail)
    assert "test://pytest tests/:failed" == refs_fail[0]


def test_record_event_and_query_trail(tmp_path):
    log = tmp_path / "audit.jsonl"
    entry1 = audit_trail.record_event(
        event_type="action",
        session_id="session-1",
        tool_name="write_file",
        inputs={"file": "src/test.py"},
        path=log,
    )
    entry2 = audit_trail.record_event(
        event_type="validation",
        session_id="session-1",
        tool_name="terminal",
        inputs={"command": "pytest tests/"},
        metadata={"result": "10 passed"},
        path=log,
    )
    entry3 = audit_trail.record_event(
        event_type="action",
        session_id="session-2",
        tool_name="browser",
        inputs={"url": "https://example.com"},
        path=log,
    )

    assert entry1 is not None and entry2 is not None and entry3 is not None

    # Query filtered by session (default newest_first=True)
    s1_events = audit_trail.query_trail(session_id="session-1", path=log)
    assert len(s1_events) == 2
    assert s1_events[0]["payload"]["event_type"] == "validation"
    assert s1_events[1]["payload"]["event_type"] == "action"

    # Query with newest_first=False
    s1_events_chrono = audit_trail.query_trail(session_id="session-1", newest_first=False, path=log)
    assert s1_events_chrono[0]["payload"]["event_type"] == "action"
    assert s1_events_chrono[1]["payload"]["event_type"] == "validation"

    # Query filtered by event_type
    val_events = audit_trail.query_trail(event_type="validation", path=log)
    assert len(val_events) == 1
    assert val_events[0]["payload"]["session_id"] == "session-1"


def test_reconstruct_run_dag(tmp_path):
    log = tmp_path / "audit.jsonl"
    audit_trail.record_event(
        event_type="action",
        session_id="session-dag",
        tool_name="write_file",
        inputs={"target_file": "src/model.py"},
        path=log,
    )
    audit_trail.record_event(
        event_type="delegation",
        session_id="session-dag",
        task_id="sub-1",
        tool_name="delegate_task",
        inputs={"goal": "Verify the model"},
        artifact_refs=["file://src/model.py"],
        status="success",
        path=log,
    )
    audit_trail.record_event(
        event_type="validation",
        session_id="session-dag",
        tool_name="terminal",
        inputs={"command": "pytest tests/test_model.py"},
        metadata={"result": "5 passed"},
        path=log,
    )

    recon = audit_trail.reconstruct_run("session-dag", path=log)
    assert recon["session_id"] == "session-dag"
    assert recon["event_count"] == 3
    assert recon["valid_chain"] is True
    assert "file://src/model.py" in recon["artifacts"]
    assert any("test://pytest tests/test_model.py:passed" in v for v in recon["validations"])
    assert len(recon["delegations"]) == 1
    assert recon["summary"]["success_rate"] == 1.0
    assert recon["summary"]["actions_count"] == 1
    assert recon["summary"]["validations_count"] == 1
    assert recon["summary"]["delegations_count"] == 1


def test_concurrent_append_maintains_hash_chain(tmp_path):
    import concurrent.futures

    log = tmp_path / "concurrent_audit.jsonl"
    num_threads = 10
    records_per_thread = 15

    def _worker(thread_idx):
        for i in range(records_per_thread):
            audit_trail.record_event(
                event_type="action",
                session_id=f"session-{thread_idx}",
                tool_name="terminal",
                inputs={"cmd": f"echo {thread_idx}_{i}"},
                path=log,
            )

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(_worker, t) for t in range(num_threads)]
        for f in concurrent.futures.as_completed(futures):
            f.result()

    valid, count = audit_trail.verify(log)
    assert valid is True
    assert count == num_threads * records_per_thread


def test_sanitize_metadata_redacts_and_truncates():
    raw_meta = {
        "api_key": "sk-secret-12345",
        "result": "Authorization: Bearer my-top-secret-token-abcdef12345\nOutput details here...",
        "status_code": 200,
        "huge_output": "x" * 1000,
    }
    sanitized = audit_trail.sanitize_metadata(raw_meta)
    assert sanitized["api_key"] == "[REDACTED]"
    assert "[REDACTED]" in sanitized["result"]
    assert len(sanitized["huge_output"]) <= 503  # 500 + '...'


def test_extract_artifact_and_validation_accuracy():
    # read_file should NOT produce an artifact
    read_refs = audit_trail.extract_artifact_refs("read_file", {"path": "secrets.env"})
    assert len(read_refs) == 0

    # write_to_file SHOULD produce an artifact
    write_refs = audit_trail.extract_artifact_refs("write_to_file", {"target_file": "app.py"})
    assert "file://app.py" in write_refs

    # xfailed should NOT count as failed test
    xfailed_res = "1 xfailed, 9 passed in 2.0s"
    val_refs1 = audit_trail.extract_validation_refs("terminal", {"command": "pytest tests/"}, result=xfailed_res)
    assert len(val_refs1) == 1
    assert "passed" in val_refs1[0] and "failed" not in val_refs1[0]

    # '10 passed, 0 failed' should be passed, not failed
    zero_failed_res = "10 passed, 0 failed in 1.2s"
    val_refs2 = audit_trail.extract_validation_refs("terminal", {"command": "pytest tests/"}, result=zero_failed_res)
    assert len(val_refs2) == 1
    assert "passed" in val_refs2[0]

    # Linter tagging
    lint_res = "ruff check --fix"
    lint_val = audit_trail.extract_validation_refs("terminal", {"command": lint_res}, result="All checks passed!")
    assert len(lint_val) == 1
    assert lint_val[0].startswith("lint://")


def test_cmd_audit_cli_verify_and_show(tmp_path, capsys):
    log = tmp_path / "audit.jsonl"
    audit_trail.record_event(
        event_type="action",
        session_id="session-cli",
        tool_name="write_file",
        inputs={"file": "app.py"},
        path=log,
    )
    from argparse import Namespace
    from hermes_cli.audit_cmd import cmd_audit

    # Test verify
    cmd_audit(Namespace(audit_command="verify", path=str(log)))
    captured = capsys.readouterr()
    assert "Audit trail valid" in captured.out

    # Test show
    cmd_audit(Namespace(audit_command="show", session_id="session-cli", json=False, path=str(log)))
    captured = capsys.readouterr()
    assert "Audit Trail for Session: session-cli" in captured.out
    assert "file://app.py" in captured.out

    # Test prune with days
    cmd_audit(Namespace(audit_command="prune", days=1, path=str(log)))
    captured = capsys.readouterr()
    assert "Pruned" in captured.out


def test_handle_function_call_records_audit_trail(tmp_path, monkeypatch):
    log = tmp_path / "model_tools_audit.jsonl"
    monkeypatch.setattr(audit_trail, "_audit_path", lambda: log)

    from model_tools import handle_function_call
    from tools.registry import registry

    # Register dummy tool for testing
    registry.register(
        name="dummy_audit_tool",
        toolset="core",
        schema={"type": "function", "function": {"name": "dummy_audit_tool", "parameters": {"type": "object"}}},
        handler=lambda args, **kw: f"result_{args.get('arg1')}",
    )

    res = handle_function_call(
        function_name="dummy_audit_tool",
        function_args={"arg1": "test_val"},
        session_id="session-model-tools-test",
    )
    assert "result_test_val" in res

    events = audit_trail.query_trail(session_id="session-model-tools-test", path=log)
    assert len(events) == 1
    p = events[0]["payload"]
    assert p["event_type"] == "action"
    assert p["tool_name"] == "dummy_audit_tool"
    assert p["status"] == "success"
    assert "result_test_val" in p["metadata"]["result"]


