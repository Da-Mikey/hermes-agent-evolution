"""#347 — a lifetime owner that dies before READY is a race, not a defect.

``LocalProcessHandle`` spawns a supervisor process and waits for a ``READY``
handshake before handing it the workload. When the host is under fork/exec
pressure (fd exhaustion, a transient vfork failure) that supervisor can die in
the handshake window, and the handle used to die with it: a bare
``RuntimeError("Local process owner exited during startup")`` that the failure
classifier had no rule for, so it fell through to ``persistent_error`` —
non-retryable — and fed the failure breaker that then blocked further calls.

Two contracts are pinned here:

* the handle replays the spawn **exactly once**, and only in the pre-dispatch
  window where the workload provably never ran; and
* the classifier reads the failure as a retryable transient, not a persistent
  defect, while the dispatch-phase variants stay non-retryable.
"""

import pytest

import tools.tool_failure_classifier as tfc
from tools.environments import local_process

# The two messages the READY window can raise (tools/environments/local_process.py).
STARTUP_MESSAGES = (
    "Local process owner exited during startup",
    "Local process owner failed to initialize",
)


def _fail_open_once(monkeypatch, exc):
    """Make the first `_open_once` attempt raise `exc`; later ones stand down."""
    calls = []

    def flaky(self, args, kwargs):
        calls.append(args)
        if len(calls) == 1:
            raise exc

    monkeypatch.setattr(local_process.LocalProcessHandle, "_open_once", flaky)
    return calls


class TestBoundedReplay:
    @pytest.mark.parametrize("message", STARTUP_MESSAGES)
    def test_startup_race_is_replayed_once_then_succeeds(self, monkeypatch, message):
        calls = _fail_open_once(monkeypatch, local_process._OwnerStartupRace(message))

        local_process.LocalProcessHandle(["true"])

        assert len(calls) == 2  # one replay, then it stood down

    def test_startup_race_is_bounded_at_two_attempts(self, monkeypatch):
        calls = []

        def always(self, args, kwargs):
            calls.append(args)
            raise local_process._OwnerStartupRace(STARTUP_MESSAGES[0])

        monkeypatch.setattr(local_process.LocalProcessHandle, "_open_once", always)

        # Still a RuntimeError as far as every existing caller is concerned.
        with pytest.raises(RuntimeError, match="exited during startup"):
            local_process.LocalProcessHandle(["true"])

        assert len(calls) == 2  # bounded: never an unbounded spawn loop

    def test_startup_race_keeps_the_runtimeerror_contract(self):
        assert issubclass(local_process._OwnerStartupRace, RuntimeError)
        assert str(local_process._OwnerStartupRace(STARTUP_MESSAGES[0])) == STARTUP_MESSAGES[0]

    def test_dispatch_phase_failure_is_never_replayed(self, monkeypatch):
        """After ``S`` the workload may already be running — a replay would
        execute the command twice, so that path must not be retried."""
        calls = []

        def failing(self, args, kwargs):
            calls.append(args)
            raise RuntimeError("Local process owner failed to dispatch command")

        monkeypatch.setattr(local_process.LocalProcessHandle, "_open_once", failing)

        with pytest.raises(RuntimeError, match="failed to dispatch"):
            local_process.LocalProcessHandle(["true"])

        assert len(calls) == 1


class TestClassification:
    @pytest.mark.parametrize("message", STARTUP_MESSAGES)
    def test_startup_race_is_retryable_transient(self, message):
        result = tfc.classify_tool_failure("write_file", message)

        assert result.category is tfc.ToolFailureCategory.transient_network
        assert result.should_retry is True

    def test_startup_race_classifies_inside_an_error_envelope(self):
        """The real shape: the tool wraps the message in a JSON envelope."""
        result = tfc.classify_tool_failure(
            "terminal",
            """{"error": "Local process owner exited during startup", "exit_code": 1}""",
        )

        assert result.category is tfc.ToolFailureCategory.transient_network
        assert result.should_retry is True

    def test_dispatch_phase_failure_stays_non_retryable(self):
        result = tfc.classify_tool_failure(
            "terminal", "Local process owner failed to dispatch command"
        )

        assert result.category is not tfc.ToolFailureCategory.transient_network
        assert result.should_retry is False
