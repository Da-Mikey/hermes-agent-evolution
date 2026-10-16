"""Owned POSIX command handle with separate workload and supervisor lifetimes."""

import contextlib
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

logger = logging.getLogger(__name__)


class LocalProcessHandle:
    """Popen-like workload status backed by a private, live process-group owner.

    ``wait`` reaps the workload through its supervisor but intentionally retains
    ownership. The foreground waiter or background registry must call ``release``
    after draining a completed command, or ``terminate_owned`` on cancellation.
    """

    def __init__(self, args, **kwargs):
        self._lock = threading.RLock()
        self._buffer = b""
        self._returncode = None
        self._closed = False
        self._state = "owned"
        self._owner_failed = False
        parent, child = socket.socketpair()
        self._control = parent
        self._owner = None
        try:
            self._owner = subprocess.Popen(
                [sys.executable, "-I", str(Path(__file__).with_name("local_process_owner.py")),
                 str(child.fileno()), *args],
                pass_fds=(child.fileno(),), close_fds=True, start_new_session=False, **kwargs)
            child.close()
            self.pid = self._owner.pid
            self.stdin, self.stdout, self.stderr = self._owner.stdin, self._owner.stdout, self._owner.stderr
            parent.settimeout(5)
            if self._startup_line() != b"READY":
                raise RuntimeError("Local process owner failed to initialize")
            if os.getpgid(self.pid) != self.pid or self.pid == os.getpgrp():
                raise RuntimeError("Local process owner lacks a private process group")
            parent.sendall(b"S")
            started = self._startup_line().split()
            if len(started) != 2 or started[0] != b"STARTED":
                raise RuntimeError("Local process owner failed to dispatch command")
            self.command_pid = int(started[1])
            parent.setblocking(False)
        except BaseException:
            parent.close()  # EOF asks an already-started owner to clean up
            if self._owner is not None:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    self._owner.wait(timeout=3)
            raise
        finally:
            child.close()

    def _startup_line(self):
        while b"\n" not in self._buffer:
            chunk = self._control.recv(256)
            if not chunk:
                raise RuntimeError("Local process owner exited during startup")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return line

    @property
    def returncode(self):
        return self.poll()

    def poll(self):
        with self._lock:
            if not self._closed:
                while True:
                    try:
                        chunk = self._control.recv(256)
                    except (BlockingIOError, OSError):
                        break
                    if not chunk:
                        break
                    self._buffer += chunk
            while b"\n" in self._buffer:
                line, self._buffer = self._buffer.split(b"\n", 1)
                if line.startswith(b"EXIT "):
                    self._returncode = int(line[5:])
            owner_code = self._owner.poll()
            if owner_code is not None:
                if self._returncode is None:
                    self._returncode = owner_code or 127
                if self._state == "owned" and not self._owner_failed:
                    self._owner_failed = True
                    logger.warning("Local process cleanup incomplete: lifetime owner exited unexpectedly")
            return self._returncode

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while (code := self.poll()) is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(self._owner.args, timeout)
            time.sleep(0.01)
        return code

    def _send(self, command):
        with self._lock:
            if self._closed:
                return False
            try:
                self._control.send(command)  # one byte, nonblocking even during hard exit
                return True
            except OSError:
                logger.warning("Local process cleanup incomplete: lifetime owner unavailable")
                return False

    def terminate_owned(self, *, force=False, wait=True):
        with self._lock:
            # Cancellation and normal completion have one linearization point.
            # Once RELEASE wins, ownership has intentionally ended; a late
            # cancellation must not try to reacquire the former process group.
            if self._state == "released":
                return True
            self.poll()
            if self._owner_failed:
                logger.warning("Local process cleanup incomplete: lifetime owner unavailable")
                return False
            self._state = "cancelled"
            if not self._send(b"K" if force else b"T"):
                return self._owner.poll() is not None and not self._owner_failed
        if wait:
            try:
                self._owner.wait(timeout=3)
            except subprocess.TimeoutExpired:
                # Closing the private channel is another bounded cancellation
                # request, never an excuse to signal a cached PID or group.
                self._close_control()
                logger.warning("Local process cleanup incomplete: lifetime owner did not finish")
                return False
            self.poll()
        return True

    def kill(self):
        self.terminate_owned(force=True, wait=False)

    def release(self):
        with self._lock:
            if self._closed or self.poll() is None:
                return  # premature finalization cannot detach an active command
            if self._state == "owned":
                self._state = "released"
                self._send(b"R")
            # A cancellation that won the lock must finish cleanup; never send
            # RELEASE after it and accidentally detach its surviving children.
        try:
            self._owner.wait(timeout=1)
        except subprocess.TimeoutExpired:
            logger.warning("Local process cleanup incomplete: lifetime owner did not release")
        finally:
            self._close_control()

    def _close_control(self):
        with self._lock:
            if not self._closed:
                self._closed = True
                self._control.close()

    def __del__(self):
        # Do not block GC. An abandoned controller socket must not leave a live
        # supervisor/workload behind; process exit closes it by the same route.
        control = getattr(self, "_control", None)
        if control is not None:
            control.close()
