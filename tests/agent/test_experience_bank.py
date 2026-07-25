# -*- coding: utf-8 -*-
"""Tests for :mod:`agent.experience_bank` — the MemoHarness experience bank.

Hermetic by construction: the autouse fixtures in ``tests/conftest.py``
redirect ``HERMES_HOME`` to a per-test tempdir, and the module resolves
``get_hermes_home()`` lazily on every call, so no per-test monkeypatching is
needed.  Stdlib + pytest only; behavior/invariant tests, no source reading.
"""

from __future__ import annotations

import json
import time

import pytest

from agent import experience_bank as eb
from agent.experience_bank import (
    CATEGORY_TO_DIMENSION,
    CONFIDENCE_LEVELS,
    HARNESS_DIMENSIONS,
    ExperienceEntry,
    ExperiencePattern,
)

# evolution/ is a namespace directory (no __init__.py); the repo root is on
# sys.path via tests/conftest.py, so this import works without touching the
# runtime module (which must stay evolution-free).
from evolution.lib.root_cause_diagnosis import FailureCategory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _entry(**overrides) -> ExperienceEntry:
    base = dict(
        ts=1_700_000_000.0,
        session_id="sess-1",
        platform="cli",
        model="test-model",
        success=None,
        outcome_source="heuristic:unhandled_exception",
        confidence="low",
        terminal_reason="completed",
        primary_dimension="tool",
        secondary_dimensions=["context"],
        failure_category="network",
        tool="web_search",
        analysis="connection refused twice",
        stats={"tool_calls": 10, "tool_errors": 2, "iterations": 5},
    )
    base.update(overrides)
    return ExperienceEntry(**base)


def _pattern(id_: str = "tool-timeout", **overrides) -> ExperiencePattern:
    base = dict(
        id=id_,
        dimension="tool",
        category="timeout",
        trigger="tool calls time out repeatedly",
        guidance="Reduce batch size and retry with backoff.",
        evidence_count=4,
        first_seen=1_700_000_000.0,
        last_seen=1_700_086_400.0,
    )
    base.update(overrides)
    return ExperiencePattern(**base)


def _v0_entry_dict() -> dict:
    """A pre-versioning (v0) entry dict — no v1-only keys at all."""
    return {
        "ts": 1_700_000_000.0,
        "session_id": "old-sess",
        "platform": "telegram",
        "model": "old-model",
        "success": False,
        "primary_dimension": "context",
        "secondary_dimensions": [],
        "failure_category": "not_found",
        "analysis": "old-format line",
        "stats": {"tool_calls": 3},
    }


# ---------------------------------------------------------------------------
# Entry round-trip: append + iter, since_ts filtering
# ---------------------------------------------------------------------------

def test_append_and_iter_round_trip():
    e1 = _entry(session_id="s1", ts=100.0)
    e2 = _entry(session_id="s2", ts=200.0, success=False, primary_dimension=None)
    eb.append_entry(e1)
    eb.append_entry(e2)

    loaded = list(eb.iter_entries())
    assert [e.session_id for e in loaded] == ["s1", "s2"]
    assert loaded[0].to_dict() == e1.to_dict()
    assert loaded[1].to_dict() == e2.to_dict()
    assert loaded[1].success is False
    assert loaded[1].primary_dimension is None
    assert loaded[0].stats == {"tool_calls": 10, "tool_errors": 2, "iterations": 5}


def test_new_fields_round_trip():
    e = _entry(
        v=1,
        outcome_source="heuristic:interrupted",
        confidence="high",
        terminal_reason="interrupted",
        tool="terminal",
    )
    eb.append_entry(e)
    (loaded,) = list(eb.iter_entries())
    assert loaded.v == 1
    assert loaded.outcome_source == "heuristic:interrupted"
    assert loaded.confidence == "high"
    assert loaded.terminal_reason == "interrupted"
    assert loaded.tool == "terminal"
    assert loaded.to_dict() == e.to_dict()


