"""A2A-compatible data models shared by the inbound/outbound bridge.

The field names and JSON shapes here track the Agent2Agent (A2A) protocol
specification (https://a2a-protocol.org/latest/specification): camelCase on the
wire, snake_case in Python. They are deliberately a *minimal* subset — enough to
describe an agent (``AgentCard``), send a request (``Message``), and read back a
result (``Task``). Optional protocol features (push notifications, streaming,
security schemes, signatures) are preserved on round-trip via the ``extra`` /
``metadata`` escape hatches but are not modelled as typed fields in this first
increment.

A2A and IBM's ACP converge on the same primitives: an agent descriptor, a
role/parts message, and a task with a status and artifacts. Keeping the model
protocol-neutral (no transport assumptions) lets a later increment add an ACP
serializer over the same objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# A2A task lifecycle states (subset). Mirrors the spec's TaskState enum values
# in their canonical lowercase-dashed wire form.
TASK_STATE_SUBMITTED = "submitted"
TASK_STATE_WORKING = "working"
TASK_STATE_INPUT_REQUIRED = "input-required"
TASK_STATE_COMPLETED = "completed"
TASK_STATE_FAILED = "failed"
TASK_STATE_CANCELED = "canceled"

# Message roles. A2A uses "user" for the requesting peer and "agent" for the
# responding peer.
ROLE_USER = "user"
ROLE_AGENT = "agent"


@dataclass
class MessagePart:
    """A single part of a message or artifact.

    A2A parts are a tagged union (text / file / data). This increment models the
    common ``text`` and ``data`` parts as typed fields and carries every other
    part kind (e.g. ``file``) verbatim through the ``extra`` escape hatch so a
    round-trip never drops data or crashes on an unmodeled kind.
    """

    text: Optional[str] = None
    data: Optional[Any] = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Any part fields this increment doesn't model (e.g. a ``file`` part's
    # ``file`` payload, or a non-text ``kind``) are preserved here so unknown
    # part types survive serialization losslessly.
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.extra)
        if self.text is not None:
            out["kind"] = "text"
            out["text"] = self.text
        elif self.data is not None:
            out["kind"] = "data"
            out["data"] = self.data
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MessagePart":
        text = raw.get("text")
        data = raw.get("data")
        # Drop ``kind`` from extra only for parts we re-derive it for (text /
        # data). For any other kind (e.g. ``file``) keep ``kind`` so to_dict
        # re-emits it verbatim — otherwise the tag would be lost on round-trip.
        known = {"text", "data", "metadata"}
        if text is not None or data is not None:
            known = known | {"kind"}
        return cls(
            text=text,
            data=data,
            metadata=dict(raw.get("metadata") or {}),
            extra={k: v for k, v in raw.items() if k not in known},
        )


@dataclass
class Message:
    """A request or response message exchanged between peers."""

    role: str
    parts: list[MessagePart] = field(default_factory=list)
    message_id: Optional[str] = None
    context_id: Optional[str] = None
    task_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_text(cls, text: str, *, role: str = ROLE_USER, message_id: Optional[str] = None) -> "Message":
        """Convenience constructor for a single-text-part message."""
        return cls(role=role, parts=[MessagePart(text=text)], message_id=message_id)

    def text(self) -> str:
        """Concatenate all text parts (artifacts often arrive as several)."""
        return "".join(p.text for p in self.parts if p.text is not None)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "role": self.role,
            "parts": [p.to_dict() for p in self.parts],
        }
        if self.message_id is not None:
            out["messageId"] = self.message_id
        if self.context_id is not None:
            out["contextId"] = self.context_id
        if self.task_id is not None:
            out["taskId"] = self.task_id
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Message":
        return cls(
            role=str(raw.get("role") or ROLE_AGENT),
            parts=[MessagePart.from_dict(p) for p in (raw.get("parts") or []) if isinstance(p, dict)],
            message_id=raw.get("messageId"),
            context_id=raw.get("contextId"),
            task_id=raw.get("taskId"),
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass
class TaskStatus:
    """The status of a task: a state plus an optional timestamp and message."""

    state: str
    timestamp: Optional[str] = None
    message: Optional[Message] = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"state": self.state}
        if self.timestamp is not None:
            out["timestamp"] = self.timestamp
        if self.message is not None:
            out["message"] = self.message.to_dict()
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TaskStatus":
        msg_raw = raw.get("message")
        return cls(
            state=str(raw.get("state") or TASK_STATE_SUBMITTED),
            timestamp=raw.get("timestamp"),
            message=Message.from_dict(msg_raw) if isinstance(msg_raw, dict) else None,
        )


@dataclass
class Artifact:
    """A named output produced by a task."""

    artifact_id: Optional[str] = None
    name: Optional[str] = None
    parts: list[MessagePart] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def text(self) -> str:
        return "".join(p.text for p in self.parts if p.text is not None)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"parts": [p.to_dict() for p in self.parts]}
        if self.artifact_id is not None:
            out["artifactId"] = self.artifact_id
        if self.name is not None:
            out["name"] = self.name
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Artifact":
        return cls(
            artifact_id=raw.get("artifactId"),
            name=raw.get("name"),
            parts=[MessagePart.from_dict(p) for p in (raw.get("parts") or []) if isinstance(p, dict)],
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass
class Task:
    """A unit of work tracked by an A2A agent, with status and artifacts."""

    id: str
    context_id: Optional[str] = None
    status: TaskStatus = field(default_factory=lambda: TaskStatus(state=TASK_STATE_SUBMITTED))
    artifacts: list[Artifact] = field(default_factory=list)
    history: list[Message] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> str:
        return self.status.state

    def is_terminal(self) -> bool:
        """True when the task has reached a state that won't change further."""
        return self.status.state in {
            TASK_STATE_COMPLETED,
            TASK_STATE_FAILED,
            TASK_STATE_CANCELED,
        }

    def result_text(self) -> str:
        """Best-effort flattening of artifact text — the structured result a
        delegating agent can act on. Falls back to the status message text when
        the task carried no artifacts (e.g. an ``input-required`` turn)."""
        artifact_text = "\n".join(a.text() for a in self.artifacts if a.text())
        if artifact_text:
            return artifact_text
        if self.status.message is not None:
            return self.status.message.text()
        return ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "status": self.status.to_dict(),
        }
        if self.context_id is not None:
            out["contextId"] = self.context_id
        if self.artifacts:
            out["artifacts"] = [a.to_dict() for a in self.artifacts]
        if self.history:
            out["history"] = [m.to_dict() for m in self.history]
        if self.metadata:
            out["metadata"] = dict(self.metadata)
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Task":
        status_raw = raw.get("status")
        return cls(
            id=str(raw.get("id") or ""),
            context_id=raw.get("contextId"),
            status=(
                TaskStatus.from_dict(status_raw)
                if isinstance(status_raw, dict)
                else TaskStatus(state=TASK_STATE_SUBMITTED)
            ),
            artifacts=[
                Artifact.from_dict(a) for a in (raw.get("artifacts") or []) if isinstance(a, dict)
            ],
            history=[
                Message.from_dict(m) for m in (raw.get("history") or []) if isinstance(m, dict)
            ],
            metadata=dict(raw.get("metadata") or {}),
        )


