"""Immutable-hash registry for long-lived prompt/state files (#98).

Self-propagating payloads ("mind viruses", Anthropic/EPFL Aug 2026) persist by
writing instructions into long-lived prompt/state files — SOUL.md, MEMORY.md,
USER.md — which the next agent then re-reads as directives.  The standing
``UNTRUSTED_CONTENT_GUIDANCE`` clause teaches the agent to treat that content
as data; this module adds the complementary *detection* half: a SHA-256
registry of those files that alerts when one drifts from its baseline.

Deliberately small, fail-open, and side-effect-minimal:

* ``verify_integrity`` never raises for operational reasons (missing or corrupt
  registry, unreadable files) — it returns a report the caller can log.
* Content is treated strictly as DATA: files are hashed, never parsed or
  executed, so an embedded payload can never influence this module's behavior.
* The registry lives under ``<home>/integrity/prompt_files.sha256.json``; the
  first run establishes the baseline, subsequent runs compare against it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Long-lived prompt/state files a mind-virus payload would target.  All are
# resolved relative to the agent's own HERMES_HOME (profile home).
PROTECTED_FILES: tuple[str, ...] = ("SOUL.md", "MEMORY.md", "USER.md")

_REGISTRY_DIRNAME = "integrity"
_REGISTRY_FILENAME = "prompt_files.sha256.json"

_CHUNK = 65536


def registry_path(home: Path) -> Path:
    """Absolute path of the hash registry for ``home``."""
    return home / _REGISTRY_DIRNAME / _REGISTRY_FILENAME


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_hashes(
    home: Path, files: tuple[str, ...] = PROTECTED_FILES
) -> dict[str, str]:
    """SHA-256 of each protected file that currently exists (relative paths).

    Unreadable files are treated as absent (fail open) rather than raising.
    """
    out: dict[str, str] = {}
    for rel in files:
        path = home / rel
        try:
            if path.is_file():
                out[rel] = _sha256(path)
        except OSError:
            continue
    return out


def load_registry(home: Path) -> dict[str, str]:
    """Read the stored registry; empty dict when absent or malformed (fail open)."""
    rp = registry_path(home)
    try:
        raw = json.loads(rp.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return {}
        return {str(k): str(v) for k, v in raw.items() if isinstance(v, str)}
    except (OSError, ValueError, TypeError):
        return {}


def store_registry(home: Path, hashes: dict[str, str]) -> None:
    """Atomically write the registry; never raises for operational reasons."""
    rp = registry_path(home)
    try:
        rp.parent.mkdir(parents=True, exist_ok=True)
        tmp = rp.with_name(_REGISTRY_FILENAME + ".tmp")
        tmp.write_text(
            json.dumps(hashes, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(tmp, rp)
    except OSError:
        logger.debug("Could not store prompt-integrity registry at %s", rp)


@dataclass
class IntegrityReport:
    """Outcome of a prompt-integrity verification."""

    established: bool = False
    drifts: list[str] = field(default_factory=list)
    current: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.drifts


_ALERT_STATE_SUFFIX = ".last-alert.json"


def _alert_state_path(home: Path) -> Path:
    """Where the last *alerted* drift signature is remembered for this profile.

    Beside the registry, because both share one home and one lifetime — this is one
    small file per profile, not per process, so a cron run and an interactive run do not
    each get to repeat the same warning (#271 logged 98 across processes).
    """
    rp = registry_path(home)
    return rp.with_name(rp.name + _ALERT_STATE_SUFFIX)


def drift_signature(report: IntegrityReport) -> str:
    """Identity of a drift *condition*: which paths drifted and to what content.

    Level-triggered logging repeats one sentence forever because the condition is
    unchanged. This signature is what makes the alarm edge-triggered: a file that drifts
    to *different* content is a new condition and does alarm again, while a file that
    simply stays drifted does not.
    """
    parts = [
        f"{rel}:{report.current.get(rel, 'missing')}" for rel in sorted(report.drifts)
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _load_alert_signature(home: Path) -> str:
    """Last alerted signature, or ``""`` when absent/unreadable (fail-open)."""
    try:
        payload = json.loads(_alert_state_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    signature = payload.get("signature") if isinstance(payload, dict) else None
    return signature if isinstance(signature, str) else ""


def _store_alert_signature(home: Path, signature: str) -> None:
    """Remember the alerted signature. Never raises: alerting state is not load-bearing."""
    try:
        path = _alert_state_path(home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"signature": signature}), encoding="utf-8")
    except OSError:  # pragma: no cover - defensive
        logger.debug(
            "Could not store prompt-integrity alert state at %s", _alert_state_path(home)
        )


def verify_integrity(
    home: Path,
    files: tuple[str, ...] = PROTECTED_FILES,
    *,
    establish: bool = True,
) -> IntegrityReport:
    """Compare current protected-file hashes against the stored registry.

    First run (no registry present) establishes the baseline and reports no
    drift.  Later runs report the relative paths of any file that was added,
    removed, or modified since the baseline.  Fail-open throughout: a missing
    or malformed registry is treated as "no baseline yet", never as an error.
    """
    current = compute_hashes(home, files)
    stored = load_registry(home)
    if not stored:
        if establish:
            store_registry(home, current)
        return IntegrityReport(established=True, current=current)

    drifts: list[str] = []
    for rel in sorted(set(stored) | set(current)):
        if stored.get(rel) != current.get(rel):
            drifts.append(rel)
    return IntegrityReport(drifts=drifts, current=current)


def verify_and_log(
    home: Path, files: tuple[str, ...] = PROTECTED_FILES
) -> IntegrityReport:
    """Run :func:`verify_integrity` and log baseline establishment + drift.

    Fail-open: an unexpected exception returns an empty report (no alert) and
    logs at debug level, so integrity checking can never disrupt prompt load.

    The drift warning is **edge-triggered** (#271): it fires when a drift condition
    appears or changes, then stays silent while that same condition persists. It used
    to fire on every read — 98 identical warnings for SOUL.md in one 24h window — and a
    security warning that repeats without ever changing state is a retry loop with no
    terminal outcome: the operator learns to ignore the line, which is the opposite of
    what an integrity alarm is for. The *signal* is unchanged (``report.drifts`` is
    always populated, and callers gate on it); only the repetition is suppressed.
    """
    try:
        report = verify_integrity(home, files)
    except Exception:  # pragma: no cover - defensive
        logger.debug("Prompt-integrity verification failed", exc_info=True)
        return IntegrityReport()
    if report.established:
        logger.info("Established prompt-file integrity baseline for %s", home)
    if report.ok:
        # The condition cleared: forget the last alert so the next drift is news again.
        if _load_alert_signature(home):
            _store_alert_signature(home, "")
        return report
    signature = drift_signature(report)
    if signature == _load_alert_signature(home):
        logger.debug(
            "Prompt-file integrity drift unchanged in %s (%s) — already alerted once",
            home,
            ",".join(sorted(report.drifts)),
        )
        return report
    for rel in report.drifts:
        logger.warning(
            "Prompt-file integrity drift detected for %r in %s — a long-lived "
            "prompt/state file changed since its baseline. Review it before "
            "trusting its content.",
            rel,
            home,
        )
    _store_alert_signature(home, signature)
    return report

