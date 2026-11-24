# -*- coding: utf-8 -*-
"""Read + schema-lock the Hydra per-edge dispatch ledger (issue #148, Slice A).

The Hydra pipeline is itself a fixed DAG of subagent stages
(scan -> research -> issues -> analysis -> curate -> dispatch -> review), and a
plan is currently evaluated by *executing* it.  #148 asks for an OrchBench-style
deterministic simulator so orchestration variants can be compared at ~1% of the
token cost (Ren et al., arXiv:2607.25656).  That work needs a trustworthy read
of what past ticks actually dispatched -- which is exactly what the per-edge
dispatch ledger written by ``scripts/evolution_hydra_gate.py`` records.

**Slice A scope (this module): reading and schema locking only.**
Slice B (replay) and slice C (scoring) are deliberately out of scope; see the
decomposition on issue #148.

Why a dedicated reader instead of reusing the gate's private helper
-------------------------------------------------------------------
``evolution_hydra_gate._read_ledger`` **fails open** -- it returns ``{}`` on a
corrupt file, because a damaged ledger must never block a genuine stage
wake-up.  That is right for a gate and wrong for analysis: a silent ``{}`` here
would score a tick as "dispatched nothing" instead of "the ledger is broken",
turning a data fault into a fabricated clean result.  So this reader is
**fail-loud**: an absent ledger is ``{}`` (nothing to read, not an error), but a
present ledger that does not match the locked schema raises
:class:`LedgerSchemaError` naming the offending edge and field.

Schema (locked as version 1)
----------------------------
The on-disk ledger is a single JSON object keyed by edge label; each value is
one per-tick record::

    {
      "analysis->implementation": {
        "date": "2026-09-01",
        "upstream_mtime": 1788218339.84029,
        "upstream_hash": "<64-hex sha256>",
        "verdict": null
      },
      ...
    }

Version 1 records carry no ``version`` key (the writer never emitted one), so
an absent marker means version 1.  An explicit top-level ``"version"`` key is
honoured when present and rejected loudly when unknown, so a future writer can
bump the schema without this reader silently misreading it.  Unknown *extra*
keys inside a record are preserved in :attr:`LedgerRecord.extra` rather than
rejected -- additive fields stay forward-compatible -- but every field the
slice-B simulator will depend on is mandatory and validated.

CLI::

    python scripts/orchbench_ledger.py --evolution-dir ~/.hermes/evolution

prints a JSON summary on stdout and exits 2 on a schema violation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

__all__ = [
    "CURRENT_LEDGER_VERSION",
    "LEDGER_FILENAME",
    "SUPPORTED_LEDGER_VERSIONS",
    "LedgerRecord",
    "LedgerSchemaError",
    "ledger_path",
    "parse_ledger",
    "read_ledger",
    "summarise",
]

LEDGER_FILENAME = ".hydra-dispatch-ledger.json"
_VERSION_KEY = "version"
CURRENT_LEDGER_VERSION = 1
SUPPORTED_LEDGER_VERSIONS = frozenset({1})

# Every field the slice-B simulator will depend on. Validated, never inferred.
_REQUIRED_RECORD_FIELDS = ("date", "upstream_mtime", "upstream_hash", "verdict")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LedgerSchemaError(ValueError):
    """The ledger exists but does not match the locked schema.

    Raised loudly and never swallowed: a schema violation means the reader
    cannot be trusted to describe what was dispatched, and silently degrading to
    an empty ledger would fabricate a clean result.
    """


@dataclass(frozen=True)
class LedgerRecord:
    """One per-edge, per-tick dispatch record."""

    edge: str
    date: str
    upstream_mtime: float
    upstream_hash: str
    verdict: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Serialise back to the on-disk record shape (``edge`` excluded)."""
        payload: Dict[str, Any] = {
            "date": self.date,
            "upstream_mtime": self.upstream_mtime,
            "upstream_hash": self.upstream_hash,
            "verdict": self.verdict,
        }
        payload.update(self.extra)
        return payload

    @classmethod
    def from_dict(cls, edge: str, data: Any) -> "LedgerRecord":
        """Build a record from a raw ledger value, validating every field."""
        if not isinstance(data, dict):
            raise LedgerSchemaError(
                f"edge {edge!r}: record must be a JSON object, got "
                f"{type(data).__name__}"
            )
        missing = [f for f in _REQUIRED_RECORD_FIELDS if f not in data]
        if missing:
            raise LedgerSchemaError(
                f"edge {edge!r}: missing required field(s) {', '.join(sorted(missing))}"
            )
        date = data["date"]
        if not isinstance(date, str) or not _DATE_RE.match(date):
            raise LedgerSchemaError(
                f"edge {edge!r}: 'date' must be YYYY-MM-DD, got {date!r}"
            )
        mtime = data["upstream_mtime"]
        if isinstance(mtime, bool) or not isinstance(mtime, (int, float)):
            raise LedgerSchemaError(
                f"edge {edge!r}: 'upstream_mtime' must be a number, got "
                f"{type(mtime).__name__}"
            )
        digest = data["upstream_hash"]
        if not isinstance(digest, str) or not _SHA256_RE.match(digest):
            raise LedgerSchemaError(
                f"edge {edge!r}: 'upstream_hash' must be a 64-char lowercase "
                f"hex sha256, got {digest!r}"
            )
        verdict = data["verdict"]
        if verdict is not None and not isinstance(verdict, str):
            raise LedgerSchemaError(
                f"edge {edge!r}: 'verdict' must be a string or null, got "
                f"{type(verdict).__name__}"
            )
        extra = {
            k: v
            for k, v in data.items()
            if k not in _REQUIRED_RECORD_FIELDS and k != _VERSION_KEY
        }
        return cls(
            edge=edge,
            date=date,
            upstream_mtime=float(mtime),
            upstream_hash=digest,
            verdict=verdict,
            extra=extra,
        )


