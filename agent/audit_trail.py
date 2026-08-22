"""Tamper-evident append-only audit trail (issue #1719, issue #3065).

Hash-chained JSONL: each entry stores the SHA-256 of the previous entry's hash
plus its payload, so tampering is detectable via ``verify()``. Retention via
``security.audit.retention_days`` (default 90); ``prune()`` re-anchors the chain.

Issue #3065: structured event schema linking action -> artifact -> validation,
with causal DAG reconstruction and query helpers for autonomous long-horizon runs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
import time
from typing import Any, Dict, List, Optional, Set
import uuid

from hermes_constants import get_hermes_home

DEFAULT_RETENTION_DAYS = 90
_GENESIS = "genesis"


@dataclass
class AuditEvent:
    """Structured audit event for autonomous agent actions and subagents."""

    event_id: str
    event_type: str  # "action", "artifact", "validation", "delegation"
    session_id: str
    task_id: Optional[str] = None
    parent_event_id: Optional[str] = None
    tool_name: Optional[str] = None
    inputs_digest: Optional[str] = None
    artifact_refs: List[str] = field(default_factory=list)
    validation_refs: List[str] = field(default_factory=list)
    status: str = "success"  # "success", "failure", "denied", "interrupted"
    metadata: Dict[str, Any] = field(default_factory=dict)
    ts: int = field(default_factory=lambda: int(time.time()))

    def to_record(self) -> dict:
        return asdict(self)

    @classmethod
    def from_record(cls, data: dict) -> AuditEvent:
        return cls(
            event_id=str(data.get("event_id", "")),
            event_type=str(data.get("event_type", "action")),
            session_id=str(data.get("session_id", "")),
            task_id=data.get("task_id"),
            parent_event_id=data.get("parent_event_id"),
            tool_name=data.get("tool_name"),
            inputs_digest=data.get("inputs_digest"),
            artifact_refs=list(data.get("artifact_refs", [])),
            validation_refs=list(data.get("validation_refs", [])),
            status=str(data.get("status", "success")),
            metadata=dict(data.get("metadata", {})),
            ts=int(data.get("ts", time.time())),
        )

    @staticmethod
    def hash_inputs(inputs: Any) -> str:
        """Compute deterministic SHA-256 digest of input parameters."""
        if inputs is None:
            return ""
        if isinstance(inputs, (dict, list)):
            try:
                raw = json.dumps(inputs, sort_keys=True, default=str)
            except Exception:
                raw = str(inputs)
        else:
            raw = str(inputs)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def retention_days() -> int:
    """Return the configured retention window in days (fail-open to default)."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = (load_config_readonly().get("security") or {}).get("audit") or {}
        val = int(cfg.get("retention_days", DEFAULT_RETENTION_DAYS))
        return val if val > 0 else DEFAULT_RETENTION_DAYS
    except Exception:
        return DEFAULT_RETENTION_DAYS


def is_audit_enabled() -> bool:
    """Return whether the audit trail is enabled (default True)."""
    try:
        from hermes_cli.config import load_config_readonly

        cfg = (load_config_readonly().get("security") or {}).get("audit") or {}
        return bool(cfg.get("enabled", True))
    except Exception:
        return True


def _audit_path() -> Path:
    return get_hermes_home() / "logs" / "audit-trail.jsonl"


def _hash(prev_hash: str, payload: str) -> str:
    return hashlib.sha256((prev_hash + payload).encode("utf-8")).hexdigest()


def _last_hash(path: Path) -> str:
    if not path.exists():
        return _GENESIS
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        if line.strip():
            try:
                return json.loads(line)["hash"]
            except (json.JSONDecodeError, KeyError):
                return _GENESIS
    return _GENESIS


