"""Tests for scripts/evolution_experience_harvest.py — deterministic harvest."""

import fcntl
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import evolution_experience_harvest as eh  # noqa: E402
from agent.experience_bank import get_harvest_state, iter_entries  # noqa: E402
from hermes_state import SessionDB  # noqa: E402

# Fixed clock: sessions are aged 1h behind "now" so they clear the 30-minute
# possibly-active grace window with room to spare.
NOW = 1_800_000_000.0
OLD = NOW - 3600.0

UNIQUE_USER_STRING = "zqxjvk-unique-user-string-9f8e7d"


def _make_db(tmp_path):
    db_path = tmp_path / "state.db"
    return SessionDB(db_path=db_path), db_path


def _add_session(
    db,
    sid,
    messages,
    *,
    source="cli",
    model="test-model",
    ended=True,
    ts=OLD,
):
    """Insert a session with messages, aged to *ts* (epoch seconds).

    ``create_session``/``end_session`` stamp real wall-clock time, so the row
    is back-dated with a direct UPDATE afterwards — the same approach
    existing SessionDB tests use for compression-chain fixtures.
    """
    db.create_session(sid, source, model=model)
    for m in messages:
        db.append_message(
            sid,
            m["role"],
            content=m.get("content"),
            tool_name=m.get("tool_name"),
            timestamp=m.get("ts", ts),
        )
    if ended:
        db.end_session(sid, "cli_close")
    db._conn.execute(
        "UPDATE sessions SET started_at=?, ended_at=? WHERE id=?",
        (ts, ts if ended else None, sid),
    )


def _user(text="do the thing"):
    return {"role": "user", "content": text}


def _assistant(text="Done."):
    return {"role": "assistant", "content": text}


def _tool_error(tool, content):
    return {"role": "tool", "tool_name": tool, "content": content}


def _tool_ok(tool, content):
    return {"role": "tool", "tool_name": tool, "content": content}


def _harvest(db, db_path, now=NOW):
    """Close the writer and run one harvest pass against the same file."""
    db.close()
    return eh.run_harvest(db_path=db_path, now=now)


def _entries():
    return list(iter_entries())