def ledger_path(evolution_dir: Union[str, Path]) -> Path:
    """Path to the per-edge dispatch ledger inside *evolution_dir*.

    Mirrors ``evolution_hydra_gate._ledger_path`` (verified against
    ``scripts/evolution_hydra_gate.py:117``); this reader must point at the same
    file the writer owns.
    """
    return Path(evolution_dir) / LEDGER_FILENAME


def _resolve_version(payload: Dict[str, Any]) -> int:
    """Return the declared schema version, defaulting to 1 when absent."""
    raw = payload.get(_VERSION_KEY, CURRENT_LEDGER_VERSION)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise LedgerSchemaError(
            f"'{_VERSION_KEY}' must be an integer, got {type(raw).__name__}"
        )
    if raw not in SUPPORTED_LEDGER_VERSIONS:
        raise LedgerSchemaError(
            f"unknown ledger schema version {raw} — this reader supports "
            f"{sorted(SUPPORTED_LEDGER_VERSIONS)}; refusing to guess at the "
            f"record shape"
        )
    return raw


def parse_ledger(payload: Any) -> Dict[str, LedgerRecord]:
    """Validate an already-decoded ledger payload (pure; no IO).

    Raises :class:`LedgerSchemaError` on anything that would make a downstream
    score untrustworthy.
    """
    if not isinstance(payload, dict):
        raise LedgerSchemaError(
            f"ledger must be a JSON object keyed by edge label, got "
            f"{type(payload).__name__}"
        )
    _resolve_version(payload)
    records: Dict[str, LedgerRecord] = {}
    for edge, value in payload.items():
        if edge == _VERSION_KEY:
            continue
        if not isinstance(edge, str) or not edge:
            raise LedgerSchemaError(f"edge label must be a non-empty string: {edge!r}")
        records[edge] = LedgerRecord.from_dict(edge, value)
    return records


def read_ledger(evolution_dir: Union[str, Path]) -> Dict[str, LedgerRecord]:
    """Read and validate the dispatch ledger in *evolution_dir*.

    An absent ledger returns ``{}`` — nothing has been dispatched yet, which is
    not a fault.  A *present* ledger that is unreadable or off-schema raises
    :class:`LedgerSchemaError` (fail-loud, unlike the gate's fail-open read).
    """
    path = ledger_path(evolution_dir)
    if not path.is_file():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise LedgerSchemaError(f"cannot read ledger {path}: {exc}") from exc
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        raise LedgerSchemaError(f"ledger {path} is not valid JSON: {exc}") from exc
    return parse_ledger(payload)


def summarise(records: Dict[str, LedgerRecord]) -> Dict[str, Any]:
    """Deterministic, JSON-safe summary of a validated ledger.

    Deliberately descriptive only: this is the slice-A hand-off to the slice-B
    replay/scoring work, not a score.  ``latest_date`` and ``edges`` are sorted
    so callers can diff two ticks without a tie-break rule.
    """
    edges: List[Dict[str, Any]] = [
        {
            "edge": rec.edge,
            "date": rec.date,
            "verdict": rec.verdict,
            "upstream_hash": rec.upstream_hash,
        }
        for rec in (records[e] for e in sorted(records))
    ]
    return {
        "version": CURRENT_LEDGER_VERSION,
        "edge_count": len(records),
        "edges": edges,
        "latest_date": max((r["date"] for r in edges), default=None),
    }


def _default_evolution_dir() -> Path:
    """``$HERMES_HOME/evolution`` when set, else ``~/.hermes/evolution``."""
    base = os.environ.get("HERMES_HOME")
    root = Path(base) if base else Path.home() / ".hermes"
    return root / "evolution"


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns a process exit code (2 on schema violation)."""
    parser = argparse.ArgumentParser(
        description="Read + schema-lock the Hydra per-edge dispatch ledger (#148)."
    )
    parser.add_argument(
        "--evolution-dir",
        default=None,
        help="Directory holding .hydra-dispatch-ledger.json "
        "(default: $HERMES_HOME/evolution)",
    )
    args = parser.parse_args(argv)
    evolution_dir = (
        Path(args.evolution_dir) if args.evolution_dir else _default_evolution_dir()
    )
    try:
        records = read_ledger(evolution_dir)
    except LedgerSchemaError as exc:
        print(f"[orchbench-ledger] SCHEMA ERROR: {exc}", file=sys.stderr)
        return 2
    json.dump(summarise(records), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via main()
    raise SystemExit(main())
