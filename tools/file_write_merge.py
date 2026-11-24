"""Self-healing recovery for a stale ``write_file`` precondition (#252).

``write_file`` refuses to overwrite a file whose bytes changed since this task
last read it. The refusal is right in principle, but the file changing under a
task is the *normal* condition for any path a scheduler, a consolidation pass or
a sibling agent also touches — so the caller was handed a manual recovery
("re-read, then write") for a write that is usually still satisfiable.

This module performs that recovery automatically, at the real call site
(:func:`tools.file_tools.write_file_tool`):

1. take the content this task last saw in full (the merge base, captured by the
   read path in :mod:`tools.file_tools_read_tracking`),
2. take the intended content (``ours``) and the current on-disk content
   (``theirs``),
3. apply ``base -> ours`` hunks onto ``theirs`` where ``theirs`` still has the
   base bytes, and
4. return the merged content, or the conflicting regions when the two sides
   edited the same lines.

A merge that cannot be computed (no base, big file, disabled) returns ``None``
and the caller falls back to the existing refusal, so behaviour is unchanged
for every case the old code handled.
"""

from __future__ import annotations

import difflib
import logging
import os
import re
from dataclasses import dataclass, field

logger = logging.getLogger("tools.file_tools")

#: Kill switch, matching the ``HERMES_DISABLE_FILE_STATE_GUARD`` convention.
MERGE_DISABLE_ENV = "HERMES_DISABLE_STALE_WRITE_MERGE"

#: Cap on the regions reported back to the model (one turn of context).
MAX_REPORTED_CONFLICTS = 3
#: Cap on the characters of each conflicting side echoed back.
MAX_CONFLICT_CHARS = 600

STALE_MERGE_REASON = "write-stale-merge-conflict"


def merge_disabled() -> bool:
    """True when the user switched the re-read/merge recovery off."""
    return os.environ.get(MERGE_DISABLE_ENV, "").strip() == "1"


@dataclass(frozen=True)
class ConflictRegion:
    """One base region both sides changed, reported to the caller."""

    base_start: int  # 1-based first base line, 0 for a pure insertion point
    base_end: int  # 1-based last base line, 0 for a pure insertion point
    ours: str
    theirs: str

    def to_dict(self) -> dict:
        return {
            "base_lines": [self.base_start, self.base_end],
            "your_write": _clip(self.ours),
            "on_disk_now": _clip(self.theirs),
        }


@dataclass
class MergeOutcome:
    """Result of :func:`three_way_merge`.

    ``conflicts`` empty means *content* is safe to write; non-empty means the
    caller must decide, and *content* is the unmerged ``ours``.
    """

    content: str
    conflicts: list[ConflictRegion] = field(default_factory=list)
    hunks_applied: int = 0
    already_applied: bool = False

    @property
    def ok(self) -> bool:
        return not self.conflicts


def _clip(text: str) -> str:
    if len(text) <= MAX_CONFLICT_CHARS:
        return text
    return text[:MAX_CONFLICT_CHARS] + "\n… (truncated)"


def _opcodes(base: list[str], other: list[str]) -> list[tuple]:
    return difflib.SequenceMatcher(None, base, other).get_opcodes()


def _base_to_theirs(theirs_ops: list[tuple]) -> dict[int, int]:
    """``{base index: theirs index}`` for base lines ``theirs`` left untouched."""
    mapping: dict[int, int] = {}
    for tag, i1, i2, j1, _j2 in theirs_ops:
        if tag == "equal":
            for offset in range(i2 - i1):
                mapping[i1 + offset] = j1 + offset
    return mapping


def _insertion_point(
    pos: int, mapping: dict[int, int], base_len: int, theirs_len: int
) -> int | None:
    """Where an insertion before base line *pos* lands in ``theirs``, or ``None``.

    ``None`` means ``theirs`` already changed the bytes on one side of that
    point, so the two insertions compete for the same spot.
    """
    if base_len == 0:
        return None  # both sides wrote a file that did not exist: nothing to anchor to
    if pos == 0:
        return 0 if mapping.get(0) == 0 else None
    anchor = mapping.get(pos - 1)
    if anchor is None:
        return None
    target = anchor + 1
    if pos < base_len:
        return target if mapping.get(pos) == target else None
    # Appending: only clean when theirs did not append anything after the last base line.
    return target if anchor == theirs_len - 1 else None


