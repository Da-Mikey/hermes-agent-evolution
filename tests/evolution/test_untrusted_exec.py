# -*- coding: utf-8 -*-
"""#160 item 1 — untrusted-code execution hygiene.

These tests do not merely assert that a flag is present: they build the real
attack the flag exists to stop (an untrusted directory containing a shadowing
``json.py``) and show the shadow winning without isolation and losing with it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from evolution.lib import caf_loop, tool_synthesis
from evolution.lib.untrusted_exec import (
    ISOLATED_FLAG,
    isolated_env,
    isolated_python_command,
    neutral_cwd,
    run_untrusted_python,
)

_JSON_USE = "import json; print(json.dumps({'a': 1}))"

_PROBE = "import hermes_shadow_probe as m; print(m.MARKER)"


def _untrusted_dir(tmp_path: Path) -> Path:
    """A directory standing in for an unpacked archive or downloaded bundle."""
    shadow = tmp_path / "untrusted"
    shadow.mkdir()
    (shadow / "hermes_shadow_probe.py").write_text(
        'MARKER = "shadow"\n', encoding="utf-8"
    )
    (shadow / "json.py").write_text(
        'raise SystemExit("shadow-json-imported")\n', encoding="utf-8"
    )
    return shadow


def test_isolated_command_puts_the_flag_after_the_interpreter():
    assert isolated_python_command("-c", "pass") == [
        sys.executable,
        ISOLATED_FLAG,
        "-c",
        "pass",
    ]


def test_isolated_flag_is_a_single_constant():
    assert ISOLATED_FLAG == "-I"


def test_shadowing_module_wins_without_isolation(tmp_path):
    """Establishes the threat: untrusted cwd on sys.path hijacks the import."""
    untrusted = _untrusted_dir(tmp_path)

    proc = run_untrusted_python(["-c", _PROBE], cwd=untrusted, isolated=False)

    assert proc.returncode == 0
    assert proc.stdout.strip() == "shadow"


def test_isolated_mode_defeats_the_shadowing_module(tmp_path):
    """The fix: the planted module is no longer reachable from the child."""
    untrusted = _untrusted_dir(tmp_path)

    proc = run_untrusted_python(["-c", _PROBE], cwd=untrusted, isolated=True)

    assert proc.returncode != 0
    assert "ModuleNotFoundError" in proc.stderr


def test_isolated_mode_protects_a_stdlib_module(tmp_path):
    """A malicious ``json.py`` must not be able to hijack stdlib imports."""
    untrusted = _untrusted_dir(tmp_path)

    hijacked = run_untrusted_python(["-c", _JSON_USE], cwd=untrusted, isolated=False)
    assert hijacked.returncode != 0
    assert "shadow-json-imported" in hijacked.stderr

    clean = run_untrusted_python(["-c", _JSON_USE], cwd=untrusted, isolated=True)
    assert clean.returncode == 0
    assert clean.stdout.strip() == '{"a": 1}'


def test_children_run_outside_the_directory_they_execute(tmp_path):
    untrusted = _untrusted_dir(tmp_path)

    proc = run_untrusted_python(["-c", "import os; print(os.getcwd())"])

    assert proc.returncode == 0
    assert Path(proc.stdout.strip()).resolve() == neutral_cwd().resolve()
    assert Path(proc.stdout.strip()).resolve() != untrusted.resolve()


def test_an_explicit_cwd_is_still_honoured(tmp_path):
    untrusted = _untrusted_dir(tmp_path)

    proc = run_untrusted_python(["-c", "import os; print(os.getcwd())"], cwd=untrusted)

    assert Path(proc.stdout.strip()).resolve() == untrusted.resolve()


def test_isolated_env_strips_only_python_variables():
    env = isolated_env({
        "PYTHONPATH": "/evil",
        "PYTHONSTARTUP": "/evil",
        "PATH": "/bin",
    })

    assert env == {"PATH": "/bin"}


def test_run_untrusted_python_defaults_to_stripping_python_env():
    proc = run_untrusted_python(
        ["-c", "import os; print(os.environ.get('PYTHONPATH', 'absent'))"],
    )

    assert proc.stdout.strip() == "absent"


def test_timeouts_still_propagate_to_the_caller():
    """Call sites rely on catching TimeoutExpired — keep that contract."""
    with pytest.raises(subprocess.TimeoutExpired):
        run_untrusted_python(["-c", "import time; time.sleep(5)"], timeout=0.2)


@pytest.mark.parametrize("module", [caf_loop, tool_synthesis])
def test_untrusted_call_sites_are_wired_to_the_isolated_runner(module):
    """Regression guard: the helper is useless if a call site drifts back."""
    source = Path(module.__file__).read_text(encoding="utf-8")

    assert "run_untrusted_python(" in source
    assert "sys.executable" not in source
