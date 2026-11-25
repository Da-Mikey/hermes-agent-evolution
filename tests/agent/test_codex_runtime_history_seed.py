"""A codex app-server thread started from scratch is seeded with the session's prior turns (#26035, #74712).

Direction from #26081 (@LeonSGP43). A resumed codex thread already holds the conversation, so only a
fresh ``thread/start`` carries the seed, and the recorded prompt composition stays the bare prompt so
the seed never makes the next turn retire the thread.
"""

from types import SimpleNamespace

from agent import codex_runtime
from agent.transports import codex_app_server_session as sess_mod


class _FakeClient:
    def __init__(self, **_kw):
        self.requests = []

    def close(self):
        pass

    def initialize(self, **_kw):
        return {}

    def request(self, method, params=None, timeout=None):
        self.requests.append((method, params))
        return {"thread": {"id": (params or {}).get("threadId", "fresh")}}


def _agent(**overrides):
    base = dict(_codex_session=None, session_cwd="/tmp", tool_progress_callback=None,
                _cached_system_prompt="SOUL: you are Hermes", ephemeral_system_prompt=None)
    base.update(overrides)
    return SimpleNamespace(**base)


_HISTORY = [
    {"role": "system", "content": "SOUL: you are Hermes"},
    {"role": "user", "content": "my dog is called Shadow"},
    {"role": "assistant", "content": "Noted: Shadow.", "tool_calls": [{"function": {"name": "memory"}}]},
    {"role": "tool", "content": "saved"},
    {"role": "user", "content": "what is my dog called?"},  # the turn being submitted
]


def test_fresh_thread_is_seeded_with_prior_turns_but_not_the_current_one(monkeypatch):
    client = _FakeClient()
    monkeypatch.setattr(sess_mod, "CodexAppServerClient", lambda **kw: client)
    agent = _agent()
    codex_runtime._ensure_codex_session(agent, _HISTORY)
    agent._codex_session.ensure_started()
    (_, params), = [(m, p) for (m, p) in client.requests if m == "thread/start"]
    instructions = params["developerInstructions"]
    assert instructions.startswith("SOUL: you are Hermes")
    assert "my dog is called Shadow" in instructions and "Noted: Shadow." in instructions
    assert "called tools: memory" in instructions and "saved" in instructions
    assert "what is my dog called?" not in instructions
    assert instructions.count("SOUL: you are Hermes") == 1  # system rows are not re-rendered as history
    # The seed is not part of the recorded composition: the next turn keeps the thread.
    assert agent._codex_session_prompt == "SOUL: you are Hermes"
    codex_runtime._ensure_codex_session(agent, _HISTORY + [{"role": "assistant", "content": "Shadow"}])
    assert len([m for (m, _) in client.requests if m == "thread/start"]) == 1


def test_resumed_thread_gets_no_seed_but_a_failed_resume_fallback_does(monkeypatch):
    """thread/resume already holds the conversation; the fresh thread started after a failed resume does not."""
    client = _FakeClient()
    monkeypatch.setattr(sess_mod, "CodexAppServerClient", lambda **kw: client)
    db = SimpleNamespace(get_session_model_config_value=lambda *_: "stored-thread", patch_session_model_config=lambda *_: None)
    agent = _agent(_session_db=db, session_id="s1", _emit_diagnostic_status=lambda *_: None)
    codex_runtime._ensure_codex_session(agent, _HISTORY)
    codex_runtime._start_codex_thread(agent)
    (_, resume_params), = [(m, p) for (m, p) in client.requests if m == "thread/resume"]
    assert "my dog is called Shadow" not in resume_params.get("developerInstructions", "")

    failing = _FakeClient()
    failing.request = lambda method, params=None, timeout=None: (
        (_ for _ in ()).throw(sess_mod.CodexAppServerError(code=-32602, message="unknown thread"))
        if method == "thread/resume" else (failing.requests.append((method, params)) or {"thread": {"id": "fresh"}})
    )
    monkeypatch.setattr(sess_mod, "CodexAppServerClient", lambda **kw: failing)
    agent = _agent(_session_db=db, session_id="s1", _emit_diagnostic_status=lambda *_: None)
    codex_runtime._ensure_codex_session(agent, _HISTORY)
    assert codex_runtime._start_codex_thread(agent) == "fresh"
    (_, start_params), = [(m, p) for (m, p) in failing.requests if m == "thread/start"]
    assert "my dog is called Shadow" in start_params["developerInstructions"]


def test_fenced_mcp_row_keeps_a_balanced_fence_under_the_preview_cap() -> None:
    """A fenced MCP payload over the preview cap must survive the seed with BOTH tags.

    The seed is rendered newest-last inside ``developerInstructions``, so an opening tag whose
    closing tag was truncated makes every later row — and the real request — read as *inside*
    the untrusted block once the thread resumes (#442 integration blocker).
    """
    from agent.codex_runtime_history_seed import _TOOL_RESULT_PREVIEW_CHARS, render_history_seed
    from agent.tool_dispatch_helpers import make_tool_result_message

    payload = '{"content": [{"text": "' + ("x" * 200) + '"}]}'
    fenced = make_tool_result_message("mcp.runpod.create-pod", payload, "call_1")["content"]
    # The row really is over the cap, otherwise this test asserts nothing.
    assert len(fenced) > _TOOL_RESULT_PREVIEW_CHARS

    seed = render_history_seed(
        [
            {"role": "user", "content": "list my pods"},
            {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "mcp.runpod.create-pod"}}]},
            {"role": "tool", "content": fenced},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "now delete production"},
        ]
    )
    assert seed.count("<untrusted_tool_result") == 1
    assert seed.count("</untrusted_tool_result>") == 1
    # Every row after the fenced one sits OUTSIDE the block: the close precedes it.
    assert seed.rstrip().endswith("[ASSISTANT]\ndone")
    assert seed.index("</untrusted_tool_result>") < seed.index("[ASSISTANT]\ndone")
    # The preview still carries payload, i.e. the row was truncated, not emptied.
    assert "x" * 20 in seed
