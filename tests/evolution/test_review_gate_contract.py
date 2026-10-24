"""Contract tests for the review-label gate (``.github/workflows/review-labels.yml``).

These pin the properties that make ``ci-reviewed`` a real control rather than a
formality:

1. the exact label name the gate looks for,
2. that the gate FAILS CLOSED when the label is absent, and
3. that the gate is scoped to CI-sensitive / MCP-catalog / supply-chain changes
   rather than blocking every pull request.

Written as the smoke test for the independent-review control
(``scripts/evolution_pr_review.py`` in the operator's HERMES_HOME): a reviewer
that applies a label must be pinned against the gate it satisfies, so a silent
change to either side is caught by CI rather than noticed months later.
"""

from pathlib import Path


def _workflow_text() -> str:
    root = next(
        p for p in Path(__file__).resolve().parents
        if (p / ".github" / "workflows" / "review-labels.yml").exists()
    )
    return (root / ".github" / "workflows" / "review-labels.yml").read_text(
        encoding="utf-8"
    )


def test_gate_looks_for_the_exact_label_name():
    """The label name is a contract shared with whatever applies it."""
    assert "grep -Fxq 'ci-reviewed'" in _workflow_text()


def test_gate_fails_closed_when_label_absent():
    """Absent label => hard failure. If this inverts, the control is decorative."""
    text = _workflow_text()
    assert "Fail on missing label" in text
    assert "steps.label-check.outputs.ci_reviewed != 'true'" in text


def test_gate_is_scoped_to_sensitive_changes():
    """A label gate on every PR would be noise; scope is part of the contract."""
    assert (
        "inputs.ci_review || inputs.mcp_catalog || inputs.supply_chain"
        in _workflow_text()
    )