def test_iter_entries_since_ts_filters():
    for ts in (100.0, 200.0, 300.0):
        eb.append_entry(_entry(ts=ts, session_id=f"s{int(ts)}"))

    assert [e.ts for e in eb.iter_entries(since_ts=200.0)] == [200.0, 300.0]
    assert [e.ts for e in eb.iter_entries(since_ts=999.0)] == []
    assert len(list(eb.iter_entries(since_ts=None))) == 3


def test_append_entry_never_raises_on_unwritable_path(monkeypatch, tmp_path):
    # Point HERMES_HOME at a path whose parent is a *file*, so mkdir fails.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    monkeypatch.setenv("HERMES_HOME", str(blocker / "home"))
    eb.append_entry(_entry())  # must swallow OSError, not raise


# ---------------------------------------------------------------------------
# iter_entries tolerance (and its non-silent stderr warning)
# ---------------------------------------------------------------------------

def test_iter_entries_missing_file_returns_empty():
    assert list(eb.iter_entries()) == []


def test_iter_entries_skips_corrupt_and_blank_lines(capsys):
    good = _entry(session_id="good")
    path = eb.entries_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n"
        "this is not json\n"
        '{"partial": true\n'
        '["a", "list", "line"]\n'
        + json.dumps(good.to_dict())
        + "\n"
        '{"ts": "not-a-number", "session_id": "bad-ts"}\n',
        encoding="utf-8",
    )
    loaded = list(eb.iter_entries())
    assert [e.session_id for e in loaded] == ["good"]
    # Tolerance is not silent: one summary warning per call.
    err = capsys.readouterr().err
    assert err.count("[experience_bank]") == 1
    assert "skipped 4 corrupt entries" in err


def test_iter_entries_clean_file_warns_nothing(capsys):
    eb.append_entry(_entry())
    assert len(list(eb.iter_entries())) == 1
    assert capsys.readouterr().err == ""


def test_iter_entries_single_corrupt_line_uses_singular(capsys):
    path = eb.entries_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("garbage\n", encoding="utf-8")
    assert list(eb.iter_entries()) == []
    err = capsys.readouterr().err
    assert "skipped 1 corrupt entry" in err


# ---------------------------------------------------------------------------
# Dimension validation / forward-compat coercion
# ---------------------------------------------------------------------------

def test_constructor_rejects_bad_primary_dimension():
    with pytest.raises(ValueError):
        _entry(primary_dimension="vibes")


def test_constructor_rejects_bad_secondary_dimension():
    with pytest.raises(ValueError):
        _entry(secondary_dimensions=["tool", "vibes"])


def test_constructor_rejects_non_tristate_success():
    with pytest.raises(ValueError):
        _entry(success="yes")


def test_from_dict_coerces_unknown_dimensions_to_none():
    d = _entry().to_dict()
    d["primary_dimension"] = "vibes"
    d["secondary_dimensions"] = ["tool", "vibes", "memory"]
    entry = ExperienceEntry.from_dict(d)
    assert entry.primary_dimension is None
    assert entry.secondary_dimensions == ["tool", "memory"]


def test_from_dict_tolerates_missing_fields():
    entry = ExperienceEntry.from_dict({})
    assert entry.v == 1
    assert entry.ts == 0.0
    assert entry.success is None
    assert entry.outcome_source == ""
    assert entry.confidence == "low"
    assert entry.terminal_reason == ""
    assert entry.tool is None
    assert entry.primary_dimension is None
    assert entry.secondary_dimensions == []
    assert entry.stats == {}


def test_from_dict_v0_line_defaults_new_fields():
    entry = ExperienceEntry.from_dict(_v0_entry_dict())
    assert entry.session_id == "old-sess"
    assert entry.success is False
    assert entry.v == 1
    assert entry.outcome_source == ""
    assert entry.confidence == "low"
    assert entry.terminal_reason == ""
    assert entry.tool is None