def _region_unchanged(i1: int, i2: int, mapping: dict[int, int]) -> int | None:
    """``theirs`` start index for base region ``[i1, i2)`` if it is untouched and
    contiguous there, else ``None``."""
    start = mapping.get(i1)
    if start is None:
        return None
    for k in range(i1, i2):
        if mapping.get(k) != start + (k - i1):
            return None
    return start


def _same_change_on_both_sides(
    theirs_ops: list[tuple],
    theirs_lines: list[str],
    i1: int,
    i2: int,
    ours_lines: list[str],
) -> bool:
    """True when ``theirs`` replaced exactly this base region with exactly our text."""
    for tag, a1, a2, b1, b2 in theirs_ops:
        if tag == "equal" or a1 != i1 or a2 != i2:
            continue
        return theirs_lines[b1:b2] == ours_lines
    return False


def three_way_merge(base: str, ours: str, theirs: str) -> MergeOutcome:
    """Apply the ``base -> ours`` change onto ``theirs``.

    Line-based and conservative: a hunk is replayed only when ``theirs`` still
    holds the base bytes for that region, so a change on a *different* region
    (the concurrent-writer case this exists for) merges cleanly, while two
    changes to the *same* region are reported as conflicts instead of guessed.
    """
    if ours == theirs:
        return MergeOutcome(content=ours, already_applied=True)
    if theirs == base:
        return MergeOutcome(content=ours)

    base_lines = base.splitlines(keepends=True)
    ours_lines = ours.splitlines(keepends=True)
    theirs_lines = theirs.splitlines(keepends=True)

    theirs_ops = _opcodes(base_lines, theirs_lines)
    mapping = _base_to_theirs(theirs_ops)

    edits: list[
        tuple[int, int, list[str]]
    ] = []  # (theirs_start, theirs_end, replacement)
    conflicts: list[ConflictRegion] = []
    already = False

    for tag, i1, i2, j1, j2 in _opcodes(base_lines, ours_lines):
        if tag == "equal":
            continue
        replacement = ours_lines[j1:j2]
        if i1 == i2:  # pure insertion before base line i1
            target = _insertion_point(i1, mapping, len(base_lines), len(theirs_lines))
            if target is not None:
                edits.append((target, target, replacement))
                continue
            if _same_change_on_both_sides(
                theirs_ops, theirs_lines, i1, i2, replacement
            ):
                already = True
                continue
            conflicts.append(
                ConflictRegion(
                    base_start=i1,
                    base_end=i1,
                    ours="".join(replacement),
                    theirs=_theirs_window(theirs_ops, theirs_lines, i1),
                )
            )
            continue
        start = _region_unchanged(i1, i2, mapping)
        if start is not None:
            edits.append((start, start + (i2 - i1), replacement))
            continue
        if _same_change_on_both_sides(theirs_ops, theirs_lines, i1, i2, replacement):
            already = True
            continue
        conflicts.append(
            ConflictRegion(
                base_start=i1 + 1,
                base_end=i2,
                ours="".join(replacement),
                theirs=_theirs_window(theirs_ops, theirs_lines, i1, i2),
            )
        )

    # Two of our hunks landing on the same on-disk region would silently drop
    # one of them; treat that as a conflict rather than a merge.
    edits.sort(key=lambda e: (e[0], e[1]))
    overlapping = set()
    for prev, cur in zip(edits, edits[1:]):
        if cur[0] < prev[1] or (cur[0] == prev[0] and cur[1] == prev[1]):
            overlapping.update({id(prev), id(cur)})
    if overlapping:
        for edit in edits:
            if id(edit) in overlapping:
                conflicts.append(
                    ConflictRegion(
                        base_start=0,
                        base_end=0,
                        ours="".join(edit[2]),
                        theirs="".join(theirs_lines[edit[0] : edit[1]]),
                    )
                )
        edits = [e for e in edits if id(e) not in overlapping]

    merged = list(theirs_lines)
    for start, end, replacement in reversed(edits):
        merged[start:end] = replacement

    return MergeOutcome(
        content="".join(merged) if not conflicts else ours,
        conflicts=conflicts[:MAX_REPORTED_CONFLICTS],
        hunks_applied=len(edits),
        already_applied=already,
    )


