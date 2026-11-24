"""Cron prompt template-marker expansion (#418).

A scheduled job whose prompt carries ``{current_date}`` used to hand the marker to the agent
verbatim; a run that copied its goal into ``delegate_task`` then hit the unexpanded-marker guard in
``tools.delegate_tool_tasks`` and the delegated work was silently refused. These tests pin the fix
at the composition site — and the last test asserts the property that actually matters: the
composed prompt no longer contains anything the delegate guard would reject.
"""

from __future__ import annotations

import datetime as _dt

import pytest

from cron import scheduler_prompt


TODAY = "2026-11-21"


@pytest.fixture()
def frozen_clock(monkeypatch):
    """Pin the household clock so the assertions don't depend on the day the suite runs."""

    class _FixedDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 21, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(
        scheduler_prompt,
        "_hermes_now",
        lambda: _FixedDatetime(2026, 11, 21, 12, 0, 0),
    )
    return TODAY


@pytest.mark.parametrize("marker", scheduler_prompt._DATE_MARKERS)
def test_every_date_marker_form_is_expanded(marker, frozen_clock):
    assert (
        scheduler_prompt._expand_prompt_markers(f"Report for {marker}.")
        == f"Report for {TODAY}."
    )


def test_marker_inside_a_sentence_is_expanded(frozen_clock):
    text = "Write the digest to notes/{current_date}-digest.md"
    assert scheduler_prompt._expand_prompt_markers(text) == (
        f"Write the digest to notes/{TODAY}-digest.md"
    )


@pytest.mark.parametrize(
    "text",
    [
        "<feature_name>",  # carries information the dispatcher does not have
        "{{file path}}",
        "Vec<T>",  # legitimate generic syntax, never a template
        '{"key": 1}',  # JSON snippet
        "{a,b}",  # glob braces
        "{i}",  # f-string style
        "<div>",
    ],
)
def test_unresolvable_or_legitimate_braces_are_left_alone(text, frozen_clock):
    assert scheduler_prompt._expand_prompt_markers(text) == text


def test_no_marker_leaves_text_byte_identical(frozen_clock):
    text = "Just a plain prompt with no placeholders at all."
    assert scheduler_prompt._expand_prompt_markers(text) == text


def test_empty_and_none_are_safe(frozen_clock):
    assert scheduler_prompt._expand_prompt_markers("") == ""
    assert scheduler_prompt._expand_prompt_markers(None) is None


def test_broken_clock_leaves_text_untouched(monkeypatch):
    """A clock failure must not corrupt the prompt or kill the run — the tool guard still refuses."""

    def _boom():
        raise RuntimeError("clock unavailable")

    monkeypatch.setattr(scheduler_prompt, "_hermes_now", _boom)
    text = "Digest for {date}"
    assert scheduler_prompt._expand_prompt_markers(text) == text


def test_build_job_prompt_expands_the_job_prompt(frozen_clock):
    job = {"id": "mtest-418", "prompt": "Summarise the household for {current_date}."}
    built = scheduler_prompt._build_job_prompt(job, prerun_script=(True, "data"))
    assert f"Summarise the household for {TODAY}." in built
    assert "{current_date}" not in built


def test_build_job_prompt_expands_run_context(frozen_clock):
    job = {"id": "mtest-418b", "prompt": "Do the thing."}
    built = scheduler_prompt._build_job_prompt(job, extra_prompt="Window: {date}")
    assert f"Window: {TODAY}" in built
    assert "{date}" not in built


def test_composed_job_prompt_cannot_trip_the_delegate_marker_guard(frozen_clock):
    """The end-to-end property: what the dispatcher emits is no longer what the guard rejects."""
    from tools.delegate_tool_tasks import _TEMPLATE_MARKER_RE

    job = {"id": "mtest-418c", "prompt": "Digest for {current_date} run on {date}."}
    built = scheduler_prompt._build_job_prompt(job)

    assert not _TEMPLATE_MARKER_RE.search(built), (
        "composed cron prompt still contains a marker the delegate_task guard refuses: "
        f"{_TEMPLATE_MARKER_RE.search(built).group(0)!r}"
    )
