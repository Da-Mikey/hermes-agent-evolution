"""The optional trajectory logger must never kill a delegation (#414).

Both directions are asserted: with the module unimportable EscalationRun still
constructs and runs against the null trace, and with the module present it uses
the real TrajectoryLog. The unavailability is proven to be genuine — a bare
import under the same monkeypatch still raises — so the first test cannot pass
just because the patch did nothing.
"""
import sys
from types import SimpleNamespace

import pytest

from tools.delegation_escalation import (
    EscalationRun,
    _NullTrajectoryLog,
    run_with_escalation,
)

MODULE = "scripts.evolution_trajectory_logger"


def test_missing_trajectory_logger_degrades_to_null_trace(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # None in sys.modules makes the import machinery raise ImportError.
    monkeypatch.setitem(sys.modules, MODULE, None)

    # The guard is what saves us: the same import, unguarded, still blows up.
    with pytest.raises(ImportError):
        import importlib

        importlib.import_module(MODULE)

    child = SimpleNamespace(_interrupt_requested=False)
    run = EscalationRun(child, "private goal", {})

    assert isinstance(run.trace, _NullTrajectoryLog)
    # The start observation in __init__ already went through the null trace.
    assert [e.tool for e in run.trace.entries] == ["delegation_contact"]

    # Every call EscalationRun makes on the trace must stay fail-open.
    assert run.contact("terminal", '{"error": "failed"}') == ""
    assert run.contact("terminal", '{"error": "failed"}') == ""
    assert len(run.trace.entries) == len(run.contacts)
    assert run.trace.entries[-1].args_summary is run.contacts[-1]
    entry = run.finish({"status": "completed"})
    assert entry["escalation"]["contacts"] == len(run.contacts)
    assert any("trajectory logger unavailable" in r.message for r in caplog.records)


def test_missing_trajectory_logger_does_not_abort_run_with_escalation(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setitem(sys.modules, MODULE, None)

    child = SimpleNamespace(_interrupt_requested=False)
    result = run_with_escalation(lambda *a, **kw: {"status": "completed"}, 0, "goal", child)

    assert result["status"] == "completed"
    assert result["escalation"]["contacts"] >= 1
    assert child._delegation_escalation is None


def test_present_trajectory_logger_still_uses_real_log(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    run = EscalationRun(SimpleNamespace(_interrupt_requested=False), "goal", {})

    from scripts.evolution_trajectory_logger import TrajectoryLog

    assert isinstance(run.trace, TrajectoryLog)
    assert not isinstance(run.trace, _NullTrajectoryLog)
    run.contact("terminal", '{"error": "failed"}')
    run.finish({"status": "completed"})
    written = list((tmp_path / "evolution" / "trajectories").glob("*.json"))
    assert len(written) == 1
