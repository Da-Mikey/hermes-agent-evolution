"""Tests for the A2A bridge data models — wire-shape round-trips and helpers."""

from __future__ import annotations

from a2a_bridge.models import (
    ROLE_AGENT,
    ROLE_USER,
    TASK_STATE_COMPLETED,
    TASK_STATE_INPUT_REQUIRED,
    AgentCapabilities,
    AgentCard,
    AgentSkill,
    Artifact,
    Message,
    MessagePart,
    Task,
    TaskStatus,
    build_hermes_agent_card,
)


# ── MessagePart ─────────────────────────────────────────────────────────────


def test_message_part_text_round_trip():
    part = MessagePart(text="hello")
    raw = part.to_dict()
    assert raw == {"kind": "text", "text": "hello"}
    assert MessagePart.from_dict(raw).text == "hello"


def test_message_part_data_round_trip():
    payload = {"ticketNumber": "REQ123"}
    part = MessagePart(data=payload, metadata={"mediaType": "application/json"})
    raw = part.to_dict()
    assert raw["kind"] == "data"
    assert raw["data"] == payload
    assert raw["metadata"] == {"mediaType": "application/json"}
    back = MessagePart.from_dict(raw)
    assert back.data == payload
    assert back.metadata == {"mediaType": "application/json"}


def test_message_part_unmodeled_file_kind_round_trips_losslessly():
    # A 'file' part is not modeled as a typed field; it must survive a
    # round-trip verbatim via the extra escape hatch (no data dropped, no
    # crash on the unknown kind).
    raw = {
        "kind": "file",
        "file": {"uri": "https://x/report.pdf", "mimeType": "application/pdf"},
        "metadata": {"source": "peer"},
    }
    part = MessagePart.from_dict(raw)
    assert part.text is None
    assert part.data is None
    assert part.extra["kind"] == "file"
    assert part.to_dict() == raw


# ── Message ─────────────────────────────────────────────────────────────────


def test_message_from_text_and_serialization():
    msg = Message.from_text("do the thing", message_id="m1")
    raw = msg.to_dict()
    assert raw["role"] == ROLE_USER
    assert raw["messageId"] == "m1"
    assert raw["parts"] == [{"kind": "text", "text": "do the thing"}]


def test_message_round_trip_with_context_and_task_ids():
    msg = Message(
        role=ROLE_AGENT,
        parts=[MessagePart(text="part-a"), MessagePart(text="part-b")],
        message_id="m2",
        context_id="ctx-1",
        task_id="task-1",
        metadata={"k": "v"},
    )
    back = Message.from_dict(msg.to_dict())
    assert back.role == ROLE_AGENT
    assert back.text() == "part-apart-b"
    assert back.context_id == "ctx-1"
    assert back.task_id == "task-1"
    assert back.metadata == {"k": "v"}


def test_message_from_dict_defaults_role_to_agent():
    # A peer reply that omits role should be read as an agent message.
    back = Message.from_dict({"parts": [{"text": "x"}]})
    assert back.role == ROLE_AGENT


# ── Task / TaskStatus / Artifact ──────────────────────────────────────────────


def test_task_parses_spec_response_shape():
    # The exact shape from the A2A spec's structured-data example.
    raw = {
        "id": "d8c6243f",
        "contextId": "c295ea44",
        "status": {"state": "completed", "timestamp": "2025-04-17T17:47:09Z"},
        "artifacts": [
            {
                "artifactId": "c5e0382f",
                "parts": [{"text": '[{"ticketNumber":"REQ123"}]'}],
            }
        ],
    }
    task = Task.from_dict(raw)
    assert task.id == "d8c6243f"
    assert task.context_id == "c295ea44"
    assert task.state == TASK_STATE_COMPLETED
    assert task.is_terminal() is True
    assert task.result_text() == '[{"ticketNumber":"REQ123"}]'


