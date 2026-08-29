"""AgentRuntimeHarness supervision wiring in _run_single_child (#3303).

Slice 1 of #3301: runtime_harness.py (#3279) was dead code; this pins the live
wiring — tool-event supervision, kill via hard interrupt, harness registry.
"""

import unittest
from unittest.mock import MagicMock

from agent.runtime_harness import HarnessPolicy
from tools import delegate_tool
from tools.delegate_tool import _run_single_child


def _child(sid, policy=None):
    c = MagicMock()
    c._subagent_id = sid
    c.enabled_toolsets = ["file"]  # concrete list: skip #2826 gate
    c._team_identity = None
    if policy is not None:
        c._runtime_harness_policy = policy
    return c


def _parent():
    p = MagicMock()
    p._delegate_depth = 0
    return p


# Non-empty tool trace keeps the shallow-retry path (issue #323) out.
_TRACE = [
    {"role": "assistant", "content": "", "tool_calls": [
        {"id": "tc1", "function": {"name": "read_file", "arguments": "{}"}}]},
    {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
]
_RESULT = {"final_response": "done", "completed": True, "interrupted": False,
           "api_calls": 1, "messages": _TRACE}


class TestHarnessWiring(unittest.TestCase):
    def test_completion_releases_harness_and_forwards_events(self):
        seen, orig = {}, []

        def _orig_cb(event, *a, **kw):
            orig.append(event)

        def _run(**_kw):
            seen["h"] = delegate_tool._SUBAGENT_HARNESSES.get("sa-x-done")
            child.tool_progress_callback("tool.started", "read_file", "p", {})
            return dict(_RESULT)

        child = _child("sa-x-done")
        child.tool_progress_callback = _orig_cb
        child.run_conversation.side_effect = _run
        res = _run_single_child(
            task_index=0, goal="Summarize the report", child=child,
            parent_agent=_parent(),
        )
        self.assertEqual(res["status"], "completed")
        self.assertIsNotNone(seen["h"])
        self.assertNotIn("harness_kill_reason", res)
        self.assertNotIn("sa-x-done", delegate_tool._SUBAGENT_HARNESSES)
        # Tool events flow through the supervisor unchanged to the original.
        self.assertEqual(orig, ["subagent.start", "tool.started", "subagent.complete"])

    def test_kill_mid_run_halts_and_records_reason(self):
        def _run(**_kw):
            delegate_tool._SUBAGENT_HARNESSES["sa-x-kill"].kill("emergency stop")
            return {"final_response": "partial", "completed": False,
                    "interrupted": True, "api_calls": 2, "messages": list(_TRACE)}

        child = _child("sa-x-kill")
        child.run_conversation.side_effect = _run
        res = _run_single_child(
            task_index=1, goal="Investigate the logs", child=child,
            parent_agent=_parent(),
        )
        self.assertEqual(res["harness_kill_reason"], "emergency stop")
        self.assertNotIn("sa-x-kill", delegate_tool._SUBAGENT_HARNESSES)

    def test_spiral_kill_dispatches_hard_interrupt(self):
        def _run(**_kw):
            # The rebound child callback IS the supervisor adapter.
            child.tool_progress_callback(
                "tool.completed", "terminal", None, None,
                duration=0.1, is_error=True,
            )
            return dict(_RESULT)

        child = _child(
            "sa-x-spiral",
            HarnessPolicy(max_unproductive_turns=1, kill_on_unhandled_spiral=True),
        )
        child.run_conversation.side_effect = _run
        res = _run_single_child(
            task_index=3, goal="Retry the upload", child=child,
            parent_agent=_parent(),
        )
        # request_hard_interrupt falls back to child.interrupt on mocks.
        self.assertTrue(child.interrupt.called or child.hard_interrupt.called)
        self.assertNotIn("sa-x-spiral", delegate_tool._SUBAGENT_HARNESSES)
        self.assertTrue(res["harness_kill_reason"].startswith("Max unproductive"))
