"""Unit tests for agent runtime harness and kill-switch controls (Issue #3279)."""

from agent.runtime_harness import (
    AgentRuntimeHarness,
    HarnessAction,
    HarnessPolicy,
    HarnessStatus,
)


def test_harness_pause_and_resume():
    harness = AgentRuntimeHarness("sess-1")
    assert harness.check_pre_execution("read_file", {}).action == HarnessAction.PROCEED

    # Pause
    dec = harness.pause("user request")
    assert dec.action == HarnessAction.PAUSE
    assert harness.status == HarnessStatus.PAUSED
    assert harness.check_pre_execution("read_file", {}).action == HarnessAction.PAUSE

    # Resume
    dec_res = harness.resume()
    assert dec_res.action == HarnessAction.PROCEED
    assert harness.status == HarnessStatus.RUNNING


def test_harness_kill_switch():
    harness = AgentRuntimeHarness("sess-2")
    dec_kill = harness.kill("emergency stop")
    assert dec_kill.action == HarnessAction.KILL
    assert harness.status == HarnessStatus.KILLED

    # Pre-execution is blocked
    assert harness.check_pre_execution("terminal", {}).action == HarnessAction.KILL

    # Cannot resume
    assert harness.resume().action == HarnessAction.KILL


def test_harness_spiral_detection():
    policy = HarnessPolicy(max_unproductive_turns=3, kill_on_unhandled_spiral=True)
    harness = AgentRuntimeHarness("sess-3", policy=policy)

    harness.record_turn_result(has_productive_output=False, failed=True)
    harness.record_turn_result(has_productive_output=False, failed=True)
    dec = harness.record_turn_result(has_productive_output=False, failed=True)

    assert dec.action == HarnessAction.KILL
    assert harness.status == HarnessStatus.KILLED


def test_harness_checkpoint_restore():
    harness = AgentRuntimeHarness("sess-4")
    snapshot = {"memory": ["fact1", "fact2"], "messages_count": 10}
    cp = harness.create_checkpoint(snapshot)

    assert cp["session_id"] == "sess-4"
    assert len(harness.checkpoints) == 1

    restored = harness.restore_checkpoint(cp)
    assert restored["messages_count"] == 10
    assert harness.status == HarnessStatus.RUNNING
