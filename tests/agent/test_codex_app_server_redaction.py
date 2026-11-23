"""Codex app-server rows reach the session DB redacted, like the chat path (#43666).

``build_assistant_message`` masks ``content`` and the unsigned reasoning copies before a
chat-completion row is stored. The app-server runtime builds its rows in
``CodexEventProjector`` and flushes them from ``_persist_projected_messages`` instead, so
before this fix a credential the model quoted in its reply or reasoning reached
``state.db`` (and the FTS index) verbatim.

The proof is taken at the session-DB writer boundary: the rows asserted here are the exact
list handed to ``_flush_messages_to_session_db``, so what is masked here is what is stored.
Redaction off keeps every row verbatim; tool-call arguments stay verbatim regardless
(#43083), because a provider replays them.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.codex_runtime import _persist_projected_messages, run_codex_app_server_turn
from agent.transports.codex_event_projector import CodexEventProjector

_SECRET = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_THOUGHT = f"I will use the token {_SECRET} to call the API."
_REPLY = f"Done. Your token is {_SECRET}."


def _completed(item):
    return {"method": "item/completed", "params": {"item": item}}


def _projected_turn(*items):
    """Build a turn through the real projector, so the row shape under test is the shipped one."""
    projector = CodexEventProjector()
    rows = []
    final_text = None
    for item in items:
        projection = projector.project(_completed(item))
        rows.extend(projection.messages)
        if projection.final_text is not None:
            final_text = projection.final_text
    return SimpleNamespace(
        interrupted=False,
        error=None,
        thread_id="thread-1",
        turn_id="turn-1",
        projected_messages=rows,
        tool_iterations=0,
        final_text=final_text,
        should_retire=False,
    )


def _tool_then_reply_turn():
    """A reasoning item, a tool call, a second reasoning item, then the reply — the shape in which
    the projector stashes reasoning onto the *tool* round as well as the final assistant row."""
    return _projected_turn(
        {"type": "reasoning", "id": "r1", "summary": [_THOUGHT], "content": []},
        {"type": "commandExecution", "id": "c1", "command": "gh auth status", "cwd": "/w",
         "aggregatedOutput": "ok", "exitCode": 0},
        {"type": "reasoning", "id": "r2", "summary": [], "content": [_THOUGHT]},
        {"type": "agentMessage", "id": "a1", "text": _REPLY},
    )


def _capturing_agent():
    """Stub agent whose session-DB writer records exactly the rows it was handed."""
    written = []

    def _flush(rows):
        written.append([dict(row) for row in rows])
        return True

    agent = MagicMock()
    agent._codex_session = MagicMock()
    agent._codex_session_prompt = None
    agent.tool_progress_callback = None
    agent._iters_since_skill = 0
    agent._skill_nudge_interval = 0
    agent.valid_tool_names = set()
    agent.session_id = "sess-codex"
    agent._session_db_created = True
    agent._session_db = MagicMock()  # present -> the flush branch is taken; also needs a real-ish API
    agent._flush_messages_to_session_db = _flush
    return agent, written


def _persisted_rows(monkeypatch, *, redaction_on=True):
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", redaction_on, raising=False)
    agent, written = _capturing_agent()
    messages = [{"role": "user", "content": "check my token"}]
    assert _persist_projected_messages(agent, _tool_then_reply_turn(), messages) is True
    assert len(written) == 1, "the turn must be flushed exactly once"
    return messages, written[0]


def test_codex_reply_and_reasoning_are_masked_before_the_db_write(monkeypatch):
    """Neither the reply nor the reasoning copy may carry the credential into state.db."""
    messages, rows = _persisted_rows(monkeypatch)

    assistant_rows = [row for row in rows if row.get("role") == "assistant"]
    assert len(assistant_rows) == 2, "one tool-call round plus the final reply"
    assert not any(_SECRET in str(row) for row in rows), "a credential survived to the DB writer"
    # The reasoning stashed on the tool round and the reply text are both masked, not dropped.
    tool_call_row, reply_row = assistant_rows
    assert tool_call_row.get("reasoning") and _SECRET not in tool_call_row["reasoning"]
    assert reply_row.get("reasoning") and _SECRET not in reply_row["reasoning"]
    assert reply_row.get("content") and _SECRET not in reply_row["content"]
    # Masked, not emptied: the row still says something.
    assert "ghp_" in reply_row["content"] and "..." in reply_row["content"]
    # The in-memory history the agent keeps is the stored copy too.
    assert not any(_SECRET in str(m.get("content")) + str(m.get("reasoning")) for m in messages)


def test_tool_call_arguments_stay_verbatim(monkeypatch):
    """Replay material is never masked (#43083) — only the readable text is."""
    _messages, rows = _persisted_rows(monkeypatch)

    tool_call_row = next(row for row in rows if row.get("role") == "assistant" and row.get("tool_calls"))
    arguments = tool_call_row["tool_calls"][0]["function"]["arguments"]
    assert "gh auth status" in arguments
    tool_row = next(row for row in rows if row.get("role") == "tool")
    # The projector carries exit status alongside output; the scrub must leave the whole row alone.
    assert "ok" in tool_row["content"] and '"exit_code": 0' in tool_row["content"]
    assert next(row for row in rows if row.get("role") == "user")["content"] == "check my token"


def test_redaction_off_keeps_codex_rows_verbatim(monkeypatch):
    """``security.redact_secrets: false`` keeps every row byte-identical."""
    _messages, rows = _persisted_rows(monkeypatch, redaction_on=False)

    tool_call_row, reply_row = [row for row in rows if row.get("role") == "assistant"]
    assert tool_call_row["reasoning"] == _THOUGHT
    assert reply_row["reasoning"] == _THOUGHT
    assert reply_row["content"] == _REPLY


def test_run_turn_wires_the_scrub_into_the_persist_path(monkeypatch):
    """Through the real entry point, not just the helper: the wiring is what was missing."""
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True, raising=False)
    agent, written = _capturing_agent()
    agent._codex_session.run_turn.return_value = _tool_then_reply_turn()

    result = run_codex_app_server_turn(
        agent,
        user_message="check my token",
        original_user_message="check my token",
        messages=[{"role": "user", "content": "check my token"}],
        effective_task_id="task-1",
    )

    assert result["completed"] is True
    assert result["agent_persisted"] is True
    assert written, "the turn must have reached the DB writer"
    assert not any(_SECRET in str(row) for row in written[0])
    assert not any(_SECRET in str(m) for m in result["messages"])


