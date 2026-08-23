#!/usr/bin/env python3
"""Grade-weighted dream pass for the evolution pipeline (#1875, child of #1870).

Phase 1 of grade-weighted memory retention ("dreaming"): reads recent cycle
outcomes from ``metrics.jsonl`` and adjusts a file-backed note store so
high-grade runs get promoted (weight raised) and revision-needed runs get a
failure-mode tag.

Also hosts slice 1 of issue #48 (Oracular Dream upgrade): a deterministic
contradiction scanner over the same note store. Notes sharing a topic whose
lifecycle signals disagree (one promoted, one failure-tagged) are paired into
a prune/promote proposal — the newer note is authoritative and the older is
deprecation-linked for audit. Pure file-based — no LLM, no MCP dependency.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PROMOTE_BUMP = 0.5  # weight increment for high-grade cycles
WEIGHT_CAP = 2.0

#: Lifecycle signals the dream pass itself writes. A note tagged ``promoted``
#: or carrying weight > 1.0 asserts "this worked"; ``failure:unmerged``
#: asserts "this did not". Two notes about the same topic carrying opposite
#: signals are a contradiction candidate.
POSITIVE_SIGNAL_TAGS = ("promoted",)
NEGATIVE_SIGNAL_TAGS = ("failure:unmerged",)
POSITIVE_WEIGHT = 1.0  # weight strictly above this counts as positive signal
NEGATIVE_WEIGHT = 1.0  # weight strictly below this counts as negative signal


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file (one JSON object per line), skipping blank/malformed."""
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        try:
            obj = json.loads(s)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def load_records(metrics_file: Path) -> list[dict[str, Any]]:
    """Read metrics.jsonl records (one JSON object per line)."""
    return _load_jsonl(metrics_file)


def load_notes(notes_file: Path) -> list[dict[str, Any]]:
    """Read the file-backed note store (one JSON object per line)."""
    return _load_jsonl(notes_file)


def classify_cycle(rec: dict[str, Any]) -> str:
    """high-grade: >=1 merged and ratio >=0.5; revision-needed: 0 merged & >=1
    rejected; else neutral."""
    merged = int(rec.get("merged", 0) or 0)
    selected = int(rec.get("selected", 0) or 0)
    rejected = int(rec.get("rejected", 0) or 0)
    ratio = merged / selected if selected else 0.0
    if merged >= 1 and ratio >= 0.5:
        return "high-grade"
    if merged == 0 and rejected >= 1:
        return "revision-needed"
    return "neutral"


