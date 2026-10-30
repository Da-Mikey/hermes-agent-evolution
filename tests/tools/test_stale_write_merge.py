"""A stale write precondition is re-read and merged instead of refused (#252).

The guard fires when the file changed after this task last saw it, which is the
normal condition for any path a scheduler, a consolidation pass or a sibling
agent also writes. These tests pin the self-healing path at the REAL call site
(``write_file_tool`` via the registry), not a helper:

* a change to a DIFFERENT region than ours merges and the write succeeds,
* a change to the SAME region refuses, and reports the conflicting region,
* the ``not_fully_read`` class is untouched (it still needs a full read),
* no recorded base, and the kill switch, keep the old refusal verbatim.
"""

import json

import pytest

from tools.file_state import STALE_DISK_DRIFT  # noqa: F401  (documents the class in play)
from tools.file_tools import clear_file_ops_cache
from tools.file_tools_read_tracking import _read_tracker
from tools.file_write_merge import (
    MERGE_DISABLE_ENV,
    three_way_merge,
)
from tools.registry import registry


def _call(name, path, task_id, **arguments):
    result = registry.dispatch(name, {"path": str(path), **arguments}, task_id=task_id)
    assert isinstance(result, str)
    return json.loads(result)


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    monkeypatch.delenv(MERGE_DISABLE_ENV, raising=False)
    _read_tracker.clear()
    yield
    _read_tracker.clear()


# ── the merge itself ─────────────────────────────────────────────────────


def test_non_overlapping_changes_merge():
    base = "alpha\nbeta\ngamma\ndelta\n"
    ours = "alpha-mine\nbeta\ngamma\ndelta\n"
    theirs = "alpha\nbeta\ngamma\ndelta-theirs\n"
    outcome = three_way_merge(base, ours, theirs)
    assert outcome.ok, outcome.conflicts
    assert outcome.content == "alpha-mine\nbeta\ngamma\ndelta-theirs\n"
    assert outcome.hunks_applied == 1


def test_same_region_is_a_reported_conflict():
    base = "alpha\nbeta\n"
    ours = "alpha\nours\n"
    theirs = "alpha\ntheirs\n"
    outcome = three_way_merge(base, ours, theirs)
    assert not outcome.ok
    assert outcome.content == ours  # nothing half-merged is ever written
    region = outcome.conflicts[0]
    assert region.ours == "ours\n"
    assert region.theirs == "theirs\n"
    assert (region.base_start, region.base_end) == (2, 2)


def test_identical_change_on_both_sides_is_not_a_conflict():
    base = "alpha\nbeta\n"
    same = "alpha\nbeta-plus\n"
    outcome = three_way_merge(base, same, same)
    assert outcome.ok
    assert outcome.already_applied
    assert outcome.content == same


def test_conflict_text_reaches_the_payload():
    from tools.file_write_merge import merge_conflict_payload

    outcome = three_way_merge("a\nb\n", "a\nours\n", "a\ntheirs\n")
    payload = merge_conflict_payload("f.txt", "/abs/f.txt", outcome)
    assert payload["stale_write_blocked"] is True
    assert payload["merge_conflict"] is True
    assert payload["stale_reason_class"] == "stale_on_disk"
    assert payload["conflicts"][0]["on_disk_now"] == "theirs\n"
    assert payload["conflicts"][0]["your_write"] == "ours\n"
    assert payload["resolved_path"] == "/abs/f.txt"
    assert "Nothing was written" in payload["error"]


# ── the wired call site ──────────────────────────────────────────────────


def test_stale_write_merges_a_concurrent_change_elsewhere(tmp_path):
    path = tmp_path / "notes.txt"
    task = "merge-elsewhere"
    path.write_text("alpha\nbeta\ngamma\ndelta\n", encoding="utf-8")
    try:
        assert "error" not in _call("read_file", path, task)
        # A concurrent writer (scheduler, sibling agent) touches a later line.
        path.write_text("alpha\nbeta\ngamma\ndelta-concurrent\n", encoding="utf-8")

        written = _call(
            "write_file", path, task, content="alpha-mine\nbeta\ngamma\ndelta\n"
        )
        assert not written.get("stale_write_blocked"), written
        assert "error" not in written, written
        assert written["stale_merge"]["stale_precondition_recovered"] is True
        # Both changes survived: ours and the concurrent writer's.
        assert path.read_text(encoding="utf-8") == (
            "alpha-mine\nbeta\ngamma\ndelta-concurrent\n"
        )
    finally:
        clear_file_ops_cache(task)


def test_stale_write_on_the_same_region_still_refuses(tmp_path):
    path = tmp_path / "conflict.txt"
    task = "merge-conflict"
    path.write_text("alpha\nbeta\n", encoding="utf-8")
    try:
        assert "error" not in _call("read_file", path, task)
        path.write_text("alpha\ntheirs\n", encoding="utf-8")

        refused = _call("write_file", path, task, content="alpha\nours\n")
        assert refused.get("stale_write_blocked") is True, refused
        assert refused["merge_conflict"] is True
        assert refused["conflicts"][0]["on_disk_now"] == "theirs\n"
        # Nothing was written, and the refusal names the region in one turn.
        assert path.read_text(encoding="utf-8") == "alpha\ntheirs\n"
        assert "conflicts" in refused
    finally:
        clear_file_ops_cache(task)


def test_stale_write_merge_is_skipped_for_a_partial_read(tmp_path):
    """``not_fully_read`` keeps its own remedy: never merge onto unseen bytes."""
    path = tmp_path / "partial.txt"
    task = "merge-partial"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")
    try:
        assert "error" not in _call("read_file", path, task, offset=1, limit=1)
        path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")

        refused = _call("write_file", path, task, content="one\nmine\nthree\n")
        assert refused.get("stale_write_blocked") is True, refused
        assert "stale_merge" not in refused
        assert path.read_text(encoding="utf-8") == "one\ntwo\nthree\nfour\n"
    finally:
        clear_file_ops_cache(task)


def test_kill_switch_restores_the_hard_refusal(tmp_path, monkeypatch):
    path = tmp_path / "switch.txt"
    task = "merge-switch"
    path.write_text("alpha\nbeta\n", encoding="utf-8")
    try:
        assert "error" not in _call("read_file", path, task)
        path.write_text("alpha\nbeta-concurrent\n", encoding="utf-8")
        monkeypatch.setenv(MERGE_DISABLE_ENV, "1")

        refused = _call("write_file", path, task, content="alpha-mine\nbeta\n")
        assert refused.get("stale_write_blocked") is True, refused
        assert "next_action" in refused
        assert path.read_text(encoding="utf-8") == "alpha\nbeta-concurrent\n"
    finally:
        clear_file_ops_cache(task)


def test_missing_base_falls_back_to_the_plain_refusal(tmp_path):
    """No recorded base (e.g. the file was only patched, or the base was evicted)
    must keep the old refusal: a merge is never guessed."""
    from tools.file_tools_read_tracking import forget_merge_base

    path = tmp_path / "no-base.txt"
    task = "merge-no-base"
    path.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    try:
        assert "error" not in _call("read_file", path, task)
        forget_merge_base(str(path), task)  # simulate an evicted/absent base
        path.write_text("alpha\nbeta\ngamma-concurrent\n", encoding="utf-8")

        refused = _call("write_file", path, task, content="alpha-mine\nbeta\ngamma\n")
        assert refused.get("stale_write_blocked") is True, refused
        assert "next_action" in refused
        assert "stale_merge" not in refused
        assert path.read_text(encoding="utf-8") == "alpha\nbeta\ngamma-concurrent\n"
    finally:
        clear_file_ops_cache(task)