def append(record: dict, *, path: Path | None = None) -> dict:
    """Append a record to the chained log and return it with hash fields."""
    path = path or _audit_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(record, sort_keys=True)
    prev = _last_hash(path)
    entry = {
        "ts": int(time.time()),
        "prev_hash": prev,
        "payload": payload,
        "hash": _hash(prev, payload),
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return entry


def extract_artifact_refs(
    tool_name: str, args: Optional[Dict[str, Any]] = None, result: Any = None
) -> List[str]:
    """Extract artifact references (file://, git://) from tool invocations."""
    refs: Set[str] = set()
    args = args or {}
    name = (tool_name or "").strip().lower()

    # File-modifying tools
    for key in (
        "file",
        "path",
        "file_path",
        "target_file",
        "targetfile",
        "destination",
        "dest",
        "filename",
    ):
        val = args.get(key)
        if val and isinstance(val, str) and not val.startswith(("http://", "https://")):
            refs.add(f"file://{val.strip()}")

    # Git operations in terminal or git tools
    if isinstance(result, str):
        # Look for commit hashes (e.g. [main abc1234] or commit abc1234567890)
        commit_match = re.search(
            r"\[[\w.\-/]+\s+([0-9a-f]{7,40})\]|\bcommit\s+([0-9a-f]{7,40})\b", result
        )
        if commit_match:
            commit_sha = commit_match.group(1) or commit_match.group(2)
            if commit_sha:
                refs.add(f"git://{commit_sha}")

    # URL artifacts from web/export tools
    if name in ("browser_navigate", "web_fetch", "export", "download"):
        url = args.get("url") or args.get("uri")
        if url and isinstance(url, str) and url.startswith(("http://", "https://")):
            refs.add(url.strip())

    return sorted(list(refs))


def extract_validation_refs(
    tool_name: str, args: Optional[Dict[str, Any]] = None, result: Any = None
) -> List[str]:
    """Extract validation references (test://, check://, exit://) from executions."""
    refs: Set[str] = set()
    args = args or {}
    name = (tool_name or "").strip().lower()
    cmd = str(args.get("command") or args.get("cmd") or "").strip()
    result_text = str(result or "")

    if name in ("terminal", "bash", "execute_command", "run_command") or cmd:
        if any(keyword in cmd for keyword in ("pytest", "python -m unittest", "cargo test", "npm test", "go test", "ruff", "flake8", "mypy", "lint")):
            # Test or linter run detected
            test_tag = "test" if "test" in cmd else "lint"
            passed = "passed" in result_text or "All checks passed" in result_text or "SUCCESS" in result_text
            failed = "failed" in result_text or "ERROR" in result_text or "FAIL" in result_text
            status = "passed" if (passed and not failed) else ("failed" if failed else "completed")
            refs.add(f"{test_tag}://{cmd[:60].strip()}:{status}")

    return sorted(list(refs))


def record_event(
    event_type: str,
    session_id: str,
    *,
    task_id: Optional[str] = None,
    parent_event_id: Optional[str] = None,
    tool_name: Optional[str] = None,
    inputs: Any = None,
    artifact_refs: Optional[List[str]] = None,
    validation_refs: Optional[List[str]] = None,
    status: str = "success",
    metadata: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
) -> Optional[dict]:
    """Construct a structured AuditEvent and append it to the tamper-evident log."""
    if not is_audit_enabled():
        return None

    # Auto-extract refs if not explicitly provided
    extracted_artifacts = list(artifact_refs or [])
    extracted_validations = list(validation_refs or [])

    if tool_name and isinstance(inputs, dict):
        if not artifact_refs:
            extracted_artifacts.extend(extract_artifact_refs(tool_name, inputs, metadata.get("result") if metadata else None))
        if not validation_refs:
            extracted_validations.extend(extract_validation_refs(tool_name, inputs, metadata.get("result") if metadata else None))

    event = AuditEvent(
        event_id=uuid.uuid4().hex,
        event_type=event_type,
        session_id=session_id,
        task_id=task_id,
        parent_event_id=parent_event_id,
        tool_name=tool_name,
        inputs_digest=AuditEvent.hash_inputs(inputs) if inputs is not None else None,
        artifact_refs=sorted(list(set(extracted_artifacts))),
        validation_refs=sorted(list(set(extracted_validations))),
        status=status,
        metadata=metadata or {},
    )
    return append(event.to_record(), path=path)


def query_trail(
    *,
    session_id: Optional[str] = None,
    task_id: Optional[str] = None,
    event_type: Optional[str] = None,
    limit: int = 500,
    path: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Query audit trail records with filtering."""
    path = path or _audit_path()
    if not path.exists():
        return []

    results: List[Dict[str, Any]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            payload_raw = entry.get("payload", "{}")
            payload = json.loads(payload_raw) if isinstance(payload_raw, str) else payload_raw
            if not isinstance(payload, dict):
                continue
            if session_id and payload.get("session_id") != session_id:
                continue
            if task_id and payload.get("task_id") != task_id:
                continue
            if event_type and payload.get("event_type") != event_type:
                continue
            results.append({"entry": entry, "payload": payload})
            if len(results) >= limit:
                break
        except (json.JSONDecodeError, KeyError):
            continue
    return results


def reconstruct_run(session_id: str, *, path: Optional[Path] = None) -> Dict[str, Any]:
    """Reconstruct action -> artifact -> validation DAG and summary for a session."""
    events = query_trail(session_id=session_id, limit=5000, path=path)
    chain_valid, total_entries = verify(path)

    actions: List[Dict[str, Any]] = []
    artifacts: Set[str] = set()
    validations: List[str] = []
    delegations: List[Dict[str, Any]] = []
    successful_actions = 0
    failed_actions = 0

    for e in events:
        p = e["payload"]
        etype = p.get("event_type", "action")
        status = p.get("status", "success")

        if p.get("artifact_refs"):
            artifacts.update(p["artifact_refs"])
        if p.get("validation_refs"):
            validations.extend(p["validation_refs"])

        if etype == "delegation":
            delegations.append(p)
        else:
            actions.append(p)

        if status == "success":
            successful_actions += 1
        elif status in ("failure", "error", "interrupted"):
            failed_actions += 1

    total_ops = len(actions) + len(delegations)
    success_rate = (successful_actions / total_ops) if total_ops > 0 else 1.0

    return {
        "session_id": session_id,
        "event_count": len(events),
        "valid_chain": chain_valid,
        "actions": actions,
        "artifacts": sorted(list(artifacts)),
        "validations": validations,
        "delegations": delegations,
        "summary": {
            "total_events": len(events),
            "actions_count": len(actions),
            "artifacts_count": len(artifacts),
            "validations_count": len(validations),
            "delegations_count": len(delegations),
            "successful_ops": successful_actions,
            "failed_ops": failed_actions,
            "success_rate": round(success_rate, 4),
        },
    }


def verify(path: Path | None = None) -> tuple[bool, int]:
    """Recompute the chain; return (valid, entry_count)."""
    path = path or _audit_path()
    if not path.exists():
        return True, 0
    prev, count = _GENESIS, 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            return False, count
        if entry.get("prev_hash") != prev or entry.get("hash") != _hash(
            prev, entry.get("payload", "")
        ):
            return False, count
        prev = entry["hash"]
        count += 1
    return True, count


def prune(*, now: float | None = None, path: Path | None = None) -> int:
    """Drop entries older than the retention window; re-anchor the chain."""
    path = path or _audit_path()
    if not path.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - retention_days() * 86400
    kept, removed = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ts = json.loads(line)["ts"]
        except (json.JSONDecodeError, KeyError, TypeError):
            kept.append(line)
            continue
        if ts < cutoff:
            removed += 1
        else:
            kept.append(line)
    if not removed:
        return 0
    reanchored, prev = [], _GENESIS
    for line in kept:
        entry = json.loads(line)
        entry["prev_hash"] = prev
        entry["hash"] = _hash(prev, entry["payload"])
        reanchored.append(json.dumps(entry, sort_keys=True))
        prev = entry["hash"]
    out = "\n".join(reanchored) + ("\n" if reanchored else "")
    path.write_text(out, encoding="utf-8")
    return removed
