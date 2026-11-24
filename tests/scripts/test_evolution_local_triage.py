"""Tests for evolution_local_triage.py (#783).

Verifies the local triage pass:
- Reads local sidecar files without GitHub API calls
- Extracts filed proposals from issues sidecar
- Applies effort budget from health sidecar
- Detects consolidation mode from realized-impact sidecar
- Produces output in the standard analysis JSON format
- Does not clobber existing full (non-local) analysis files
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from evolution_local_triage import run_local_triage, _latest_file, _read_sidecar


@pytest.fixture
def evo_dir(tmp_path):
    """Create a minimal evolution directory with sidecars."""
    evo = tmp_path / "evolution"
    issues_dir = evo / "issues"
    issues_dir.mkdir(parents=True)
    (issues_dir / "2026-07-08.json").write_text(
        json.dumps({
            "date": "2026-07-08",
            "proposals": [
                {
                    "title": "Alpha",
                    "decision": "filed",
                    "issue": 100,
                    "priority_score": 1.5,
                    "impact": 0.8,
                    "effort": 0.3,
                },
                {
                    "title": "Beta",
                    "decision": "skipped",
                    "issue": None,
                    "priority_score": 0.5,
                    "impact": 0.2,
                    "effort": 0.1,
                },
                {
                    "title": "Gamma",
                    "decision": "filed",
                    "issue": 200,
                    "priority_score": 0.9,
                    "impact": 0.5,
                    "effort": 0.8,
                },
            ],
        })
    )
    return evo


def test_local_triage_basic(evo_dir):
    result = run_local_triage(evo_dir)
    assert result["local_triage"] is True
    assert result["date"]  # ISO date string
    assert "issues" in result["sidecars_read"]


def test_local_triage_excludes_skipped_proposals(evo_dir):
    result = run_local_triage(evo_dir)
    nums = [s["issue_number"] for s in result["selected_for_implementation"]]
    assert 100 in nums
    assert 200 in nums
    # Beta was "skipped" with issue=None — must not appear
    assert None not in nums


def test_local_triage_sorted_by_priority(evo_dir):
    result = run_local_triage(evo_dir)
    scores = [s["priority_score"] for s in result["selected_for_implementation"]]
    assert scores == sorted(scores, reverse=True)


def test_local_triage_effort_budget_default(evo_dir):
    """Without health sidecar, effort_budget defaults to 3.0."""
    result = run_local_triage(evo_dir)
    assert result["effort_budget"] == 3.0


def test_local_triage_effort_budget_throttled(evo_dir):
    """Health sidecar with LOW_SELECTION_EFFICIENCY sets budget to 1.5."""
    (evo_dir / "evolution-health.txt").write_text(
        "[evolution-metrics] LOW_SELECTION_EFFICIENCY effort_budget=1.5"
    )
    result = run_local_triage(evo_dir)
    assert result["effort_budget"] == 1.5


def test_local_triage_consolidation_mode(evo_dir):
    """Realized-impact sidecar with REALIZED_IMPACT_LOW sets consolidation."""
    (evo_dir / "realized-impact.txt").write_text(
        "[evolution-realized-impact] REALIZED_IMPACT_LOW"
    )
    result = run_local_triage(evo_dir)
    assert result["consolidation_mode"] is True


def test_local_triage_no_consolidation_without_signal(evo_dir):
    result = run_local_triage(evo_dir)
    assert result["consolidation_mode"] is False


def test_local_triage_effort_budget_enforced(evo_dir):
    """Total selected effort must not exceed the budget."""
    (evo_dir / "evolution-health.txt").write_text(
        "[evolution-metrics] effort_budget=1.5"
    )
    result = run_local_triage(evo_dir)
    total = sum(s["effort_score"] for s in result["selected_for_implementation"])
    assert total <= 1.5


def test_local_triage_standard_format(evo_dir):
    """Output must have the standard analysis JSON keys."""
    result = run_local_triage(evo_dir)
    assert "date" in result
    assert "rejected" in result
    assert "selected_for_implementation" in result
    for entry in result["selected_for_implementation"]:
        assert "issue_number" in entry
        assert "title" in entry
        assert "priority_score" in entry
        assert "selected_reason" in entry


def test_local_triage_empty_issues_dir(tmp_path):
    """If no sidecars exist, produce empty output without error."""
    evo = tmp_path / "evolution"
    evo.mkdir()
    result = run_local_triage(evo)
    assert result["local_triage"] is True
    assert result["selected_for_implementation"] == []
    assert result["sidecars_read"] == {}


def test_latest_file_returns_most_recent(tmp_path):
    """_latest_file should return the most recently modified file."""
    d = tmp_path / "issues"
    d.mkdir()
    old = d / "2026-07-01.json"
    new = d / "2026-07-08.json"
    old.write_text("{}")
    new.write_text("{}")
    # Ensure new is modified after old
    import os

    os.utime(old, (1, 1))
    os.utime(new, (2, 2))
    result = _latest_file(d)
    assert result == new


def test_read_sidecar_json(tmp_path):
    f = tmp_path / "test.json"
    f.write_text('{"key": "value"}')
    result = _read_sidecar(f)
    assert result["data"]["key"] == "value"


def test_read_sidecar_md(tmp_path):
    f = tmp_path / "test.md"
    f.write_text("# Research report\n\nContent here.")
    result = _read_sidecar(f)
    assert result["char_count"] > 0


def test_output_is_json_serializable_with_stage_result(tmp_path):
    """The triage output must survive json.dumps with the StageResult envelope
    attached (#1338 slice A).

    The envelope describes the very dict it is attached to, so carrying its
    ``result`` field would make output["stage_result"]["result"] point back at
    output — and json.dumps raises "Circular reference detected" on that. The
    gate swallows the failure (`|| true`), so the only visible symptom is a
    missing analysis file; this asserts serializability directly.
    """
    evolution_dir = tmp_path / "evolution"
    evolution_dir.mkdir()
    output = run_local_triage(evolution_dir)

    encoded = json.dumps(output, indent=2, ensure_ascii=False)
    assert json.loads(encoded) == output

    if "stage_result" in output:
        assert "result" not in output["stage_result"]
        assert output["stage_result"]["stage"] == "local_triage"


def test_local_triage_reconciles_unverified_already_implemented(tmp_path):
    """An issue marked already_implemented citing a non-existent file must be reconciled (#2494)."""
    evo = tmp_path / "evolution"
    issues_dir = evo / "issues"
    issues_dir.mkdir(parents=True)
    (issues_dir / "2026-08-16.json").write_text(
        json.dumps({
            "date": "2026-08-16",
            "proposals": [
                {
                    "title": "Unverified Fake Feature",
                    "decision": "already_implemented",
                    "issue": 300,
                    "priority_score": 1.8,
                    "impact": 0.9,
                    "effort": 0.5,
                    "reason": "Implemented in nonexistent/feature.py (lines 1-10)",
                },
                {
                    "title": "Real Implemented Feature",
                    "decision": "already_implemented",
                    "issue": 400,
                    "priority_score": 1.2,
                    "impact": 0.6,
                    "effort": 0.4,
                    "reason": "Implemented in real/existing.py (lines 1-10)",
                },
            ],
        })
    )

    repo_dir = tmp_path / "repo"
    real_file = repo_dir / "real" / "existing.py"
    real_file.parent.mkdir(parents=True)
    real_file.write_text("# existing code\n")

    result = run_local_triage(evo, repo_root=repo_dir)
    selected_issues = [s["issue_number"] for s in result["selected_for_implementation"]]

    # Issue 300 must be reconciled and selected because nonexistent/feature.py does not exist
    assert 300 in selected_issues
    # Issue 400 must NOT be selected because real/existing.py exists
    assert 400 not in selected_issues


# --- #308: the shape issues passes actually write -------------------------
#
# Every issues sidecar under evolution/issues/ stores its output as a top-level
# `filed` array; none carries the `proposals` container the fixture above uses.
# The local pass is the floor under the analysis stage, so reading the wrong
# container turned a fallback cycle into a silent zero-selection "quiet day".


def _real_shape_filed(n, priority_base=2.0):
    """A `filed[]` entry in the exact shape 2026-10-26-b.json writes."""
    return [
        {
            "letter": chr(ord("A") + i),
            "number": 400 + i,
            "url": f"https://github.com/Da-Mikey/hermes-agent-evolution/issues/{400 + i}",
            "title": f"[IMPROVEMENT] candidate {i}",
            "labels": ["enhancement", "proposal", "research-generated"],
            "source": f"research/2026-10-26.md {i}.1",
            "hook": f"scripts/thing_{i}.py:12",
            "priority_score": priority_base - (i * 0.1),
            "why_it_survived": f"measured reason {i}",
        }
        for i in range(n)
    ]


def _filed_sidecar(tmp_path, n, priority_base=2.0):
    evo = tmp_path / "evolution"
    issues_dir = evo / "issues"
    issues_dir.mkdir(parents=True)
    (issues_dir / "2026-10-26-b.json").write_text(
        json.dumps({
            "date": "2026-10-26",
            "stage": "issues",
            "filed": _real_shape_filed(n, priority_base),
            "budget": {"limit": 10, "filed_this_pass": n},
        })
    )
    return evo


def test_filed_shape_selects_issues_instead_of_zero(tmp_path):
    """A sidecar in the real `filed[]` shape must select, not silently yield (#308)."""
    evo = _filed_sidecar(tmp_path, 3)
    result = run_local_triage(evo)
    selected = result["selected_for_implementation"]

    assert [s["issue_number"] for s in selected] == [400, 401, 402]
    # priority ordering is preserved, highest first
    assert [s["priority_score"] for s in selected] == [2.0, 1.9, 1.8]


def test_filed_shape_maps_the_fields_it_has(tmp_path):
    """The mapping is lossless about what the sidecar records — and invents nothing."""
    evo = _filed_sidecar(tmp_path, 1)
    cand = run_local_triage(evo)["selected_for_implementation"][0]

    assert cand["issue_number"] == 400
    assert cand["title"] == "[IMPROVEMENT] candidate 0"
    assert cand["claimed_artifact"] == "scripts/thing_0.py:12"
    assert cand["reason"] == "research/2026-10-26.md 0.1"
    assert cand["notes"] == "measured reason 0"
    # No effort figure exists in `filed[]`; the sidecar's `budget` is a count.
    assert cand["effort_score"] == 0.0
    assert cand["effort_unknown"] is True


def test_filed_shape_caps_unknown_effort_selections(tmp_path):
    """Unknown effort costs 0.0 against the budget, so the list needs its own cap (#308)."""
    evo = _filed_sidecar(tmp_path, 7, priority_base=5.0)
    result = run_local_triage(evo)

    assert len(result["selected_for_implementation"]) == 4


def test_filed_shape_marks_the_report_degraded(tmp_path):
    """A fallback selection must be visible in the artifact, not look like a quiet day."""
    evo = _filed_sidecar(tmp_path, 2)
    result = run_local_triage(evo)

    assert result["degraded"] is True
    assert "#308" in result["degraded_reason"]
    # still a valid analysis document for downstream consumers
    assert result["local_triage"] is True
    assert result["effort_budget"] == 3.0


def test_proposals_container_is_not_marked_degraded(evo_dir):
    """The `proposals` path is unchanged and stays a normal (non-degraded) report."""
    result = run_local_triage(evo_dir)

    assert "degraded" not in result
    assert [s["issue_number"] for s in result["selected_for_implementation"]] == [
        100,
        200,
    ]
