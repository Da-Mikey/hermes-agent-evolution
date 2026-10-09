"""Pure metric helpers for the read-tool eval.

Deliberately stdlib-only and import-safe: ``runner.py`` needs a live agent and
a built workspace to run a task, but these helpers must be unit-testable with
neither (no API key, no model). Issue #165's claim — that the first chunk an
agent receives decides whether it ever finds a tail-side answer — is only
falsifiable if the first chunk is measured, so that number lives here.
"""

from __future__ import annotations


def _text_of(content) -> str:
    """Flatten a message's ``content`` (plain str or content-block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            c.get("text", "") for c in content if isinstance(c, dict)
        )
    return ""


def first_read_window(messages: list) -> str:
    """Text of the FIRST ``read_file`` tool result in ``messages``.

    That first result is the whole point of first-chunk selection: it is what
    the model had in front of it before it could decide to paginate. Agents
    rarely spend a turn paginating, so if the answer is not in this chunk the
    read has effectively failed even though the tool returned success.

    Returns ``""`` when the run never called ``read_file``. The id plumbing
    mirrors ``agent/loop_guard.py`` (assistant ``tool_calls[i]["id"]`` matched
    against a tool message's ``tool_call_id``).
    """
    call_id = None
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            if (tc.get("function") or {}).get("name") == "read_file":
                call_id = tc.get("id")
                break
        if call_id:
            break
    if not call_id:
        return ""
    for m in messages:
        if m.get("role") == "tool" and m.get("tool_call_id") == call_id:
            return _text_of(m.get("content"))
    return ""


def inclusion_coverage(gold: list, window: str) -> float:
    """Fraction of ``gold`` terms present in ``window`` (case-insensitive).

    This is the "gold-in-window" metric: 1.0 means every planted fact was
    inside the first chunk, and a low value on a task we already know the
    answer to means the harness hid the answer behind a pagination turn.

    ``1.0`` is returned when no gold terms are declared — nothing could be
    missed — which callers read as "not applicable" rather than as a win. An
    empty window with gold declared scores ``0.0``, the shape head-only
    truncation produces when the answer sits past the budget.
    """
    terms = [str(g) for g in (gold or []) if str(g).strip()]
    if not terms:
        return 1.0
    if not window:
        return 0.0
    low = window.lower()
    return sum(1 for t in terms if t.lower() in low) / len(terms)