def test_v0_line_survives_disk_round_trip():
    path = eb.entries_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_v0_entry_dict()) + "\n", encoding="utf-8")
    (entry,) = list(eb.iter_entries())
    assert entry.v == 1
    assert entry.confidence == "low"
    assert entry.tool is None
    assert entry.primary_dimension == "context"


# ---------------------------------------------------------------------------
# Confidence validation / coercion
# ---------------------------------------------------------------------------

def test_confidence_accepts_both_levels():
    assert set(CONFIDENCE_LEVELS) == {"high", "low"}
    assert _entry(confidence="high").confidence == "high"
    assert _entry(confidence="low").confidence == "low"


def test_constructor_rejects_bad_confidence():
    with pytest.raises(ValueError):
        _entry(confidence="medium")


def test_from_dict_coerces_unknown_confidence_to_low():
    d = _entry(confidence="high").to_dict()
    d["confidence"] = "medium"
    assert ExperienceEntry.from_dict(d).confidence == "low"


# ---------------------------------------------------------------------------
# pattern_id slug helper
# ---------------------------------------------------------------------------

def test_pattern_id_without_tool():
    assert eb.pattern_id("tool", "timeout") == "tool-timeout"


def test_pattern_id_with_tool():
    assert eb.pattern_id("tool", "timeout", "terminal") == "tool-timeout-terminal"


def test_pattern_id_empty_tool_behaves_like_none():
    assert eb.pattern_id("tool", "timeout", "") == "tool-timeout"


# ---------------------------------------------------------------------------
# Patterns save/load (and its non-silent stderr warning)
# ---------------------------------------------------------------------------

def test_patterns_round_trip():
    pats = [
        _pattern("a", evidence_count=3),
        _pattern("b", dimension="memory", tool="memory_store"),
    ]
    eb.save_patterns(pats)
    loaded = eb.load_patterns()
    assert [p.to_dict() for p in loaded] == [p.to_dict() for p in pats]
    assert loaded[1].tool == "memory_store"
    assert loaded[0].v == 1


def test_pattern_from_dict_v0_defaults():
    d = _pattern("x").to_dict()
    del d["v"]
    del d["tool"]
    p = ExperiencePattern.from_dict(d)
    assert p.v == 1
    assert p.tool is None


def test_load_patterns_missing_file_returns_empty(capsys):
    assert eb.load_patterns() == []
    assert capsys.readouterr().err == ""


def test_load_patterns_corrupt_json_returns_empty_and_warns(capsys):
    path = eb.patterns_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert eb.load_patterns() == []
    err = capsys.readouterr().err
    assert err.count("[experience_bank]") == 1
    assert "could not parse" in err


def test_load_patterns_non_list_payload_returns_empty():
    path = eb.patterns_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('"just a string"', encoding="utf-8")
    assert eb.load_patterns() == []


def test_load_patterns_max_age_days_filters_stale():
    now = time.time()
    fresh = _pattern("fresh", last_seen=now - 5 * 86400)
    stale = _pattern("stale", last_seen=now - 60 * 86400)
    eb.save_patterns([fresh, stale])
    loaded = eb.load_patterns(max_age_days=30.0)
    assert [p.id for p in loaded] == ["fresh"]


# ---------------------------------------------------------------------------
# format_patterns_prompt
# ---------------------------------------------------------------------------

def test_format_patterns_prompt_empty_when_no_patterns():
    assert eb.format_patterns_prompt([]) == ""
    assert eb.format_patterns_prompt(None) == ""  # nothing on disk either


def test_format_patterns_prompt_loads_from_disk_when_none():
    eb.save_patterns([_pattern("p1")])
    block = eb.format_patterns_prompt()
    assert block.startswith("## Learned execution patterns\n")
    assert "[tool]" in block