@dataclass
class AgentSkill:
    """A capability advertised by an agent in its AgentCard."""

    id: str
    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AgentSkill":
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            tags=[str(t) for t in (raw.get("tags") or [])],
        )


@dataclass
class AgentCapabilities:
    """Optional protocol features an agent supports.

    All default to False so a freshly built Hermes card claims only what this
    increment actually implements (synchronous request/response).
    """

    streaming: bool = False
    push_notifications: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "streaming": self.streaming,
            "pushNotifications": self.push_notifications,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AgentCapabilities":
        return cls(
            streaming=bool(raw.get("streaming", False)),
            push_notifications=bool(raw.get("pushNotifications", False)),
        )


@dataclass
class AgentCard:
    """A discoverable description of an agent and its skills.

    The A2A discovery convention serves this JSON at
    ``/.well-known/agent-card.json``. We model the load-bearing fields and keep
    everything else (security schemes, provider, signatures) under ``extra`` so
    parsing a richer peer card never loses data.
    """

    name: str
    description: str
    version: str
    url: str
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)
    default_input_modes: list[str] = field(default_factory=lambda: ["text/plain"])
    default_output_modes: list[str] = field(default_factory=lambda: ["text/plain"])
    skills: list[AgentSkill] = field(default_factory=list)
    protocol_version: str = "0.3.0"
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.extra)
        out.update(
            {
                "name": self.name,
                "description": self.description,
                "version": self.version,
                "url": self.url,
                "protocolVersion": self.protocol_version,
                "capabilities": self.capabilities.to_dict(),
                "defaultInputModes": list(self.default_input_modes),
                "defaultOutputModes": list(self.default_output_modes),
                "skills": [s.to_dict() for s in self.skills],
            }
        )
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AgentCard":
        known = {
            "name",
            "description",
            "version",
            "url",
            "protocolVersion",
            "capabilities",
            "defaultInputModes",
            "defaultOutputModes",
            "skills",
        }
        caps_raw = raw.get("capabilities")
        return cls(
            name=str(raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            version=str(raw.get("version") or ""),
            url=str(raw.get("url") or ""),
            protocol_version=str(raw.get("protocolVersion") or "0.3.0"),
            capabilities=(
                AgentCapabilities.from_dict(caps_raw)
                if isinstance(caps_raw, dict)
                else AgentCapabilities()
            ),
            default_input_modes=[str(m) for m in (raw.get("defaultInputModes") or ["text/plain"])],
            default_output_modes=[
                str(m) for m in (raw.get("defaultOutputModes") or ["text/plain"])
            ],
            skills=[AgentSkill.from_dict(s) for s in (raw.get("skills") or []) if isinstance(s, dict)],
            extra={k: v for k, v in raw.items() if k not in known},
        )


def build_hermes_agent_card(url: str, *, version: Optional[str] = None) -> AgentCard:
    """Build the AgentCard advertising this Hermes instance as an A2A peer.

    ``url`` is the externally reachable A2A endpoint for this instance (the
    caller owns deployment/routing). The version defaults to the installed
    Hermes version. Skills are derived from Hermes's registered toolsets so the
    card reflects what this build can actually do, rather than a hardcoded list
    that drifts.
    """
    if version is None:
        try:
            from hermes_cli import __version__ as hermes_version

            version = str(hermes_version)
        except Exception:
            version = "0.0.0"

    skills = _derive_skills_from_toolsets()
    return AgentCard(
        name="Hermes Agent",
        description=(
            "Self-improving open-source AI agent with persistent memory, skills, "
            "and rich tool support, exposed as an A2A peer for task delegation."
        ),
        version=version,
        url=url,
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["text/plain", "application/json"],
        default_output_modes=["text/plain", "application/json"],
        skills=skills,
    )


def _derive_skills_from_toolsets() -> list[AgentSkill]:
    """Map Hermes toolsets to A2A skills.

    Best-effort: if the toolset registry can't be imported (e.g. a partial
    install), fall back to a single generic skill so the card is still valid.
    """
    try:
        from toolsets import TOOLSETS
    except Exception:
        return [
            AgentSkill(
                id="general",
                name="General task execution",
                description="Execute a delegated task using Hermes' available tools.",
                tags=["general"],
            )
        ]

    skills: list[AgentSkill] = []
    for name in sorted(TOOLSETS):
        # Skip composite/platform/scenario toolsets — they aren't user-facing
        # capabilities so much as internal bundles.
        if name.startswith("hermes-") or name in {"safe", "debugging", "rl", "moa"}:
            continue
        defn = TOOLSETS.get(name) or {}
        description = str(defn.get("description") or f"Hermes {name} capability.")
        skills.append(
            AgentSkill(
                id=name,
                name=name.replace("_", " ").title(),
                description=description,
                tags=[name],
            )
        )
    if not skills:
        skills.append(
            AgentSkill(
                id="general",
                name="General task execution",
                description="Execute a delegated task using Hermes' available tools.",
                tags=["general"],
            )
        )
    return skills
