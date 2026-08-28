"""Unit tests for graph-structured failure attribution (Issue #3278)."""

from agent.graph_failure_attribution import (
    ExecutionNode,
    InfluenceGraph,
)


def test_influence_graph_build_and_attribute():
    trace = [
        {"stage": "research", "action": "web_search", "error": None},
        {"stage": "analysis", "action": "read_file", "error": None},
        {"stage": "implementation", "action": "patch", "error": "Hunk #1 failed: context mismatch"},
        {"stage": "validation", "action": "terminal", "error": "pytest failed with 1 error"},
    ]

    graph = InfluenceGraph.build_from_trace(trace)
    assert len(graph.nodes) == 4

    report = graph.attribute_failure()
    assert report is not None
    assert report.critical_node_id == "step_2"
    assert report.culprit_stage == "implementation"
    assert report.culprit_action == "patch"
    assert "Hunk #1 failed" in report.root_cause
    assert "step_0 -> step_1 -> step_2" in report.render_markdown()


def test_influence_graph_no_failure():
    trace = [
        {"stage": "research", "action": "web_search"},
        {"stage": "implementation", "action": "write_file"},
    ]
    graph = InfluenceGraph.build_from_trace(trace)
    report = graph.attribute_failure()
    assert report is not None
    assert "No explicit failure" in report.root_cause


def test_influence_graph_empty():
    graph = InfluenceGraph()
    assert graph.attribute_failure() is None
