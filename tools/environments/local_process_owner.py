"""POSIX command supervisor, executed directly in a separate interpreter.

The supervisor remains its private process-group leader until the controller
releases it. Only this live owner signals the group; a remembered PGID is never
used as authority after the owner exits.
"""

import contextlib
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time


def _snapshot_descendants():
    try:
        import psutil
        return psutil.Process().children(recursive=True)
    except Exception:
        return []


def _bounded_snapshot():
    captured = []
    try:
        worker = threading.Thread(target=lambda: captured.extend(_snapshot_descendants()), daemon=True)
        worker.start()
        worker.join(0.05)
    except Exception:
        return []
    return list(captured)


def _sweep_escaped(descendants, group):
    for child in descendants:
        with contextlib.suppress(Exception):
            if child.is_running() and os.getpgid(child.pid) != group:
                child.kill()  # psutil rechecks its captured process identity


def _cleanup(force):
    group = os.getpgrp()
    # setsid is verified before dispatch. Never signal the controller's group,
    # even if supervisor startup is changed or unexpectedly intercepted.
    if group != os.getpid() or os.getsid(0) != os.getpid():
        return
    # Optional escaped-descendant discovery must never block the authoritative
    # same-group cleanup, including parent EOF and the hard-exit force path.
    descendants = _bounded_snapshot()
    if not force:
        os.killpg(group, signal.SIGTERM)  # windows-footgun: ok — POSIX supervisor only
        time.sleep(0.5)
    with contextlib.suppress(Exception):
        sweep = threading.Thread(target=_sweep_escaped, args=(descendants, group), daemon=True)
        sweep.start()
        sweep.join(0.05)
    os.killpg(group, signal.SIGKILL)  # windows-footgun: ok — the live owner kills itself last


def _close_stdio():
    null = os.open(os.devnull, os.O_RDWR)
    try:
        for fd in (0, 1, 2):
            os.dup2(null, fd)
    finally:
        if null > 2:
            os.close(null)


def main():
    control = socket.socket(fileno=int(sys.argv[1]))
    control.settimeout(5)
    # Do this here rather than relying on Popen.start_new_session: Darwin
    # gateway spawn shims have historically dropped that keyword.
    os.setsid()  # windows-footgun: ok — helper is launched only on POSIX
    if os.getpgrp() != os.getpid() or os.getsid(0) != os.getpid():
        raise RuntimeError("private process group unavailable")
    # A caught handler resets to default on exec; SIG_IGN would leak to bash.
    signal.signal(signal.SIGTERM, lambda *_: None)
    signal.signal(signal.SIGINT, lambda *_: None)
    control.sendall(b"READY\n")
    if control.recv(1) != b"S":
        return 1
    proc = subprocess.Popen(sys.argv[2:], stdin=0, stdout=1, stderr=2, close_fds=True)
    control.sendall(f"STARTED {proc.pid}\n".encode())
    _close_stdio()  # only the workload holds the inherited stdin/stdout pipes
    control.setblocking(False)
    reported = False
    release = False
    while True:
        code = proc.poll()  # the direct child is always reaped by its owner
        if code is not None and not reported:
            try:
                control.sendall(f"EXIT {code}\n".encode())
            except OSError:
                _cleanup(True)
                return 1
            reported = True
        if release and reported:
            return 0  # intentional background jobs keep their existing lifetime
        ready, _, _ = select.select([control], [], [], 0.02)
        if not ready:
            continue
        try:
            commands = control.recv(64)
        except BlockingIOError:
            continue
        if not commands or b"K" in commands:
            _cleanup(True)
            return 1
        if b"T" in commands:
            _cleanup(False)
            return 1
        if b"R" in commands:
            release = True  # never release a still-running command


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        # EOF is sufficient for the parent to report startup/owner failure.
        # If dispatch happened, keep cleanup inside this still-live owner.
        _cleanup(True)
        raise
