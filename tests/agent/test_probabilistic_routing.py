"""Unit tests for training-free probabilistic routing (Issue #3285)."""

import random

from agent.probabilistic_routing import (
    AgentBelief,
    ProbabilisticRouter,
    route_delegation_target,
)


def test_agent_belief_update():
    belief = AgentBelief(alpha=1.0, beta=1.0)
    assert belief.expected_value() == 0.5

    # Update with success
    belief.update(success=True, weight=2.0)
    assert belief.alpha == 3.0
    assert belief.beta == 1.0
    assert belief.expected_value() == 0.75

    # Update with failure
    belief.update(success=False, weight=1.0)
    assert belief.beta == 2.0


def test_probabilistic_router_selection():
    rng = random.Random(42)
    router = ProbabilisticRouter(rng=rng)

    candidates = ["fast_model", "capable_model", "fallback_model"]
    # Train capable_model with high successes
    for _ in range(15):
        router.record_outcome("capable_model", success=True)
    # Train fast_model with mostly failures
    for _ in range(15):
        router.record_outcome("fast_model", success=False)

    # In most trials, capable_model should be chosen
    selections = [
        router.select_target(candidates)
        for _ in range(50)
    ]
    assert selections.count("capable_model") > 40
    assert selections.count("fast_model") < 5


def test_probabilistic_router_task_priors():
    router = ProbabilisticRouter()
    router.record_outcome("code_agent", success=True, task_type="coding", weight=5.0)
    router.record_outcome("math_agent", success=True, task_type="math", weight=5.0)

    # For coding task, code_agent has higher expected value
    belief_code = router.get_or_create_belief("code_agent")
    belief_math = router.get_or_create_belief("math_agent")

    assert belief_code.expected_value(task_type="coding") > belief_math.expected_value(task_type="coding")


def test_probabilistic_router_serialization():
    router = ProbabilisticRouter()
    router.record_outcome("agent_a", success=True, weight=3.0)
    router.record_outcome("agent_b", success=False, weight=2.0)

    data = router.to_dict()
    restored = ProbabilisticRouter.from_dict(data)

    assert restored.beliefs["agent_a"].alpha == 4.0
    assert restored.beliefs["agent_b"].beta == 3.0


def test_route_delegation_target():
    candidates = ["agent_1", "agent_2"]
    choice = route_delegation_target(candidates)
    assert choice in candidates

    # Exclude choice
    choice_ex = route_delegation_target(candidates, exclude=["agent_1"])
    assert choice_ex == "agent_2"