def _theirs_window(
    theirs_ops: list[tuple], theirs_lines: list[str], i1: int, i2: int | None = None
) -> str:
    """The current on-disk text around base lines ``i1..i2`` (for the refusal)."""
    lo = i1
    hi = i1 if i2 is None else i2
    chunks: list[str] = []
    for tag, a1, a2, b1, b2 in theirs_ops:
        if tag == "equal":
            continue
        if a2 < lo or (a1 > hi and hi != lo):
            continue
        if a1 > hi and hi == lo:
            continue
        chunks.append("".join(theirs_lines[b1:b2]))
    if not chunks:
        return "".join(theirs_lines[lo:hi])
    return "".join(chunks)


# ── Call-site helpers (the real write path) ───────────────────────────────


def read_text_for_merge(resolved: str) -> str | None:
    """Current on-disk text, decoded the same way ``write_file`` encodes it."""
    try:
        with open(resolved, "rb") as stream:
            raw = stream.read()
    except OSError:
        return None
    try:
        return raw.decode("utf-8", "surrogateescape")
    except (UnicodeDecodeError, LookupError):  # pragma: no cover - defensive
        return None


def recover_stale_write(
    resolved: str | None, task_id: str, intended: str
) -> MergeOutcome | None:
    """Re-read *resolved*, merge *intended* onto it, and return the outcome.

    ``None`` means "no merge possible here" (kill switch, no recorded base, the
    path is gone, or the base is not text) — the caller then keeps its existing
    refusal, so this can never make a previously-working write fail differently.
    """
    if merge_disabled() or not resolved:
        return None
    from tools.file_tools_read_tracking import get_merge_base

    record = get_merge_base(resolved, task_id)
    if record is None:
        return None
    base = record[1]
    theirs = read_text_for_merge(resolved)
    if theirs is None:
        return None
    try:
        return three_way_merge(base, intended, theirs)
    except Exception:  # pragma: no cover - a merge bug must not break writes
        logger.debug("stale write merge failed for %s", resolved, exc_info=True)
        return None


_ACTION = (
    "Nothing was written. The automatic re-read/merge could not apply your change "
    "cleanly, so decide on the conflicting region(s) above in one step: call "
    "read_file to pick up the current content, then call write_file again with "
    "content that keeps the on-disk text for those regions (or use patch with "
    "old_string/new_string for just the lines you mean to change)."
)


def merge_conflict_payload(
    filepath: str,
    resolved: str | None,
    outcome: MergeOutcome,
    blocker_message: str | None = None,
) -> dict:
    """Model-facing refusal when the merge found a real conflict.

    Shaped like :func:`tools.file_tools_write_guards._stale_write_refusal` so
    existing consumers (``stale_write_blocked`` / ``stale_reason_class`` /
    ``next_action``) keep working, plus ``merge_conflict`` and the conflicting
    regions so the caller can resolve them without another re-read.
    ``blocker_message`` is the original guard message and is kept verbatim: it
    is what names the sibling task or the drifted path.
    """
    regions = [c.to_dict() for c in outcome.conflicts]
    reason = blocker_message or "the file changed on disk since you last read it."
    result = {
        "error": (
            f"Refusing to overwrite {filepath}: {reason} "
            f"Your change overlaps the current content in {len(regions)} region(s). "
            f"{_ACTION}"
        ),
        "stale_write_blocked": True,
        "stale_reason_class": "stale_on_disk",
        "merge_conflict": True,
        "conflicts": regions,
        "next_action": _ACTION,
        "path": filepath,
        "reason": STALE_MERGE_REASON,
        "recovery_hint": _ACTION,
    }
    if resolved:
        result["resolved_path"] = resolved
    return result


def merge_success_note(outcome: MergeOutcome) -> dict:
    """Additive result field describing an automatically merged write."""
    return {
        "stale_precondition_recovered": True,
        "merged_over_current_content": True,
        "hunks_reapplied": outcome.hunks_applied,
        "already_on_disk": outcome.already_applied,
        "message": (
            "The file changed on disk after your last read. Your change was re-applied "
            "onto the current content by a three-way merge instead of being refused; "
            "re-read the file if the merged result is not what you expected."
        ),
    }


__all__ = [
    "MERGE_DISABLE_ENV",
    "STALE_MERGE_REASON",
    "ConflictRegion",
    "MergeOutcome",
    "merge_conflict_payload",
    "merge_disabled",
    "merge_success_note",
    "read_text_for_merge",
    "recover_stale_write",
    "three_way_merge",
]
