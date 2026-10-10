# -*- coding: utf-8 -*-
"""Execution hygiene for Python this project did not author (#160, item 1).

The failure mode closed here is **stdlib module shadowing**.  When Python is
launched by script path or with ``-c``, the interpreter prepends the script's
directory (respectively the current working directory) to ``sys.path``.  If
the content being executed — an unpacked archive, a downloaded skill bundle,
a synthesised tool — sits in that directory, an attacker-planted ``json.py``
or ``os.py`` is imported *instead of* the standard-library module the caller
meant, and the escape happens before any of our own code runs.

Two rules, applied together, remove the vector:

1. **Isolated mode.**  Every launch of Python over untrusted content carries
   ``-I``, which implies ``-E`` (ignore ``PYTHON*`` environment variables),
   ``-s`` (skip the user site directory) and ``-P`` (do not prepend an unsafe
   path to ``sys.path``).  ``hermes_cli/managed_uv.py`` and
   ``hermes_cli/sqlite_runtime.py`` already probe subprocesses this way; this
   module is the shared home for the pattern so that a new call site cannot
   quietly ship without it.
2. **Neutral working directory.**  The child is never started *inside* the
   directory whose content it is executing, so no relative ``sys.path`` entry
   can resolve into untrusted files even if isolated mode is bypassed.

Contract note: under ``-I`` the child cannot import modules sitting beside
the entry point it was handed.  Untrusted entry points are therefore expected
to be self-contained — read JSON from stdin, signal rejection with a non-zero
exit status.  That is already how the C-A-F verifier contract and the
synthesised-tool harness are written, so adopting this helper changes no
behaviour at the call sites that use it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Union

__all__ = [
    "ISOLATED_FLAG",
    "absolute_script_args",
    "isolated_python_command",
    "isolated_env",
    "neutral_cwd",
    "run_untrusted_python",
]

#: The flag that turns a Python launch into an isolated one.  It is a single
#: constant so that every call site is greppable and the rule is auditable.
ISOLATED_FLAG = "-I"

#: Interpreter flags whose FOLLOWING argument is not a script path.  ``-c`` and
#: ``-m`` carry code / a module name, so a caller that passes them has no path
#: to resolve; the rest carry a value that must not be mistaken for the script.
_CODE_FLAGS = ("-c", "-m")
_VALUE_FLAGS = ("-W", "-X", "--check-hash-based-pycs")

_NEUTRAL_CWD_NAME = "hermes-untrusted-exec"

PathLike = Union[str, os.PathLike]


def absolute_script_args(args: Sequence[str]) -> list[str]:
    """Resolve a script path in ``args`` against the CALLER's cwd.

    The failure this closes (#160 item 1 follow-up, household queue 85343599bb):
    children always start in :func:`neutral_cwd`, so a caller that hands over a
    RELATIVE script path (``"verifier.py"``) gets a child that cannot find its
    entry point.  The ``FileNotFoundError`` is an ``OSError``, which the existing
    call sites catch and report as *a failed verification* — a configuration
    error silently disguised as a candidate failure, and therefore a wrong
    routing decision.  Measured live before this fix: the same verifier returned
    ``False`` through a relative path and ``True`` through an absolute one.

    Resolving against ``os.getcwd()`` restores exactly the behaviour that
    preceded the neutral cwd (the child used to inherit the caller's directory),
    so correct callers see no change and a relative verifier path works again.

    Rules:

    * ``["-c", code]`` / ``["-m", module]`` carry no path — returned unchanged.
    * A relative script path becomes absolute; arguments after it are untouched.
    * A path that does not resolve is NOT rejected here: an unrunnable verifier
      failing closed (``verify() -> False``) is a deliberate, tested contract
      (``tests/evolution/test_caf_loop.py::test_script_verifier_missing_or_crashing_is_fail``)
      — turning it into an exception would change a verdict the callers rely on.
    """
    out = list(args)
    i = 0
    while i < len(out):
        token = out[i]
        if token in _CODE_FLAGS:
            return out
        if token in _VALUE_FLAGS:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        path = Path(token)
        if not path.is_absolute():
            out[i] = str(Path.cwd() / path)
        return out
    return out


def isolated_python_command(*args: str) -> list[str]:
    """Return ``[<interpreter>, "-I", *args]`` — the only supported launch form.

    Callers pass the Python-level arguments only (``["-c", code]`` or a script
    path); the interpreter and the isolation flag are supplied here.
    """
    return [sys.executable, ISOLATED_FLAG, *args]


def isolated_env(base_env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Copy ``base_env`` (default: the current environment) minus ``PYTHON*``.

    Isolated mode already ignores those variables, but stripping them keeps an
    explicitly non-isolated launch from inheriting a ``PYTHONPATH`` that
    re-opens the import path we just closed.
    """
    source = os.environ if base_env is None else base_env
    return {key: value for key, value in source.items() if not key.startswith("PYTHON")}


def neutral_cwd() -> Path:
    """Return a private scratch directory that untrusted children may run in.

    Deliberately outside the repository and outside any unpack target, so the
    content being executed cannot plant a shadowing module where the child
    looks.  Created on first use with owner-only permissions.
    """
    scratch = Path(tempfile.gettempdir()) / _NEUTRAL_CWD_NAME
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    return scratch


def run_untrusted_python(
    args: Sequence[str],
    *,
    stdin_data: Optional[str] = None,
    timeout: Optional[float] = None,
    cwd: Optional[PathLike] = None,
    isolated: bool = True,
) -> "subprocess.CompletedProcess[str]":
    """Run Python over content this project did not author.

    Always launches in isolated mode and always from :func:`neutral_cwd`
    unless a caller names a directory explicitly.  A script path among ``args``
    is made absolute first (:func:`absolute_script_args`) so a relative verifier
    path cannot be defeated by that neutral cwd.  Timeouts and OS errors
    propagate exactly as ``subprocess.run`` would, so existing callers keep
    their current ``try``/``except`` handling.
    """
    safe_args = absolute_script_args(args)
    command = isolated_python_command(*safe_args) if isolated else [sys.executable, *safe_args]
    return subprocess.run(
        command,
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(cwd) if cwd is not None else str(neutral_cwd()),
        env=isolated_env() if isolated else None,
        check=False,
    )