def test_format_patterns_prompt_heading_and_line_shape():
    block = eb.format_patterns_prompt([_pattern()])
    lines = block.split("\n")
    assert lines[0] == "## Learned execution patterns"
    assert lines[1] == (
        "- [tool] Reduce batch size and retry with backoff. (evidence: 4)"
    )
    # No volatile date in the rendered line (prompt-cache stability).
    assert "last:" not in lines[1]


def test_format_patterns_prompt_deterministic_order():
    pats = [
        _pattern("low", guidance="low-guidance", evidence_count=1, last_seen=300.0),
        _pattern("high-old", guidance="old-guidance", evidence_count=5, last_seen=100.0),
        _pattern("high-new", guidance="new-guidance", evidence_count=5, last_seen=200.0),
    ]
    block = eb.format_patterns_prompt(pats)
    # evidence desc, then last_seen desc: high-new, high-old, low
    assert (
        block.index("new-guidance")
        < block.index("old-guidance")
        < block.index("low-guidance")
    )
    # Deterministic regardless of input order.
    assert eb.format_patterns_prompt(list(reversed(pats))) == block


def test_format_patterns_prompt_id_tiebreak():
    # Equal on evidence_count AND last_seen → id ascending decides.
    a = _pattern("aaa", guidance="guidance-aaa", evidence_count=3, last_seen=100.0)
    z = _pattern("zzz", guidance="guidance-zzz", evidence_count=3, last_seen=100.0)
    block = eb.format_patterns_prompt([z, a])
    assert block.index("guidance-aaa") < block.index("guidance-zzz")
    assert eb.format_patterns_prompt([a, z]) == block


def test_format_patterns_prompt_respects_max_patterns():
    pats = [_pattern(f"p{i}", evidence_count=i) for i in range(10)]
    block = eb.format_patterns_prompt(pats, max_patterns=3)
    assert block.count("\n- [") == 3


def test_format_patterns_prompt_respects_max_chars_on_line_boundary():
    pats = [_pattern(f"p{i}", guidance=f"guidance {i} " * 5) for i in range(10)]
    max_chars = 400
    block = eb.format_patterns_prompt(pats, max_chars=max_chars)
    assert len(block) <= max_chars
    # No mid-line cut: every line of the block is a complete rendered line.
    full = eb.format_patterns_prompt(pats)
    full_lines = full.split("\n")
    for line in block.split("\n"):
        assert line in full_lines


def test_format_patterns_prompt_tiny_max_chars_returns_empty():
    # Even the heading does not fit (or fits with zero pattern lines).
    assert eb.format_patterns_prompt([_pattern()], max_chars=10) == ""


# ---------------------------------------------------------------------------
# Mapping invariant vs the FailureCategory enum
# ---------------------------------------------------------------------------

def test_category_to_dimension_covers_every_failure_category():
    for cat in FailureCategory:
        assert cat.value in CATEGORY_TO_DIMENSION, (
            f"FailureCategory.{cat.name} missing from CATEGORY_TO_DIMENSION"
        )


def test_category_to_dimension_values_are_valid_dimensions_or_none():
    for key, dim in CATEGORY_TO_DIMENSION.items():
        assert isinstance(key, str)
        assert dim is None or dim in HARNESS_DIMENSIONS


# ---------------------------------------------------------------------------
# Harvest state
# ---------------------------------------------------------------------------

def test_harvest_state_round_trip():
    assert eb.get_harvest_state() == {}
    state = {"last_ts": 123.0, "seen_sessions": ["a", "b"]}
    eb.set_harvest_state(state)
    assert eb.get_harvest_state() == state


def test_harvest_state_corrupt_returns_empty():
    path = eb.harvest_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("### not json ###", encoding="utf-8")
    assert eb.get_harvest_state() == {}


def test_harvest_state_non_dict_returns_empty():
    path = eb.harvest_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert eb.get_harvest_state() == {}


def test_set_harvest_state_does_not_mutate_caller_dict():
    state = {"a": 1}
    eb.set_harvest_state(state)
    state["a"] = 999
    assert eb.get_harvest_state() == {"a": 1}