@pytest.mark.parametrize("row", [
    {"role": "user", "content": _REPLY},
    {"role": "tool", "tool_call_id": "c1", "content": _REPLY},
])
def test_non_assistant_rows_are_not_touched(row):
    """The scrub is scoped to assistant rows: user text and tool results are the caller's data."""
    from agent.codex_runtime import _redact_projected_assistant_row

    before = dict(row)
    _redact_projected_assistant_row(row)
    assert row == before


def test_tool_call_only_assistant_row_survives_the_scrub():
    """A tool-call round has ``content=None``; the scrub must not fabricate or drop anything."""
    from agent.codex_runtime import _redact_projected_assistant_row

    row = {"role": "assistant", "content": None,
           "tool_calls": [{"id": "c1", "type": "function",
                           "function": {"name": "exec_command", "arguments": "{\"command\": \"ls\"}"}}],
           "reasoning": _THOUGHT}
    _redact_projected_assistant_row(row)
    assert row["content"] is None
    assert row["tool_calls"][0]["function"]["arguments"] == "{\"command\": \"ls\"}"
    assert _SECRET not in row["reasoning"]


def test_a_real_session_db_holds_no_credential(tmp_path, monkeypatch):
    """End to end against sqlite, not the writer boundary: build the real agent, run the real turn,
    then read the ``messages`` table. This is the ground-truth form of the assertion above."""
    import sqlite3

    from hermes_state import SessionDB
    from run_agent import AIAgent

    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True, raising=False)
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    try:
        db.create_session(session_id="s1", source="cli", model="codex")
        agent = AIAgent(api_key="test-key", base_url="https://openrouter.ai/api/v1",
                        quiet_mode=True, skip_context_files=True, skip_memory=True,
                        session_db=db, session_id="s1")
        agent._session_db_created = True
        agent._codex_session = MagicMock()
        agent._codex_session.run_turn.return_value = _tool_then_reply_turn()
        agent.tool_progress_callback = None
        messages = [{"role": "user", "content": "check my token"}]
        agent._flush_messages_to_session_db(messages)  # turn-start flush owns the user row
        result = run_codex_app_server_turn(
            agent, user_message="check my token", original_user_message="check my token",
            messages=messages, effective_task_id="task-1")
    finally:
        db.close()

    assert result["completed"] is True and result["agent_persisted"] is True
    con = sqlite3.connect(db_path)
    rows = con.execute("SELECT role, content, reasoning, tool_calls FROM messages ORDER BY id").fetchall()
    con.close()
    assert len(rows) == 4, "user, tool-call round, tool result, final reply"
    assert not [r for r in rows if any(_SECRET in str(field) for field in r)], \
        "a credential reached sqlite"
    # Masked rather than dropped, and the tool round's arguments are untouched.
    reply_row = rows[-1]
    assert "ghp_" in reply_row[1] and "..." in reply_row[1]
    assert "gh auth status" in rows[1][3]

