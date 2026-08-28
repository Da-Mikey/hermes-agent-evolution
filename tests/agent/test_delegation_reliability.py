"""Unit tests for delegation-scoped reliability primitives (Issue #3282)."""

from agent.delegation_reliability import (
    DelegationEvidence,
    DelegationIdentity,
    DelegationReliabilityEvaluator,
    ReliabilityAction,
)


def test_delegation_identity():
    identity = DelegationIdentity.create(
        goal="Run integration tests",
        parent_session_id="session-123",
        depth=1,
    )
    assert identity.parent_session_id == "session-123"
    assert len(identity.goal_hash) == 12
    assert "session-123" in identity.key


def test_evaluate_outcome_success():
    evaluator = DelegationReliabilityEvaluator()
    identity = DelegationIdentity.create("Goal", "sess-1")
    evidence = DelegationEvidence(
        tools_executed=["terminal", "read_file"],
        has_meaningful_output=True,
        schema_valid=True,
    )
    decision = evaluator.evaluate_outcome(identity, evidence, success=True)
    assert decision.action == ReliabilityAction.PROCEED
    assert decision.can_retry is False


def test_evaluate_outcome_non_idempotent_abort():
    evaluator = DelegationReliabilityEvaluator()
    identity = DelegationIdentity.create("Send money", "sess-1")
    evidence = DelegationEvidence(
        tools_executed=["mcp_payment_tool"],
        state_mutations={"account_balance"},
        is_idempotent=False,
    )
    decision = evaluator.evaluate_outcome(identity, evidence, success=False)
    assert decision.action == ReliabilityAction.ABORT_NON_IDEMPOTENT
    assert decision.can_retry is False
    assert "non-idempotent" in decision.reason


def test_evaluate_outcome_stalled_progress():
    evaluator = DelegationReliabilityEvaluator()
    identity = DelegationIdentity.create("Refactor", "sess-1")
    evidence = DelegationEvidence(
        tools_executed=[],
        state_mutations=set(),
        token_delta=0,
    )
    decision = evaluator.evaluate_outcome(identity, evidence, success=False)
    assert decision.action == ReliabilityAction.STALLED_NO_PROGRESS
    assert decision.can_retry is False


def test_evaluate_outcome_retry_and_exhaustion():
    evaluator = DelegationReliabilityEvaluator(max_retries=2)
    identity = DelegationIdentity.create("Run tests", "sess-1")
    evidence = DelegationEvidence(
        tools_executed=["terminal"],
        token_delta=100,
        is_idempotent=True,
    )

    # Attempt 1 -> retry
    d1 = evaluator.evaluate_outcome(identity, evidence, success=False)
    assert d1.action == ReliabilityAction.RETRY
    assert d1.can_retry is True

    # Attempt 2 -> retry
    d2 = evaluator.evaluate_outcome(identity, evidence, success=False)
    assert d2.action == ReliabilityAction.RETRY
    assert d2.can_retry is True

    # Attempt 3 -> exhausted
    d3 = evaluator.evaluate_outcome(identity, evidence, success=False)
    assert d3.action == ReliabilityAction.ABORT_NON_IDEMPOTENT
    assert d3.can_retry is False
