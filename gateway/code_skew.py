"""Detect when the gateway is running stale code after a hot ``git pull``.

The gateway's ``sys.modules`` is frozen at boot.  If the checkout is updated
underneath it, a first-time lazy import can resolve a freshly-pulled module
against a stale cached dependency -> ImportError.  We snapshot the revision at
startup so risky callers (e.g. ``/model`` switching) can refuse with a clear
"restart the gateway" message.  If the revision can't be read (non-git install,
IO error) the boot snapshot stays ``None`` and detection no-ops — never a false positive.
"""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_boot_fingerprint: str | None = None


def _fingerprint() -> str | None:
    """Current checkout fingerprint via the CLI's worktree-aware git-rev reader
    (``hermes_cli.main`` is always already imported in a gateway process)."""
    try:
        from hermes_cli.main import _read_git_revision_fingerprint

        return _read_git_revision_fingerprint(_PROJECT_ROOT)
    except Exception:
        return None


def record_boot_fingerprint() -> None:
    """Snapshot the checkout revision at gateway startup (idempotent)."""
    global _boot_fingerprint
    if _boot_fingerprint is None:
        _boot_fingerprint = _fingerprint()


def _short(fingerprint: str) -> str:
    """Render a ``git:<ref>:<sha>`` fingerprint as a compact label."""
    sha = fingerprint.rsplit(":", 1)[-1]
    return sha[:10] if sha and sha != "unresolved" and len(sha) > 10 else (sha or fingerprint)


def _commit_identity(fingerprint: str) -> str:
    """The part of a fingerprint that decides whether a restart is warranted.

    ``sys.modules`` is frozen per COMMIT, not per ref: switching branches while
    sitting on the same commit (exactly what an evolution run does between
    ``git checkout -b`` and its first commit) reloads nothing, so it must not
    request a restart.  Compare the sha component and ignore the ref.

    When the sha is missing or ``unresolved`` there is no commit identity to
    compare, so fall back to the whole fingerprint — the conservative direction,
    since a genuine drift behind an unreadable ref is still a real drift.

    Only ``git:<ref>:<sha>`` fingerprints are ref-stripped.  A non-git install
    reports ``skills:<version>:<date>:<mtime_ns>:<size>``, whose fields ALL carry
    meaning (size included), so the whole string is its identity.
    """
    if fingerprint.startswith("git:"):
        sha = fingerprint.rsplit(":", 1)[-1].strip()
        if sha and sha != "unresolved":
            return sha
    return fingerprint


def current_code_sha() -> str | None:
    """Full SHA for the checkout currently on disk, or None when unresolved."""
    fingerprint = _fingerprint()
    if fingerprint is None:
        return None
    sha = fingerprint.rsplit(":", 1)[-1]
    return sha if sha and sha != "unresolved" else None


def detect_code_skew() -> tuple[str, str] | None:
    """``(boot_rev, disk_rev)`` short labels if the checkout drifted since boot, else ``None``.

    Drift is judged on the COMMIT, not on the branch ref — see ``_commit_identity``.
    A branch switch that leaves the commit unchanged reloads nothing, so it must not
    ask for a restart (the old full-fingerprint comparison did, and then logged
    ``booted at <sha>, checkout now at <sha>`` — an undiagnosable no-op restart).
    """
    current = _fingerprint() if _boot_fingerprint is not None else None
    if current is None or current == _boot_fingerprint:
        return None
    if _commit_identity(current) == _commit_identity(_boot_fingerprint):
        return None
    return _short(_boot_fingerprint), _short(current)
