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

import os
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


class TestStartupRaceDiagnostics:
    """#417 — the bare message carried no pid, no exit status and no reason.

    Eleven rows across eleven sessions in seven days still hit this text, from
    ``write_file``, ``patch`` and ``read_file`` — three different tools, so the
    mechanism was never tool-specific. The same sentence is what a code-skew
    move under a live process produces (the supervisor cannot import its own
    modules), and without a pid or the supervisor's own words there is nothing
    on the row to tell that apart from fork/exec pressure.
    """

    @staticmethod
    def _dead_owner(monkeypatch, *, pid=4242, returncode=1, stderr_text=b""):
        """A Popen stand-in whose supervisor already exited, with its last words."""
        read_fd, write_fd = os.pipe()
        if stderr_text:
            os.write(write_fd, stderr_text)
        os.close(write_fd)
        stream = os.fdopen(read_fd, "rb", buffering=0)

        class _Owner:
            stdin = None
            stdout = None

            def __init__(self):
                self.pid = pid
                self.stderr = stream

            def poll(self):
                return returncode

            def wait(self, timeout=None):
                return returncode

        owner = _Owner()
        monkeypatch.setattr(local_process.subprocess, "Popen", lambda *a, **k: owner)
        return owner

    @staticmethod
    def _handle():
        handle = local_process.LocalProcessHandle.__new__(local_process.LocalProcessHandle)
        handle._buffer = b""
        handle._closed = False
        return handle

    def test_dead_owner_is_named_with_pid_status_and_its_last_words(self, monkeypatch):
        self._dead_owner(
            monkeypatch,
            stderr_text=b"ModuleNotFoundError: No module named 'scripts'\n",
        )
        with pytest.raises(local_process._OwnerStartupRace) as excinfo:
            self._handle()._open_once(["true"], {})

        message = str(excinfo.value)

        # The anchor the classifier matches on must survive unchanged.
        assert message.startswith("Local process owner exited during startup")
        assert "pid 4242" in message
        assert "rc=1" in message
        assert "No module named 'scripts'" in message

    def test_enriched_message_still_classifies_as_the_retryable_race(self, monkeypatch):
        """The enrichment must not move the row out of the retryable class."""
        self._dead_owner(monkeypatch, stderr_text=b"boom: fd exhaustion\n")
        with pytest.raises(local_process._OwnerStartupRace) as excinfo:
            self._handle()._open_once(["true"], {})

        result = tfc.classify_tool_failure("write_file", str(excinfo.value))

        assert result.category is tfc.ToolFailureCategory.transient_network
        assert result.should_retry is True

    def test_live_owner_stderr_is_left_undrained(self, monkeypatch):
        """A still-running supervisor's stderr belongs to the workload."""
        owner = self._dead_owner(monkeypatch, returncode=None, stderr_text=b"workload output\n")
        stream = owner.stderr
        try:
            handle = self._handle()
            handle._owner = owner

            assert handle._owner_stderr_tail() == ""
            assert handle._owner_failure_detail() == (
                " (owner pid 4242 still running after the handshake failed)"
            )
            # Nothing was consumed, so the workload's bytes are still there.
            assert stream.read() == b"workload output\n"
        finally:
            stream.close()

    def test_missing_supervisor_is_stated_not_crashed(self, monkeypatch):
        handle = self._handle()
        handle._owner = None

        assert handle._owner_failure_detail() == " (no supervisor process was spawned)"
