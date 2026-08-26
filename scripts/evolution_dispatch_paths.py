#!/usr/bin/env python3
"""Canonical Hydra dispatch artifact path templates (issue #120).

Single source of truth for the dispatch-file naming convention so the
reader and writer can never drift:

  * tick1 == the base file:  hydra-dispatch-{date}.jsonl
  * per-tick snapshots:      hydra-dispatch-{date}-tickN.jsonl  (N >= 2)

Usage:
  python scripts/evolution_dispatch_paths.py base 2026-08-26
  python scripts/evolution_dispatch_paths.py tick 2026-08-26 7
  python scripts/evolution_dispatch_paths.py latest [KNOWLEDGE_DIR]
"""
from __future__ import annotations

import sys
from pathlib import Path

DISPATCH_BASE_TEMPLATE = "hydra-dispatch-{date}.jsonl"


def base_path(date: str) -> str:
    """tick1 — the base dispatch file for ``date``."""
    return DISPATCH_BASE_TEMPLATE.format(date=date)


def tick_path(date: str, tick: int) -> str:
    """Per-tick snapshot path. tick 1 is the base file, NOT a tick1 file."""
    if tick <= 1:
        return base_path(date)
    return f"hydra-dispatch-{date}-tick{tick}.jsonl"


def latest(known: Path) -> Path | None:
    """Newest existing hydra-dispatch-*.jsonl under ``known`` (or None)."""
    matches = sorted(known.glob("hydra-dispatch-*.jsonl"), key=lambda p: p.name)
    return matches[-1] if matches else None


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    cmd = argv[1]
    if cmd == "base" and len(argv) >= 3:
        print(base_path(argv[2]))
        return 0
    if cmd == "tick" and len(argv) >= 4:
        print(tick_path(argv[2], int(argv[3])))
        return 0
    if cmd == "latest":
        known = Path(argv[2]) if len(argv) >= 3 else Path.home() / ".hermes" / "knowledge"
        hit = latest(known)
        print(hit if hit else "<none>")
        return 0 if hit else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
