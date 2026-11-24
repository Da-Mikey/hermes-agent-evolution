"""Tests for #283 — busy-mode queue discards must be counted, not just logged.

The pending-queue discard branch logged one WARNING per dropped event and kept no
count: 645 discards over five days (103 in 38 seconds on 2026-10-24) left no
aggregate, so a nightly Home Assistant event storm was indistinguishable from
healthy silence.

Contract asserted here:
  * N consecutive discards produce exactly ONE edge WARNING, not N;
  * the discard count is retrievable (``busy_drop_snapshot``);
  * the first accepted follow-up emits one recovery line carrying the total and
    persists a state field for the freshness/escalation layer;
  * the window rolls over rather than pinning a session "congested" forever.
"""

from __future__ import annotations

import types

import pytest

from gateway.platforms.event import MessageType
from gateway.run_busy import (
    GatewayBusySessionMixin,
    _BUSY_DROP_STATE,
    _BUSY_DROP_WINDOW_SECONDS,
    busy_drop_snapshot,
)

SESSION = "agent:main:homeassistant:channel:ha_events:homeassistant"


def _event() -> types.SimpleNamespace:
    """Minimal MessageEvent stand-in — only the attributes the path reads."""
    return types.SimpleNamespace(
        source=object(),
        message_type=MessageType.TEXT,
        text="entity changed",
        metadata={},
        internal=False,
        allow_gateway_control=True,
        media_urls=[],
        _gateway_accepted=False,
    )


class _Harness(GatewayBusySessionMixin):
    _BUSY_QUEUE_MAX_PENDING = 2

    def __init__(self, depth: int):
        self.depth = depth
        self.adapter = types.SimpleNamespace(_pending_messages={})
        self.enqueued: list = []

    def _delivery_adapter_for(self, source):  # noqa: D401 - test double
        return self.adapter

    def _queue_depth(self, session_key, *, adapter=None):
        return self.depth

    def _enqueue_fifo(self, session_key, queued_event, adapter):
        self.enqueued.append(queued_event)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Never let a test touch the real ~/.hermes/state or leak counters."""
    import gateway.run_busy as run_busy

    _BUSY_DROP_STATE.clear()
    monkeypatch.setattr(run_busy, "_BUSY_DROP_STATE_FILE", tmp_path / "drops.json")
    yield
    _BUSY_DROP_STATE.clear()


def _warnings(caplog, needle: str) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelname == "WARNING" and needle in r.getMessage()
    ]


class TestOneLineNotMany:
    def test_five_discards_log_one_edge_warning(self, caplog):
        runner = _Harness(depth=_Harness._BUSY_QUEUE_MAX_PENDING)  # at cap
        with caplog.at_level("WARNING", logger="gateway.run"):
            for _ in range(5):
                runner._queue_or_replace_pending_event(SESSION, _event())

        edge = _warnings(caplog, "follow-up dropped")
        assert len(edge) == 1, f"expected one edge warning, got {len(edge)}"
        assert runner.enqueued == []

    def test_count_is_retrievable(self, caplog):
        runner = _Harness(depth=_Harness._BUSY_QUEUE_MAX_PENDING)
        with caplog.at_level("WARNING", logger="gateway.run"):
            for _ in range(5):
                runner._queue_or_replace_pending_event(SESSION, _event())

        snap = busy_drop_snapshot()
        assert snap[SESSION]["dropped"] == 5
        assert snap[SESSION]["cap"] == _Harness._BUSY_QUEUE_MAX_PENDING

    def test_snapshot_is_a_copy(self, caplog):
        runner = _Harness(depth=_Harness._BUSY_QUEUE_MAX_PENDING)
        with caplog.at_level("WARNING", logger="gateway.run"):
            runner._queue_or_replace_pending_event(SESSION, _event())
        snap = busy_drop_snapshot()
        snap[SESSION]["dropped"] = 999
        assert busy_drop_snapshot()[SESSION]["dropped"] == 1


class TestRecoveryEdge:
    def test_recovery_reports_total_once_and_clears(self, caplog):
        runner = _Harness(depth=_Harness._BUSY_QUEUE_MAX_PENDING)
        with caplog.at_level("WARNING", logger="gateway.run"):
            for _ in range(3):
                runner._queue_or_replace_pending_event(SESSION, _event())
            # Queue drains; the next follow-up is accepted.
            runner.depth = 0
            runner._queue_or_replace_pending_event(SESSION, _event())

        recovered = _warnings(caplog, "queue recovered")
        assert len(recovered) == 1
        assert "3 follow-up event(s)" in recovered[0]
        assert busy_drop_snapshot() == {}

    def test_recovery_persists_state_for_the_dead_man_layer(self, caplog, tmp_path):
        import json
        import gateway.run_busy as run_busy

        runner = _Harness(depth=_Harness._BUSY_QUEUE_MAX_PENDING)
        with caplog.at_level("WARNING", logger="gateway.run"):
            for _ in range(4):
                runner._queue_or_replace_pending_event(SESSION, _event())
            runner.depth = 0
            runner._queue_or_replace_pending_event(SESSION, _event())

        payload = json.loads(run_busy._BUSY_DROP_STATE_FILE.read_text(encoding="utf-8"))
        assert payload[SESSION]["dropped"] == 4
        assert "recovered_at" in payload[SESSION]

    def test_clean_accept_with_no_prior_drops_says_nothing(self, caplog):
        runner = _Harness(depth=0)
        with caplog.at_level("WARNING", logger="gateway.run"):
            runner._queue_or_replace_pending_event(SESSION, _event())
        assert _warnings(caplog, "queue recovered") == []
        assert len(runner.enqueued) == 1


class TestWindow:
    def test_stale_window_restarts_and_logs_a_fresh_edge(self, caplog):
        runner = _Harness(depth=_Harness._BUSY_QUEUE_MAX_PENDING)
        with caplog.at_level("WARNING", logger="gateway.run"):
            runner._queue_or_replace_pending_event(SESSION, _event())
            # Age the window past its bound: the session is no longer "congested".
            _BUSY_DROP_STATE[SESSION]["window_started"] -= _BUSY_DROP_WINDOW_SECONDS + 1
            runner._queue_or_replace_pending_event(SESSION, _event())

        assert len(_warnings(caplog, "follow-up dropped")) == 2
        assert busy_drop_snapshot()[SESSION]["dropped"] == 1
