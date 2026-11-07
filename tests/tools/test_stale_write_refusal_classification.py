"""#365: a stale-read write refusal is a DESIGNED refusal, retryable by instruction.

The refusal payload says ``stale_write_blocked: True`` and carries a
``next_action`` telling the model to re-read and write again — while the runtime
stamped it ``Non-retryable: persistent_error``. 43 of 45 refusals carried both;
40 sessions resolved the contradiction by retrying anyway, the rest stalled.

The envelope is built here by the REAL guard (``_stale_write_refusal``), not
hand-written, so the test cannot drift away from the payload it classifies.
"""

from scripts.introspection_extract import _expected_refusal_class
from tools.file_tools_write_guards import _stale_write_refusal
from tools.tool_failure_classifier import ToolFailureCategory, classify_tool_failure


def _envelope():
    """The payload write_file actually returns for a stale-read refusal."""
    return _stale_write_refusal(
        "/repo/notes/plan.md", "only the first 50 of 120 lines were read."
    )


def test_stale_write_refusal_is_retryable_by_instruction():
    payload = _envelope()
    assert payload["stale_write_blocked"] is True

    classification = classify_tool_failure("write_file", error=payload["error"])

    assert classification.category is ToolFailureCategory.designed_refusal
    assert classification.should_retry is True, (
        "the payload's own next_action tells the model to retry; the retry "
        "verdict must agree or every session has to resolve the contradiction "
        "on its own"
    )
    # The hint must point at the payload's recovery, not at "give up".
    assert "recovery" in classification.hint.lower()


def test_stale_write_refusal_is_not_published_as_non_retryable():
    """The label that reaches the model must not say non-retryable."""
    payload = _envelope()
    classification = classify_tool_failure("write_file", error=payload["error"])
    assert classification.should_retry is True
    # Should a future refactor re-derive the verdict, this names the two
    # categories that WOULD have been stamped before the fix.
    assert classification.category not in {
        ToolFailureCategory.persistent_error,
        ToolFailureCategory.unknown,
    }


def test_genuine_write_failure_stays_a_failure():
    """Negative fixture (#1a): a real I/O error must not borrow the refusal class.

    Without this, a rule loose enough to swallow every write error would pass the
    positive test above while silently hiding real failures.
    """
    classification = classify_tool_failure(
        "write_file", error="Permission denied: /repo/notes/plan.md"
    )

    assert classification.category is ToolFailureCategory.permission_denied
    assert classification.should_retry is False


def test_census_agrees_with_the_classifier():
    """The introspection census must call the same envelope a designed refusal."""
    assert _expected_refusal_class(_envelope()) == "stale_write"


def test_census_still_treats_a_plain_failure_as_a_failure():
    """Negative fixture for the census half of the contract."""
    assert (
        _expected_refusal_class({"error": "Permission denied: /repo/notes/plan.md"})
        is None
    )
