"""Tests for adaptive handoff format selection and graph context extraction (Issue #3283)."""

from unittest.mock import MagicMock, patch

from agent.handoff_router import (
    DependencyGraphContext,
    extract_dependency_graph,
    select_handoff_format,
)
from tools.delegate_tool import _apply_handoff_collapse


def test_extract_dependency_graph():
    turns = [
        {"role": "user", "content": "Please review agent/write_guard.py and fix the bug in tests/agent/test_write_guard.py"},
        {
            "role": "assistant",
            "content": "I will run pytest.",
            "tool_calls": [
                {
                    "name": "terminal",
                    "function": {"name": "terminal", "arguments": "pytest"},
                }
            ],
        },
        {"role": "tool", "content": "FAILED test_write_guard.py line 45: assertion error"},
    ]

    graph = extract_dependency_graph(turns)
    assert "agent/write_guard.py" in graph.files_referenced
    assert "tests/agent/test_write_guard.py" in graph.files_referenced
    assert "terminal" in graph.tools_executed
    assert any("FAILED" in err for err in graph.recent_errors)


def test_dependency_graph_render_markdown():
    graph = DependencyGraphContext(
        files_referenced={"src/main.py", "tests/test_main.py"},
        tools_executed=["read_file", "patch", "terminal"],
        recent_errors=["ImportError: cannot import foo"],
        key_facts=["Keep backward compatibility"],
    )
    rendered = graph.render_markdown()
    assert "[HANDOFF DEPENDENCY GRAPH" in rendered
    assert "src/main.py" in rendered
    assert "tests/test_main.py" in rendered
    assert "read_file -> patch -> terminal" in rendered
    assert "ImportError" in rendered
    assert "Keep backward compatibility" in rendered


def test_select_handoff_format():
    code_turns = [
        {"role": "user", "content": "Fix the pytest failure in tools/terminal_tool.py"}
    ]
    assert select_handoff_format(code_turns, goal="Fix bug in terminal_tool.py") == "graph"

    prose_turns = [
        {"role": "user", "content": "Tell me a story about space exploration."}
    ]
    assert select_handoff_format(prose_turns, goal="Write a creative story") == "collapsed_summary"

    # Explicit override honored
    assert select_handoff_format(code_turns, goal="Fix bug", requested_mode="collapsed_summary") == "collapsed_summary"
    assert select_handoff_format(prose_turns, goal="Story", requested_mode="graph") == "graph"


def test_apply_handoff_collapse_graph_mode():
    parent_agent = MagicMock()
    parent_agent._delegate_handoff_messages = [
        {"role": "system", "content": "system instructions"},
        {"role": "user", "content": "Look at agent/route.py"},
        {"role": "assistant", "content": "Delegating...", "tool_calls": [{"name": "delegate_task"}]},
    ]
    tasks = [{"goal": "Inspect route", "context": "existing context"}]

    _apply_handoff_collapse(tasks, "graph", parent_agent)
    assert "[HANDOFF DEPENDENCY GRAPH" in tasks[0]["context"]
    assert "agent/route.py" in tasks[0]["context"]
    assert "existing context" in tasks[0]["context"]
