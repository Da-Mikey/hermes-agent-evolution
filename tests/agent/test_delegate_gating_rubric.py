"""Tests for the delegate_task when-to-delegate cost gate (issue #69).

The gate is policy text inside the live tool description: it ships on every
API call, so it is wired by construction — unlike a standalone module it
cannot silently become dead code. These tests keep the gate language present,
stable, and consistent with the pre-existing USE FOR / DO NOT USE FOR rubric,
and within the compaction ceiling so the description stays cheap on every call.
"""

from tools.delegate_tool import _build_top_level_description


def _desc() -> str:
    return _build_top_level_description()


def test_description_has_cost_gate_section():
    desc = _desc()
    assert "COST GATE" in desc
    assert "USE FOR" in desc
    assert "DO NOT USE FOR" in desc


def test_cost_gate_names_both_clearance_criteria():
    # From the issue: spawn only when expected context-divergence AND
    # parallelization payoff clear a threshold (the paper's inverted-U gate).
    desc = _desc()
    assert "divergence" in desc
    assert "payoff" in desc
    # The gate is deterministic rule text, not another LLM call — it must
    # stay inside the static description (no dynamic probing language).
    assert "probe" in desc.lower()


def test_cost_gate_acts_on_last_known_state():
    # Paper principle: act on last-known state instead of live probing.
    desc = _desc()
    assert "LAST-KNOWN STATE" in desc


def test_cost_gate_keeps_existing_do_not_use_replacements():
    # The edit must not regress the pre-existing replacement pointers.
    desc = _desc()
    for needle in (
        "execute_code",
        "call the tool directly",
        "cannot ask questions",
        "cronjob",
    ):
        assert needle in desc


def test_cost_gate_stays_within_compaction_ceiling():
    # Adding the gate must not blow the description past the pre-existing
    # compaction contract (tests/tools/test_delegate.py asserts <= 2200).
    desc = _desc()
    assert len(desc) <= 2200
