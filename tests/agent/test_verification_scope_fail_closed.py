"""An active verification scope cannot allow file access when its checker fails."""
import builtins
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from agent import file_safety
from agent.verification_scope import VerificationScope, VerificationScopeEnforcer
from tools import file_tools
from tools.environments.local import LocalEnvironment
from tools.file_operations import ShellFileOperations


@pytest.fixture
def target(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "state"))
    previous = file_safety.get_active_verification_scope()
    file_safety.set_active_verification_scope(None)
    path = tmp_path / "report.txt"
    path.write_text("original", encoding="utf-8")
    try:
        yield path
    finally:
        file_safety.set_active_verification_scope(previous)


@pytest.mark.parametrize("state", ["exception", "unavailable", "import_runtime", "violation", "allowed", "unset"])
def test_real_file_read_write_require_evaluable_active_scope(target, monkeypatch, state):
    if state != "unset":
        scope = VerificationScope(denied_paths=[str(target)]) if state == "violation" else VerificationScope(allowed_paths=[str(target)])
        file_safety.set_active_verification_scope(scope)
    if state == "exception":
        monkeypatch.setattr(VerificationScopeEnforcer, "check_file_access", Mock(side_effect=RuntimeError("synthetic private failure")))
    elif state in {"unavailable", "unset"}:
        monkeypatch.setitem(sys.modules, "agent.verification_scope", None)
    elif state == "import_runtime":
        original = builtins.__import__

        def unavailable(name, *args, **kwargs):
            if name == "agent.verification_scope":
                raise RuntimeError("synthetic private failure")
            return original(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", unavailable)
    ops = ShellFileOperations(LocalEnvironment(str(target.parent)))
    monkeypatch.setattr(file_tools, "_get_file_ops", lambda task_id: ops)
    write = ops.write_file(str(target), "changed")
    read = json.loads(file_tools.read_file_tool(str(target)))
    if state in {"allowed", "unset"}:
        assert write.error is None and not read.get("error")
        assert target.read_text(encoding="utf-8") == "changed"
        assert "changed" in str(read)
        file_safety.raise_if_read_blocked(str(target))
    else:
        assert write.error and read.get("error")
        assert target.read_text(encoding="utf-8") == "original"
        assert "original" not in str(read)
        with pytest.raises(ValueError, match="verification scope"):
            file_safety.raise_if_read_blocked(str(target))
        if state != "violation":
            for error in (write.error, read["error"]):
                assert str(target) not in error and "private failure" not in error
                assert ("unavailable" if state == "unavailable" else "could not be evaluated") in error


@pytest.mark.parametrize("active", [False, True])
def test_provider_read_guard_cannot_swallow_active_failure(target, monkeypatch, active):
    if active:
        file_safety.set_active_verification_scope(VerificationScope())
    monkeypatch.setattr(file_safety, "get_read_block_error", Mock(side_effect=RuntimeError("synthetic private failure")))
    if active:
        with pytest.raises(ValueError, match="could not be evaluated") as caught:
            file_safety.raise_if_read_blocked(str(target))
        assert "private failure" not in str(caught.value)
        assert caught.value.__suppress_context__ is True
    else:
        file_safety.raise_if_read_blocked(str(target))
