"""Unit tests for direction-aware context composition (Issue #3280)."""

from agent.direction_context import (
    HandoffDirection,
    compose_directional_context,
    detect_handoff_direction,
)


def test_detect_handoff_direction():
    assert detect_handoff_direction("gpt-4o-mini", "claude-3-5-sonnet") == HandoffDirection.ESCALATE
    assert detect_handoff_direction("claude-3-opus", "gpt-4o-mini") == HandoffDirection.DOWNSHIFT
    assert detect_handoff_direction("gpt-4o", "gpt-4o") == HandoffDirection.PEER


def test_compose_directional_context_escalate():
    turns = [
        {"role": "user", "content": "Fix memory leak in websocket server"},
        {"role": "tool", "content": "Error: OutOfMemoryError in handler.py:80"},
    ]
    ctx = compose_directional_context(
        turns,
        goal="Resolve leak",
        direction=HandoffDirection.ESCALATE,
        existing_context="Check file size",
    )
    assert "[DIRECTIONAL HANDOFF: ESCALATION TO FRONTIER MODEL]" in ctx
    assert "Fix memory leak" in ctx
    assert "OutOfMemoryError" in ctx
    assert "Check file size" in ctx


def test_compose_directional_context_downshift():
    turns = [{"role": "user", "content": "Run tests and format files"}]
    ctx = compose_directional_context(
        turns,
        goal="Format code with black",
        direction=HandoffDirection.DOWNSHIFT,
    )
    assert "[DIRECTIONAL HANDOFF: DOWNSHIFT TO FAST WORKER]" in ctx
    assert "Format code with black" in ctx