def test_task_round_trip():
    task = Task(
        id="t1",
        context_id="c1",
        status=TaskStatus(state=TASK_STATE_COMPLETED, timestamp="2025-01-01T00:00:00Z"),
        artifacts=[Artifact(artifact_id="a1", name="result", parts=[MessagePart(text="done")])],
    )
    back = Task.from_dict(task.to_dict())
    assert back.id == "t1"
    assert back.context_id == "c1"
    assert back.state == TASK_STATE_COMPLETED
    assert back.artifacts[0].name == "result"
    assert back.result_text() == "done"


def test_task_non_terminal_states():
    assert Task(id="x", status=TaskStatus(state="working")).is_terminal() is False
    assert (
        Task(id="x", status=TaskStatus(state=TASK_STATE_INPUT_REQUIRED)).is_terminal()
        is False
    )


def test_task_result_text_falls_back_to_status_message():
    # An input-required turn carries no artifacts; the prompt lives in the
    # status message, which result_text() should surface.
    task = Task(
        id="t2",
        status=TaskStatus(
            state=TASK_STATE_INPUT_REQUIRED,
            message=Message(role=ROLE_AGENT, parts=[MessagePart(text="need more info")]),
        ),
    )
    assert task.result_text() == "need more info"


# ── AgentCard ─────────────────────────────────────────────────────────────────


def test_agent_card_round_trip_preserves_unknown_fields():
    raw = {
        "name": "GeoSpatial Route Planner Agent",
        "description": "Route planning.",
        "version": "1.2.0",
        "url": "https://georoute.example.com/a2a/v1",
        "protocolVersion": "0.3.0",
        "capabilities": {"streaming": True, "pushNotifications": True},
        "defaultInputModes": ["application/json"],
        "defaultOutputModes": ["image/png"],
        "skills": [
            {
                "id": "route-optimizer",
                "name": "Route Optimizer",
                "description": "Optimal routes.",
                "tags": ["maps", "routing"],
            }
        ],
        # Fields this increment doesn't model as typed attributes:
        "provider": {"organization": "Example Inc."},
        "securitySchemes": {"google": {}},
    }
    card = AgentCard.from_dict(raw)
    assert card.name == "GeoSpatial Route Planner Agent"
    assert card.capabilities.streaming is True
    assert card.capabilities.push_notifications is True
    assert card.skills[0].id == "route-optimizer"
    assert card.skills[0].tags == ["maps", "routing"]

    out = card.to_dict()
    # Unknown fields survive the round-trip via the extra escape hatch.
    assert out["provider"] == {"organization": "Example Inc."}
    assert out["securitySchemes"] == {"google": {}}
    assert out["capabilities"] == {"streaming": True, "pushNotifications": True}


def test_agent_capabilities_default_to_false():
    caps = AgentCapabilities()
    assert caps.streaming is False
    assert caps.push_notifications is False


def test_agent_skill_round_trip():
    skill = AgentSkill(id="s1", name="Skill One", description="d", tags=["a", "b"])
    assert AgentSkill.from_dict(skill.to_dict()) == skill


# ── build_hermes_agent_card ───────────────────────────────────────────────────


def test_build_hermes_agent_card_basic_shape():
    card = build_hermes_agent_card("https://hermes.example.com/a2a", version="9.9.9")
    assert card.name == "Hermes Agent"
    assert card.version == "9.9.9"
    assert card.url == "https://hermes.example.com/a2a"
    assert "text/plain" in card.default_input_modes
    # Skills are derived from real Hermes toolsets, so there must be several.
    assert len(card.skills) >= 1
    # Card must serialize to a valid, parseable A2A AgentCard.
    assert AgentCard.from_dict(card.to_dict()).version == "9.9.9"


def test_build_hermes_agent_card_skills_from_toolsets():
    card = build_hermes_agent_card("https://hermes.example.com/a2a", version="1.0.0")
    skill_ids = {s.id for s in card.skills}
    # 'web' is a standard, stable Hermes toolset; composite/internal ones are
    # excluded.
    assert "web" in skill_ids
    assert not any(sid.startswith("hermes-") for sid in skill_ids)
    assert "safe" not in skill_ids
