"""install.sh reuses an already-installed supported Python instead of downloading 3.11 (#10778)."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"


def _exe(path: Path, content: str) -> Path:
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


PKG_MANAGER_MARKER = "PACKAGE-MANAGER-REACHED"
TIMEOUT_S = 120


def _run_prerequisites(
    tmp_path: Path, *, uv_find_script: str, provide_python: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run the real ``--stage prerequisites`` with a stub managed uv whose ``python find`` we script."""
    home = tmp_path / "home"
    hermes_home = home / ".hermes"
    (hermes_home / "bin").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if provide_python:
        _exe(
            bin_dir / "python3.13",
            "#!/bin/sh\n[ \"$1\" = --version ] && echo 'Python 3.13.12'\nexit 0\n",
        )
    _exe(
        hermes_home / "bin" / "uv",
        "#!/bin/sh\n[ \"$1\" = --version ] && { echo 'uv 0.9.0'; exit 0; }\n"
        'if [ "$1" = python ] && [ "$2" = find ]; then\n' + uv_find_script + "fi\n"
        'if [ "$1" = python ] && [ "$2" = install ]; then echo \'DOWNLOAD ATTEMPTED\' >&2; exit 1; fi\nexit 0\n',
    )
    for tool in ("git", "node", "npm", "curl", "rg", "g++", "c++", "ffmpeg"):
        _exe(
            bin_dir / tool,
            "#!/bin/sh\ncase \"$1\" in --version|-version|-v) echo 'v22.12.0 2.50.0';; esac\nexit 0\n",
        )
    # Every optional system tool the installer probes must be stubbed above, and the
    # package managers are poisoned as a backstop. #275: `rg` was stubbed but `ffmpeg`
    # was not, so on a runner without ffmpeg `install_system_packages` fell through to
    # `apt install -y ffmpeg` — an unbounded network wait that the runner's per-file
    # 300s cap SIGKILL'd, red-screening a green PR and freezing merges for every slice
    # containing this file. A missing optional tool must never reach the network here.
    for pm in ("apt", "apt-get", "dnf", "pacman", "brew", "pkg"):
        _exe(bin_dir / pm, f"#!/bin/sh\necho '{PKG_MANAGER_MARKER}' >&2\nexit 1\n")
    env = os.environ.copy()
    env.update({
        "HOME": str(home),
        "HERMES_HOME": str(hermes_home),
        "PATH": f"{bin_dir}{os.pathsep}{env.get('PATH', os.defpath)}",
    })
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not available — install.sh cannot be exercised")
    assert bash is not None  # narrow for type checkers; pytest.skip is NoReturn
    try:
        return subprocess.run(
            [bash, str(INSTALL_SH), "--stage", "prerequisites", "--non-interactive"],
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        # Fail LOUDLY and with partial output. Without this the runner's per-file 300s
        # cap SIGKILLs the whole slice, which reports the file as failed with no reason
        # and blocks every PR whose slice happens to include it (#275).
        captured = exc.stdout or b""
        if isinstance(captured, bytes):
            captured = captured.decode("utf-8", "replace")
        pytest.fail(
            f"install.sh --stage prerequisites did not finish within {TIMEOUT_S}s. "
            "An unbounded wait (network fetch, package-manager install or subprocess) "
            "has been reintroduced.\n--- partial output ---\n" + captured[-4000:]
        )


@pytest.mark.linux_only
def test_supported_newer_python_is_reused_instead_of_downloading_311(
    tmp_path: Path,
) -> None:
    """3.11 absent, 3.13 present: the installer must take 3.13 and never call ``uv python install``."""
    result = _run_prerequisites(
        tmp_path,
        uv_find_script=(
            # The range probe must carry --system: with the install's own venv activated, a plain
            # `uv python find` returns venv/bin/python3, which setup_venv then deletes.
            f'  [ "$3" = 3.11 ] && exit 2\n  [ "$3" = --system ] && [ "$4" = \'>=3.11,<3.14\' ] && {{ echo {tmp_path}/bin/python3.13; exit 0; }}\n  exit 2\n'
        ),
    )
    assert "DOWNLOAD ATTEMPTED" not in result.stdout, result.stdout
    assert "Python found: Python 3.13.12" in result.stdout, result.stdout
    # The stage must never have needed a package manager: that path is a network wait
    # (#275) and a green PR must not depend on runner network speed.
    assert PKG_MANAGER_MARKER not in result.stdout, result.stdout


@pytest.mark.linux_only
def test_missing_prerequisite_fails_loudly_instead_of_hanging(tmp_path: Path) -> None:
    """#275 DoD: with no usable interpreter, the stage must FAIL, not hang.

    ``_run_prerequisites`` is bounded, so reaching the assertions at all proves the
    stage terminated. It must terminate with a non-zero status and never fall through
    to a package-manager install to "fix" the missing interpreter.
    """
    result = _run_prerequisites(
        tmp_path,
        uv_find_script="  exit 2\n",
        provide_python=False,
    )
    assert result.returncode != 0, result.stdout
    assert PKG_MANAGER_MARKER not in result.stdout, result.stdout
