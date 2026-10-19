"""Regression tests for issue #230 — keep the terminal→shared category map total.

``_TERMINAL_CATEGORY_MAP`` is keyed by every ``FailureCategory`` the terminal
classifier can emit, and it is consulted inside ``classify_tool_failure`` — the
failure path of a live turn. ``shell_syntax_error`` was missing from it, so a
malformed shell command raised ``KeyError`` out of the conversation loop
(2026-10-18 08:12:21, ``ERROR ... Outer loop error: 'shell_syntax_error'``) and
destroyed the rest of an unattended run, while the job still recorded
``last_status: ok``. Nothing asserted that the two enums agreed, which is how
the vocabularies drifted apart in the first place.
"""

from __future__ import annotations

from types import SimpleNamespace

from tools import tool_failure_classifier as tfc
from tools.terminal_failure_classifier import FailureCategory as TerminalCategory


def test_every_terminal_category_is_mapped():
    """The map must cover the whole terminal vocabulary, not a stale subset."""
    assert set(tfc._TERMINAL_CATEGORY_MAP) == set(TerminalCategory)


def test_mapped_values_use_the_shared_vocabulary():
    """Every value must be a real shared category, never a stray string."""
    for terminal_category, shared_category in tfc._TERMINAL_CATEGORY_MAP.items():
        assert isinstance(shared_category, tfc.ToolFailureCategory), terminal_category


def test_deterministic_terminal_categories_are_not_retryable():
    """Shell syntax / encoding / resource / env failures cannot succeed unchanged."""
    for category in (
        TerminalCategory.shell_syntax_error,
        TerminalCategory.output_encoding,
        TerminalCategory.resource_limit,
        TerminalCategory.env_not_found,
    ):
        shared = tfc._TERMINAL_CATEGORY_MAP[category]
        assert shared is tfc.ToolFailureCategory.persistent_error
        assert tfc._CATEGORY_RETRYABLE[shared] is False


def test_unmapped_terminal_category_degrades_instead_of_raising(monkeypatch):
    """A future terminal category must not be able to crash the turn loop.

    The classifier is patched to return a category the map has never seen: the
    lookup must fall back to the non-retryable bucket instead of raising.
    """
    monkeypatch.setattr(
        tfc,
        "classify_terminal_failure",
        lambda **_: SimpleNamespace(
            category="not_in_the_map", hint="hint", should_retry=False
        ),
    )
    result = tfc.classify_tool_failure("terminal", "boom", exit_code=2)
    assert result.category is tfc.ToolFailureCategory.persistent_error
