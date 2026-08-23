"""Tests for the grade-weighted dream pass (#1875)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from evolution_dream_pass import (  # noqa: E402
    apply_deprecations,
    classify_cycle,
    dream_pass,
    dream_pass_with_contradictions,
    load_notes,
    load_records,
    scan_contradictions,
)


def _jl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _cy(d: str, m: int, s: int, r: int) -> dict:
    return {"date": d, "merged": m, "selected": s, "rejected": r}


def _note(i: str, c: str, w: float = 1.0) -> dict:
    return {"id": i, "cycle": c, "weight": w, "tags": []}


def test_classify() -> None:
    assert classify_cycle(_cy("d", 2, 3, 1)) == "high-grade"
    assert classify_cycle(_cy("d", 0, 1, 2)) == "revision-needed"
    assert classify_cycle(_cy("d", 0, 0, 0)) == "neutral"
    assert classify_cycle(_cy("d", 1, 4, 0)) == "neutral"


def test_dream_pass_promotes_and_tags(tmp_path: Path) -> None:
    metrics, notes = tmp_path / "metrics.jsonl", tmp_path / "notes.jsonl"
    _jl(
        metrics,
        [
            _cy("2026-08-01", 2, 3, 1),
            _cy("2026-08-02", 0, 1, 2),
            _cy("2026-08-03", 0, 0, 0),
        ],
    )
    _jl(
        notes,
        [
            _note("n1", "2026-08-01"),
            _note("n2", "2026-08-02"),
            _note("n3", "2026-08-03"),
        ],
    )
    s = dream_pass(metrics, notes)
    assert s["cycles_reviewed"] == 3
    assert s["high_grade"] == ["2026-08-01"] and s["revision_needed"] == ["2026-08-02"]
    assert s["neutral"] == ["2026-08-03"]
    assert s["notes_promoted"] == 1 and s["notes_tagged"] == 1
    up = {n["id"]: n for n in load_notes(notes)}
    assert up["n1"]["weight"] == 1.5 and "promoted" in up["n1"]["tags"]
    assert "failure:unmerged" in up["n2"]["tags"]
    assert up["n3"]["tags"] == [] and up["n3"]["weight"] == 1.0
    assert (tmp_path / "dream_pass.json").exists()


def test_empty_inputs(tmp_path: Path) -> None:
    s = dream_pass(tmp_path / "metrics.jsonl", tmp_path / "notes.jsonl")
    assert s["cycles_reviewed"] == 0 and s["notes_promoted"] == 0
    assert s["notes_tagged"] == 0 and (tmp_path / "dream_pass.json").exists()


def test_weight_cap_and_recent_limit(tmp_path: Path) -> None:
    metrics, notes = tmp_path / "metrics.jsonl", tmp_path / "notes.jsonl"
    _jl(metrics, [_cy(f"2026-08-{i:02d}", 2, 3, 1) for i in range(1, 11)])
    _jl(notes, [_note(f"n{i}", f"2026-08-{i:02d}", 1.8) for i in range(1, 11)])
    s = dream_pass(metrics, notes, recent=3)
    assert s["cycles_reviewed"] == 3
    up = {n["id"]: n for n in load_notes(notes)}
    assert up["n8"]["weight"] == 2.0  # capped
    assert up["n1"]["weight"] == 1.8  # outside recent window, untouched


def test_load_records_skips_malformed(tmp_path: Path) -> None:
    f = tmp_path / "metrics.jsonl"
    f.write_text(
        '{"date":"x","merged":1}\nnot-json\n\n{"date":"y"}\n', encoding="utf-8"
    )
    assert [r["date"] for r in load_records(f)] == ["x", "y"]


# --- Contradiction scanner (issue #48, slice 1) ---


def _tn(i: str, c: str, topic: str, tags: list | None = None, w: float = 1.0) -> dict:
    n = _note(i, c, w)
    n["topic"] = topic
    if tags:
        n["tags"] = tags
    return n


def test_scan_detects_newer_overrides_older(tmp_path: Path) -> None:
    notes = tmp_path / "notes.jsonl"
    _jl(
        notes,
        [
            _tn("n1", "2026-08-01", "tool-x", ["failure:unmerged"]),
            _tn("n2", "2026-08-02", "tool-x", ["promoted"], 1.5),
        ],
    )
    props = scan_contradictions(notes)
    assert len(props) == 1
    p = props[0]
    assert p["older_id"] == "n1" and p["newer_id"] == "n2"
    assert p["topic"] == "tool-x"
    assert p["resolution"] == "newer-overrides-older"
    assert p["action"] == "deprecate-older"


def test_scan_reversed_cycles_flips_roles(tmp_path: Path) -> None:
    # Positive signal is OLDER, negative is NEWER → the newer (negative) is
    # authoritative, the older (positive) is deprecated.
    notes = tmp_path / "notes.jsonl"
    _jl(
        notes,
        [
            _tn("n1", "2026-08-01", "tool-x", ["promoted"], 1.5),
            _tn("n2", "2026-08-02", "tool-x", ["failure:unmerged"]),
        ],
    )
    props = scan_contradictions(notes)
    assert len(props) == 1
    assert props[0]["older_id"] == "n1" and props[0]["newer_id"] == "n2"


def test_scan_same_signal_is_not_a_contradiction(tmp_path: Path) -> None:
    notes = tmp_path / "notes.jsonl"
    _jl(
        notes,
        [
            _tn("n1", "2026-08-01", "tool-x", ["failure:unmerged"]),
            _tn("n2", "2026-08-02", "tool-x", ["failure:unmerged"]),
        ],
    )
    assert scan_contradictions(notes) == []


def test_scan_neutral_or_missing_topic_skipped(tmp_path: Path) -> None:
    notes = tmp_path / "notes.jsonl"
    _jl(
        notes,
        [
            _tn("n1", "2026-08-01", "tool-x"),  # neutral (no tags, weight 1.0)
            _note("n2", "2026-08-02"),  # no topic
            _tn("n3", "2026-08-03", "tool-y", ["promoted"], 1.5),
            _tn("n4", "2026-08-04", "tool-y", ["failure:unmerged"]),
        ],
    )
    props = scan_contradictions(notes)
    # Only the n3/n4 pair qualifies: n1 is neutral, n2 has no topic.
    assert len(props) == 1
    assert {props[0]["older_id"], props[0]["newer_id"]} == {"n3", "n4"}


def test_scan_requires_comparable_cycles(tmp_path: Path) -> None:
    notes = tmp_path / "notes.jsonl"
    n1 = _tn("n1", "2026-08-01", "tool-x", ["promoted"], 1.5)
    n2 = _tn("n2", "2026-08-02", "tool-x", ["failure:unmerged"])
    n2["cycle"] = None  # not orderable → pair must be skipped
    _jl(notes, [n1, n2])
    assert scan_contradictions(notes) == []


def test_apply_deprecations_tags_and_is_idempotent(tmp_path: Path) -> None:
    notes = tmp_path / "notes.jsonl"
    _jl(
        notes,
        [
            _tn("n1", "2026-08-01", "tool-x", ["failure:unmerged"]),
            _tn("n2", "2026-08-02", "tool-x", ["promoted"], 1.5),
        ],
    )
    props = scan_contradictions(notes)
    assert apply_deprecations(notes, props) == 1
    up = {n["id"]: n for n in load_notes(notes)}
    assert up["n1"]["deprecated"] is True
    assert "deprecated:by=n2" in up["n1"]["tags"]
    assert "deprecated" not in up["n2"]
    # Second run: nothing left to deprecate.
    assert apply_deprecations(notes, props) == 0


def test_dream_pass_with_contradictions_end_to_end(tmp_path: Path) -> None:
    metrics, notes = tmp_path / "metrics.jsonl", tmp_path / "notes.jsonl"
    _jl(
        metrics,
        [
            _cy("2026-08-01", 0, 1, 2),  # revision-needed → failure tag
            _cy("2026-08-02", 2, 3, 1),  # high-grade → promote
        ],
    )
    _jl(
        notes,
        [
            _tn("n1", "2026-08-01", "tool-x"),
            _tn("n2", "2026-08-02", "tool-x"),
        ],
    )
    s = dream_pass_with_contradictions(metrics, notes)
    assert s["contradictions_found"] == 1
    assert s["deprecations_applied"] == 1
    up = {n["id"]: n for n in load_notes(notes)}
    assert up["n1"]["deprecated"] is True  # older failure note deprecation-linked
    assert up["n2"]["weight"] == 1.5  # newer promoted note is authoritative
    assert (tmp_path / "contradiction_proposals.json").exists()
    assert (tmp_path / "dream_pass.json").exists()
