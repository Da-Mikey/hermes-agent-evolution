"""Refusal classes for the write_file overwrite guard (#217).

A refused overwrite used to hand the model two remedies at once — "re-read, it
changed on disk" AND "re-read every page, you never saw it all" — which are
opposite fixes, so a retry could satisfy neither and burn turns. These tests pin
the contract that replaced it:

* every ``check_stale`` message classifies into a known ``STALE_*`` kind;
* a refusal names its class (``stale_on_disk`` vs ``not_fully_read``);
* a refusal carries exactly ONE ``next_action``, and that action is the right one
  for its class.

The paginated-read case is included because a file too large for one read_file
page can only ever be seen by paging, and paging contiguously must count as a
full read (tools/file_tools_read_tracking.py:306).
"""

import json

import pytest

from tools import file_state
from tools.file_tools import read_file_tool, write_file_tool
from tools.file_tools_write_guards import (
    NOT_FULLY_READ,
    STALE_ON_DISK,
    StaleOverwriteBlocker,
    _stale_overwrite_blocker,
    _stale_write_refusal,
)


# ── stale_kind: every check_stale message maps to a class ─────────────────

_CHECK_STALE_MESSAGES = {
    "sibling-never-read": (
        "<p> was modified by sibling subagent 'sa' but this agent never read it. "
        "Read the file before writing to avoid overwriting the sibling's changes.",
        file_state.STALE_SIBLING_WRITE,
    ),
    "sibling-after-read": (
        "<p> was modified by sibling subagent 'sa' at 10:00 — after this agent's "
        "last read at 09:00. Re-read the file before writing.",
        file_state.STALE_SIBLING_WRITE,
    ),
    "disk-drift": (
        "<p> was modified since you last read it on disk (external edit or "
        "unrecorded writer). Re-read the file before writing.",
        file_state.STALE_DISK_DRIFT,
    ),
    "partial-view": (
        "<p> was last read with offset/limit pagination (partial view). Read the "
        "remaining pages, or use patch, before overwriting it.",
        file_state.STALE_PARTIAL_VIEW,
    ),
    "never-read": (
        "<p> was not read by this agent. Read the file first so you can write an "
        "informed edit.",
        file_state.STALE_NEVER_READ,
    ),
}


@pytest.mark.parametrize("name,payload", sorted(_CHECK_STALE_MESSAGES.items()))
def test_stale_kind_classifies_each_check_stale_message(name, payload):
    """A reworded check_stale message must fail here, not silently degrade."""
    message, expected = payload
    assert file_state.stale_kind(message) == expected, name


def test_stale_kind_degrades_to_unknown_and_none_is_safe():
    assert file_state.stale_kind("something completely different") == file_state.STALE_UNKNOWN
    assert file_state.stale_kind(None) == file_state.STALE_UNKNOWN


# ── the refusal payload ───────────────────────────────────────────────────

def test_refusal_payload_names_class_and_one_action():
    blocker = StaleOverwriteBlocker(
        STALE_ON_DISK, "the file changed.", "Re-read it, then write.")
    payload = _stale_write_refusal("/tmp/x", blocker, "/tmp/x")

    assert payload["stale_write_blocked"] is True
    assert payload["stale_reason_class"] == STALE_ON_DISK
    assert payload["next_action"] == "Re-read it, then write."
    # exactly one remedy: the old text offered both re-read and patch
    assert payload["error"].count(payload["next_action"]) == 1
    assert "prefer patch" not in payload["error"]


def test_refusal_payload_accepts_a_plain_string_reason():
    """Older/other call sites passing a bare message still work."""
    payload = _stale_write_refusal("/tmp/x", "plain reason")
    assert payload["stale_write_blocked"] is True
    assert "stale_reason_class" not in payload
    assert payload["next_action"]
    assert "plain reason" in payload["error"]


def test_blocker_str_is_the_human_reason():
    blocker = StaleOverwriteBlocker(NOT_FULLY_READ, "not seen in full.", "read it")
    assert str(blocker) == "not seen in full."


# ── end to end through write_file: right class, right single remedy ───────

@pytest.fixture()
def existing(tmp_path):
    path = tmp_path / "output.md"
    path.write_text("original content\n")
    return str(path)


def test_never_read_existing_file_is_not_fully_read(existing):
    refused = json.loads(write_file_tool(existing, "x\n", task_id="t-217-never"))

    assert refused.get("stale_write_blocked"), refused
    assert refused["stale_reason_class"] == NOT_FULLY_READ
    # the remedy is "see every page", not "the file changed under you"
    assert "last line" in refused["next_action"]
    assert "current content" not in refused["next_action"]


def test_partial_read_is_not_fully_read(existing, tmp_path):
    big = tmp_path / "big.md"
    big.write_text("".join(f"line {i}\n" for i in range(1, 201)))
    assert "error" not in json.loads(
        read_file_tool(str(big), offset=1, limit=5, task_id="t-217-partial"))

    refused = json.loads(write_file_tool(str(big), "x\n", task_id="t-217-partial"))
    assert refused.get("stale_write_blocked"), refused
    assert refused["stale_reason_class"] == NOT_FULLY_READ


def test_external_change_is_stale_on_disk(existing):
    assert "error" not in json.loads(read_file_tool(existing, task_id="t-217-drift"))
    with open(existing, "a") as fh:
        fh.write("someone else appended\n")

    refused = json.loads(write_file_tool(existing, "x\n", task_id="t-217-drift"))
    assert refused.get("stale_write_blocked"), refused
    assert refused["stale_reason_class"] == STALE_ON_DISK
    # the remedy is "pick up the current bytes", not "page through everything"
    assert "current content" in refused["next_action"]
    assert "last line" not in refused["next_action"]


def test_fully_paged_read_clears_the_guard(existing, tmp_path):
    """Contiguous paging to the last line counts as a full read, so the write
    lands — the guard must not be structurally unsatisfiable for big files."""
    big = tmp_path / "paged.md"
    big.write_text("".join(f"line {i}\n" for i in range(1, 301)))
    task = "t-217-paged"
    first = json.loads(read_file_tool(str(big), offset=1, limit=150, task_id=task))
    assert "error" not in first, first
    assert "error" not in json.loads(
        read_file_tool(str(big), offset=151, limit=200, task_id=task))

    written = json.loads(write_file_tool(str(big), "rewritten\n", task_id=task))
    assert not written.get("stale_write_blocked"), written
    assert big.read_text() == "rewritten\n"


def test_blocker_is_none_for_a_net_new_file(tmp_path):
    assert _stale_overwrite_blocker(str(tmp_path / "nope.md"), str(tmp_path / "nope.md"),
                                    "t-217-new") is None
