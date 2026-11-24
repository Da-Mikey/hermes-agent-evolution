"""#347 — a lifetime owner that dies before READY is a race, not a defect.

``LocalProcessHandle`` spawns a supervisor process and waits for a ``READY``
handshake before handing it the workload. When the host is under fork/exec
pressure (fd exhaustion, a transient vfork failure) that supervisor can die in
the handshake window, and the handle used to die with it: a bare
``RuntimeError("Local process owner exited during startup")`` that the failure
classifier had no rule for, so it fell through to ``persistent_error`` —
non-retryable — and fed the failure breaker that then blocked further calls.

Three contracts are pinned here:

* the handle replays the spawn **exactly once**, and only in the pre-dispatch
  window where the workload provably never ran;
* the post-``S`` window keeps the non-retryable contract *and* its own message
  text, because the classifier matches on text and a retry there could execute a
  mutating tool call twice; and
* the classifier reads the pre-dispatch failure as a retryable transient while
  both dispatch-phase variants stay non-retryable.
"""

import socket

import pytest

import tools.tool_failure_classifier as tfc
from tools.environments import local_process

# The two messages the READY window can raise (tools/environments/local_process.py).
STARTUP_MESSAGES = (
    "Local process owner exited during startup",
    "Local process owner failed to initialize",
)

# The post-`S` window: the workload may already be running here.
DISPATCH_MESSAGES = (
    "Local process owner exited during dispatch",
    "Local process owner failed to dispatch command",
)


def _bare_handle_with_closed_control():
    """A handle whose control socket already hit EOF, built without a spawn."""
    handle = local_process.LocalProcessHandle.__new__(local_process.LocalProcessHandle)
    handle._buffer = b""
    parent, child = socket.socketpair()
    child.close()  # EOF with nothing buffered == the owner died right here
    handle._control = parent
    return handle, parent


class TestPostDispatchMessage:
    """The rework crux: the two windows must not share one message text."""

    def test_eof_after_dispatch_reports_the_dispatch_wording(self):
        handle, control = _bare_handle_with_closed_control()
        try:
            # Exactly the call the STARTED read makes once `S` has been written.
            with pytest.raises(RuntimeError, match="exited during dispatch"):
                handle._startup_line()
        finally:
            control.close()

    def test_eof_before_dispatch_reports_the_startup_wording(self):
        handle, control = _bare_handle_with_closed_control()
        try:
            with pytest.raises(local_process._OwnerStartupRace, match="exited during startup"):
                handle._startup_line(retryable=True)
        finally:
            control.close()

    def test_the_two_windows_do_not_share_their_text(self):
        startup, dispatch = set(), set()
        for retryable, seen in ((True, startup), (False, dispatch)):
            handle, control = _bare_handle_with_closed_control()
            try:
                with pytest.raises(RuntimeError) as excinfo:
                    handle._startup_line(retryable=retryable)
            finally:
                control.close()
            seen.add(str(excinfo.value))

        assert startup and dispatch
        assert startup.isdisjoint(dispatch)


class TestBoundedReplay:
    @pytest.mark.parametrize("message", STARTUP_MESSAGES)
    def test_startup_race_is_replayed_once_then_succeeds(self, monkeypatch, message):
        calls = []

        def flaky(self, args, kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise local_process._OwnerStartupRace(message)

        monkeypatch.setattr(local_process.LocalProcessHandle, "_open_once", flaky)

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

    @pytest.mark.parametrize("message", DISPATCH_MESSAGES)
    def test_dispatch_phase_failure_is_never_replayed(self, monkeypatch, message):
        """After ``S`` the workload may already be running — a replay would
        execute the command twice, so that path must not be retried."""
        calls = []

        def failing(self, args, kwargs):
            calls.append(args)
            raise RuntimeError(message)

        monkeypatch.setattr(local_process.LocalProcessHandle, "_open_once", failing)

        with pytest.raises(RuntimeError, match="dispatch"):
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

    @pytest.mark.parametrize("message", DISPATCH_MESSAGES)
    def test_dispatch_phase_failure_stays_non_retryable(self, message):
        result = tfc.classify_tool_failure("terminal", message)

        assert result.category is not tfc.ToolFailureCategory.transient_network
        assert result.should_retry is False

    @pytest.mark.parametrize("message", DISPATCH_MESSAGES)
    def test_dispatch_close_stays_non_retryable_inside_an_envelope(self, message):
        """A retryable verdict on the post-``S`` close is a double-execution
        window, so it must survive the JSON envelope the tools actually emit."""
        result = tfc.classify_tool_failure(
            "terminal", """{"error": "%s", "exit_code": 1}""" % message
        )

        assert result.category is not tfc.ToolFailureCategory.transient_network
        assert result.should_retry is False