def dream_pass(
    metrics_file: Path, notes_file: Path, *, recent: int = 7
) -> dict[str, Any]:
    """Run one grade-weighted dream pass; write ``dream_pass.json`` summary.

    Promotes notes whose ``cycle`` matches a high-grade record (raise weight);
    tags notes for revision-needed cycles with ``failure:unmerged``.
    """
    records = load_records(metrics_file)[-recent:]
    grades = {r.get("date", "?"): classify_cycle(r) for r in records}
    by_grade = {
        g: [d for d, k in grades.items() if k == g]
        for g in ("high-grade", "revision-needed", "neutral")
    }
    notes = load_notes(notes_file)
    promoted = tagged = 0
    g = by_grade
    for n in notes:
        tags = n.setdefault("tags", [])
        if n.get("cycle") in g["high-grade"]:
            w = min(WEIGHT_CAP, float(n.get("weight", 1.0)) + PROMOTE_BUMP)
            n["weight"] = round(w, 2)
            if "promoted" not in tags:
                tags.append("promoted")
            promoted += 1
        elif n.get("cycle") in g["revision-needed"]:
            if "failure:unmerged" not in tags:
                tags.append("failure:unmerged")
            tagged += 1
    if notes:
        txt = "".join(json.dumps(n) + "\n" for n in notes)
        notes_file.write_text(txt, encoding="utf-8")
    summary = {
        "cycles_reviewed": len(records),
        "high_grade": by_grade["high-grade"],
        "revision_needed": by_grade["revision-needed"],
        "neutral": by_grade["neutral"],
        "notes_promoted": promoted,
        "notes_tagged": tagged,
    }
    (metrics_file.parent / "dream_pass.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


def _topic_of(note: dict[str, Any]) -> str | None:
    """The grouping key for contradiction detection.

    Prefers an explicit ``topic`` field; falls back to ``title`` when present.
    Notes with neither cannot be grouped and are skipped.
    """
    for key in ("topic", "title"):
        val = note.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip().lower()
    return None


def _signal(note: dict[str, Any]) -> str:
    """Lifecycle signal of a note: ``positive``, ``negative`` or ``neutral``.

    Positive: tagged ``promoted`` (written by the dream pass) or weight > 1.0.
    Negative: tagged ``failure:unmerged`` (written by the dream pass) or
    weight < 1.0.  Anything else is neutral — no contradiction is asserted.
    """
    tags = note.get("tags") or []
    try:
        weight = float(note.get("weight", 1.0))
    except (TypeError, ValueError):
        weight = 1.0
    if any(t in POSITIVE_SIGNAL_TAGS for t in tags) or weight > POSITIVE_WEIGHT:
        return "positive"
    if any(t in NEGATIVE_SIGNAL_TAGS for t in tags) or weight < NEGATIVE_WEIGHT:
        return "negative"
    return "neutral"


def scan_contradictions(
    notes_file: Path, notes: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """Slice 1 of #48: detect contradictory notes in the file-backed store.

    Two notes about the same topic with opposite lifecycle signals (one
    positive, one negative) form a contradiction.  For each pair the newer
    note (by ``cycle``) is authoritative: the proposal says the newer
    observation overrides the older one, and the older should be
    deprecation-linked for audit.  Deterministic, read-only — callers decide
    whether to apply the proposals (see :func:`apply_deprecations`).

    Returns a list of proposal dicts::

        {
          "topic": "...",
          "older_id": "...", "older_cycle": "...",
          "newer_id": "...", "newer_cycle": "...",
          "resolution": "newer-overrides-older",
          "action": "deprecate-older",
        }

    Notes without a topic/title are never grouped and are silently skipped.
    Cycles compare as strings; if either note lacks a comparable cycle the
    pair is skipped — conservatism beats a false positive here.
    """
    if notes is None:
        notes = load_notes(notes_file)
    by_topic: dict[str, list[dict[str, Any]]] = {}
    for n in notes:
        topic = _topic_of(n)
        if topic is None:
            continue
        by_topic.setdefault(topic, []).append(n)
    proposals: list[dict[str, Any]] = []
    for topic, group in by_topic.items():
        for i, older in enumerate(group):
            for newer in group[i + 1 :]:
                o_cycle, n_cycle = older.get("cycle"), newer.get("cycle")
                if not isinstance(o_cycle, str) or not isinstance(n_cycle, str):
                    continue  # cannot determine recency — skip
                if o_cycle == n_cycle:
                    continue  # same cycle: no ordering to adjudicate
                o_sig, n_sig = _signal(older), _signal(newer)
                if o_sig == "neutral" or n_sig == "neutral" or o_sig == n_sig:
                    continue
                if n_cycle > o_cycle:
                    older_note, newer_note = older, newer
                else:
                    older_note, newer_note = newer, older
                proposals.append({
                    "topic": topic,
                    "older_id": older_note.get("id"),
                    "older_cycle": older_note.get("cycle"),
                    "newer_id": newer_note.get("id"),
                    "newer_cycle": newer_note.get("cycle"),
                    "resolution": "newer-overrides-older",
                    "action": "deprecate-older",
                })
    return proposals


def apply_deprecations(notes_file: Path, proposals: list[dict[str, Any]]) -> int:
    """Apply deprecation links for contradiction proposals (issue #48).

    For each proposal, the older note is tagged ``deprecated:by=<newer_id>``
    and marked ``deprecated: true`` — the entry stays for audit, but is
    flagged so downstream readers treat the newer note as authoritative.
    Idempotent: notes already deprecated are left untouched.

    Returns the number of notes newly deprecated.
    """
    notes = load_notes(notes_file)
    applied = 0
    for prop in proposals:
        older_id = prop.get("older_id")
        for n in notes:
            if n.get("id") != older_id or n.get("deprecated"):
                continue
            tags = n.setdefault("tags", [])
            tag = f"deprecated:by={prop.get('newer_id')}"
            if tag not in tags:
                tags.append(tag)
            n["deprecated"] = True
            applied += 1
            break
    if applied and notes:
        txt = "".join(json.dumps(n) + "\n" for n in notes)
        notes_file.write_text(txt, encoding="utf-8")
    return applied


def dream_pass_with_contradictions(
    metrics_file: Path,
    notes_file: Path,
    *,
    recent: int = 7,
) -> dict[str, Any]:
    """Full dream pass: grade-weighted retention + contradiction scan (slice 1
    of #48).

    Runs the existing :func:`dream_pass`, then scans the (post-promotion)
    note store for same-topic contradictions, applies deprecation links, and
    writes ``contradiction_proposals.json`` next to the notes file.  The
    returned summary extends the base summary with ``contradictions_found``
    and ``deprecations_applied``.
    """
    summary = dream_pass(metrics_file, notes_file, recent=recent)
    proposals = scan_contradictions(notes_file)
    deprecated = apply_deprecations(notes_file, proposals)
    (notes_file.parent / "contradiction_proposals.json").write_text(
        json.dumps(proposals, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary["contradictions_found"] = len(proposals)
    summary["deprecations_applied"] = deprecated
    (metrics_file.parent / "dream_pass.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return summary


if __name__ == "__main__":  # pragma: no cover
    import sys

    evo = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("evolution")
    dream_pass_with_contradictions(evo / "metrics.jsonl", evo / "notes.jsonl")
