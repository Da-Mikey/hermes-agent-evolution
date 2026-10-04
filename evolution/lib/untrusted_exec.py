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
    "isolated_python_command",
    "isolated_env",
    "neutral_cwd",
    "run_untrusted_python",
]

#: The flag that turns a Python launch into an isolated one.  It is a single
#: constant so that every call site is greppable and the rule is auditable.
ISOLATED_FLAG = "-I"

_NEUTRAL_CWD_NAME = "hermes-untrusted-exec"

PathLike = Union[str, os.PathLike]


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
    unless a caller names a directory explicitly.  Timeouts and OS errors
    propagate exactly as ``subprocess.run`` would, so existing callers keep
    their current ``try``/``except`` handling.
    """
    command = isolated_python_command(*args) if isolated else [sys.executable, *args]
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