class TestSessionSelection:
    def test_active_session_skipped(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        recent = time.time()
        _add_session(db, "s-active", [_user(), _assistant()], ts=recent)
        summary = _harvest(db, db_path, now=time.time())
        assert summary["entries_appended"] == 0
        assert _entries() == []

    def test_dedup_second_run_processes_nothing_new(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-clean", [_user(), _assistant()])
        first = _harvest(db, db_path)
        assert first["entries_appended"] == 1

        # Second run: the session sits inside the cursor overlap window, so
        # only seen_ids dedups it — entries must not double.
        second = eh.run_harvest(db_path=db_path, now=NOW)
        assert second["sessions_harvested"] == 0
        assert second["entries_appended"] == 0
        assert len(_entries()) == 1

        state = get_harvest_state()
        assert state["cursor_ts"] == OLD
        assert "s-clean" in state["seen_ids"]


class TestVerdicts:
    def test_clean_session_success_low_confidence(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-clean", [_user(), _assistant("All finished.")])
        summary = _harvest(db, db_path)
        assert summary["entries_appended"] == 1
        (entry,) = _entries()
        assert entry.success is True
        assert entry.confidence == "low"
        assert entry.outcome_source == "heuristic:clean_completion"
        assert entry.terminal_reason == "completed"
        assert entry.failure_category is None
        assert entry.primary_dimension is None

    def test_loop_guard_hard_stop_is_strong_failure(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(
            db,
            "s-loopguard",
            [
                _user(),
                _tool_error("terminal", json.dumps({"exit_code": 1, "error": "boom"})),
                _assistant(
                    "[loop-guard] `terminal` has been failing repeatedly — "
                    "3 advisory warnings were ignored across 5 consecutive "
                    "calls with no course-correction."
                ),
            ],
        )
        _harvest(db, db_path)
        (entry,) = _entries()
        assert entry.success is False
        assert entry.confidence == "high"
        assert entry.outcome_source == "heuristic:loop_guard_hard_stop"
        assert entry.terminal_reason == "loop_guard_hard_stop"

    def test_unhandled_exception_is_strong_failure(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(
            db,
            "s-crash",
            [
                _user(),
                _assistant(
                    "I apologize, but I encountered an error while processing "
                    "the model response: boom"
                ),
            ],
        )
        _harvest(db, db_path)
        (entry,) = _entries()
        assert entry.success is False
        assert entry.confidence == "high"
        assert entry.outcome_source == "heuristic:unhandled_exception"
        assert entry.terminal_reason == "unhandled_exception"

    def test_recovered_only_errors_are_never_a_failure(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(
            db,
            "s-recovered",
            [
                _user(),
                _tool_error("terminal", json.dumps({"exit_code": 1, "error": "boom"})),
                _tool_ok("terminal", json.dumps({"exit_code": 0, "output": "ok"})),
                _assistant("Fixed it."),
            ],
        )
        _harvest(db, db_path)
        (entry,) = _entries()
        assert entry.success is not False
        assert entry.success is True  # recovered + clean end = clean completion
        assert "recovered=1" in entry.analysis
        assert "unrecovered=0" in entry.analysis


class TestDimensionAttribution:
    def test_timeout_dominant_honest_null(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(
            db,
            "s-timeout",
            [
                _user(),
                _tool_error(
                    "terminal",
                    json.dumps({"exit_code": 124, "error": "command timed out"}),
                ),
                _assistant("I could not finish in time."),
            ],
        )
        _harvest(db, db_path)
        (entry,) = _entries()
        assert entry.failure_category == "timeout"
        assert entry.tool == "terminal"
        assert entry.primary_dimension is None
        assert entry.secondary_dimensions == ["tool", "generation", "orchestration"]
        assert entry.success is None  # unrecovered errors, no strong signal

    def test_not_found_maps_to_context(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(
            db,
            "s-notfound",
            [
                _user(),
                _tool_error(
                    "read_file",
                    json.dumps({"success": False, "error": "File not found: /x/y.py"}),
                ),
                _assistant("The file was missing."),
            ],
        )
        _harvest(db, db_path)
        (entry,) = _entries()
        assert entry.failure_category == "not_found"
        assert entry.primary_dimension == "context"
        assert entry.secondary_dimensions == []

    def test_analysis_is_controlled_vocabulary_only(self, tmp_path):
        db, db_path = _make_db(tmp_path)
        _add_session(
            db,
            "s-vocab",
            [
                _user(UNIQUE_USER_STRING),
                _tool_error(
                    "terminal",
                    json.dumps({"exit_code": 1, "error": UNIQUE_USER_STRING}),
                ),
                _assistant(f"reply mentioning {UNIQUE_USER_STRING}"),
            ],
        )
        _harvest(db, db_path)
        (entry,) = _entries()
        assert UNIQUE_USER_STRING not in entry.analysis
        # The whole analysis string is built from scrubbed tokens + counts.
        import re

        assert re.fullmatch(
            r"tool=[a-z0-9_]+ category=[a-z_]+ unrecovered=\d+ recovered=\d+",
            entry.analysis,
        )


class TestDistillation:
    @pytest.fixture()
    def fake_distill(self, monkeypatch):
        """Fake sibling distiller; records calls. Returns the recorder."""
        import types

        calls = []

        def run_distillation(now=None):
            calls.append(now)
            return {"patterns": 2}

        module = types.ModuleType("evolution_experience_distill")
        module.run_distillation = run_distillation
        monkeypatch.setitem(sys.modules, "evolution_experience_distill", module)
        return calls

    def test_distill_invoked_when_entries_appended(self, tmp_path, fake_distill):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-clean", [_user(), _assistant()])
        summary = _harvest(db, db_path)
        assert summary["entries_appended"] == 1
        assert len(fake_distill) == 1
        assert summary["distill"] == {"patterns": 2}

    def test_distill_not_invoked_when_nothing_appended(self, tmp_path, fake_distill):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-active", [_user()], ended=False, ts=time.time())
        summary = _harvest(db, db_path, now=time.time())
        assert summary["entries_appended"] == 0
        assert summary["distill"] is None
        assert fake_distill == []

    def test_harvest_survives_distill_raising(self, tmp_path, fake_distill, capsys):
        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-clean", [_user(), _assistant()])

        import types

        mod = sys.modules["evolution_experience_distill"]
        original = mod.run_distillation

        def raising(now=None):
            raise RuntimeError("distill boom")

        mod.run_distillation = raising
        try:
            summary = _harvest(db, db_path)
        finally:
            mod.run_distillation = original
        assert summary["entries_appended"] == 1
        assert summary["distill"] is None
        assert "distillation failed" in capsys.readouterr().err


class TestLock:
    def test_second_concurrent_instance_exits_cleanly(self, tmp_path, capsys):
        from hermes_constants import get_hermes_home

        db, db_path = _make_db(tmp_path)
        _add_session(db, "s-clean", [_user(), _assistant()])
        db.close()

        lock_path = get_hermes_home() / "evolution" / "experience" / ".harvest.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = open(lock_path, "a+", encoding="utf-8")
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            rc = eh.main(["evolution_experience_harvest.py"])
        finally:
            fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
            holder.close()
        assert rc == 0
        assert "another harvest is running" in capsys.readouterr().out
        assert _entries() == []
