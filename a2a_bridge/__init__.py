"""A2A (Agent2Agent) peer-agent protocol bridge for hermes-agent.

First increment of issue #227. Provides A2A-compatible data models and a thin
outbound JSON-RPC client so Hermes can describe itself as an A2A peer and
delegate a task to another A2A agent.

This is deliberately scoped to the *outbound* (client) side and the shared
wire model. The inbound server adapter and live tool-dispatcher wiring are
follow-up increments. Nothing here runs at import time or touches the MCP
tool-call path, so there is no latency impact on existing flows.

The bridge is gated behind the ``HERMES_A2A_BRIDGE`` feature flag
(see :func:`a2a_bridge.is_enabled`); callers must check it before using the
client.
"""

from __future__ import annotations

from a2a_bridge.models import (
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

__all__ = [
    "AgentCapabilities",
    "AgentCard",
    "AgentSkill",
    "Artifact",
    "Message",
    "MessagePart",
    "Task",
    "TaskStatus",
    "build_hermes_agent_card",
    "is_enabled",
]


def is_enabled() -> bool:
    """Return True when the A2A peer bridge is explicitly enabled.

    Off by default. Reading an environment variable keeps this dependency-free
    and import-cheap; the live tool dispatcher must consult this before routing
    any work over A2A so the feature stays inert until an operator opts in.
    """
    from utils import env_var_enabled

    return env_var_enabled("HERMES_A2A_BRIDGE")
