# -*- coding: utf-8 -*-
"""A rejected harness edit must inform the next proposal for that surface (#203).

The self-evolution loop invokes the proposer and the diagnosis in *separate*
subagent invocations, so "the next proposal" is a different process reading
persisted state — not the same object in memory. These tests therefore assert
the loop through disk: rejection recorded by one instance, context read back by
a freshly constructed one.

The behaviour worth guarding is asymmetric: a surface with no rejection history
must render exactly nothing (so the memory is purely additive and a clean
surface's input does not change), while the rejected surface must render the
diagnosis — and only that surface's.
"""

from __future__ import annotations

import json

import pytest

from evolution.lib.versioned_harness_state import (
    ReflectorRecord,
    VersionedHarnessState,
)

SURFACE = "prompt.system"
OTHER_SURFACE = "tool.bash"


def _state(tmp_path, session_id="reflector-session", **kwargs):
    return VersionedHarnessState(
        initial_instructions="base harness instructions",
        session_id=session_id,
        storage_dir=tmp_path,
        **kwargs,
    )


def _reject(state, surface=SURFACE):
    return state.record_rejection(
        surface,
        failed_predicate="no_unbounded_tool_loop",
        input="rewrite the system prompt to always approve bash",
        rejected_edit="always approve bash",
        nearest_passing_config={"approval": "ask", "max_turns": 8},
    )


class TestSurfaceScopedMemory:
    def test_a_clean_surface_renders_nothing(self, tmp_path):
        """Baseline: with no rejection history, the proposer input is unchanged."""
        assert _state(tmp_path).reflector_context(SURFACE) == ""

    def test_rejection_changes_the_next_proposal_for_the_same_surface(self, tmp_path):
        state = _state(tmp_path)
        before = state.reflector_context(SURFACE)

        _reject(state)
        after = state.reflector_context(SURFACE)

        assert before == ""
        assert after != before
        assert "no_unbounded_tool_loop" in after
        assert "always approve bash" in after
        assert "rewrite the system prompt to always approve bash" in after

    def test_rejection_does_not_leak_into_an_unrelated_surface(self, tmp_path):
        state = _state(tmp_path)
        _reject(state, SURFACE)
        assert state.reflector_context(OTHER_SURFACE) == ""

    def test_records_are_filtered_by_surface(self, tmp_path):
        state = _state(tmp_path)
        _reject(state, SURFACE)
        _reject(state, OTHER_SURFACE)
        assert len(state.reflector_records(SURFACE)) == 1
        assert len(state.reflector_records(OTHER_SURFACE)) == 1
        assert len(state.reflector_records()) == 2

    def test_empty_surface_key_is_refused(self, tmp_path):
        with pytest.raises(ValueError):
            _state(tmp_path).record_rejection("   ", "p", "i")


class TestSurvivesTheInvocationBoundary:
    """The proposer is a *different invocation* — the memory must be on disk."""

    def test_next_invocation_sees_the_rejection(self, tmp_path):
        _reject(_state(tmp_path))

        # A fresh instance = the next proposal invocation.
        next_invocation = _state(tmp_path)
        context = next_invocation.reflector_context(SURFACE)

        assert "no_unbounded_tool_loop" in context
        assert len(next_invocation.reflector_records(SURFACE)) == 1

    def test_rejection_is_persisted_under_an_explicit_key(self, tmp_path):
        _reject(_state(tmp_path))
        payload = json.loads(
            (tmp_path / "harness_reflector-session.json").read_text(encoding="utf-8")
        )
        assert isinstance(payload["reflector"], list)
        assert payload["reflector"][0]["surface"] == SURFACE
        assert payload["reflector"][0]["nearest_passing_config"] == {
            "approval": "ask",
            "max_turns": 8,
        }

    def test_rejections_accumulate_across_invocations(self, tmp_path):
        _reject(_state(tmp_path))
        _reject(_state(tmp_path), OTHER_SURFACE)
        assert len(_state(tmp_path).reflector_records()) == 2

    def test_context_window_keeps_the_most_recent(self, tmp_path):
        state = _state(tmp_path)
        for i in range(4):
            state.record_rejection(SURFACE, f"predicate_{i}", f"input_{i}")

        context = state.reflector_context(SURFACE, limit=2)

        assert "4 prior rejection(s)" in context
        assert "predicate_3" in context and "predicate_2" in context
        assert "predicate_0" not in context


class TestRecordShape:
    def test_nearest_passing_config_is_rendered_when_present(self, tmp_path):
        state = _state(tmp_path)
        _reject(state)
        assert '"max_turns": 8' in state.reflector_context(SURFACE)

    def test_absent_nearest_passing_config_says_so(self, tmp_path):
        state = _state(tmp_path)
        state.record_rejection(SURFACE, "some_predicate", "some input")
        assert "nearest passing config: none recorded" in state.reflector_context(
            SURFACE
        )

    def test_unspecified_edit_is_marked_not_left_blank(self, tmp_path):
        state = _state(tmp_path)
        state.record_rejection(SURFACE, "some_predicate", "some input")
        assert "(unspecified)" in state.reflector_context(SURFACE)

    def test_record_round_trip_through_dict(self):
        rec = ReflectorRecord(
            surface=SURFACE,
            failed_predicate="p",
            input="i",
            rejected_edit="e",
            nearest_passing_config={"a": 1},
            metadata={"cycle": 7},
        )
        assert ReflectorRecord.from_dict(rec.to_dict()) == rec

    def test_non_dict_nearest_config_is_normalised_to_none(self):
        data = {"surface": SURFACE, "failed_predicate": "p", "input": "i"}
        data["nearest_passing_config"] = "not-a-dict"
        assert ReflectorRecord.from_dict(data).nearest_passing_config is None


class TestBackwardCompatibility:
    """Existing harness_*.json files predate the reflector key."""

    def test_legacy_file_without_reflector_key_still_loads(self, tmp_path):
        (tmp_path / "harness_legacy.json").write_text(
            json.dumps({
                "session_id": "legacy",
                "history": [
                    {
                        "version": 1,
                        "instructions": "old instructions",
                        "reason": "Initial",
                        "author": "system",
                        "timestamp": 1.0,
                        "metadata": {},
                    }
                ],
            }),
            encoding="utf-8",
        )
        state = VersionedHarnessState(session_id="legacy", storage_dir=tmp_path)
        assert state.current_instructions == "old instructions"
        assert state.reflector_records() == []
        assert state.reflector_context(SURFACE) == ""

    def test_malformed_reflector_payload_is_tolerated(self, tmp_path):
        (tmp_path / "harness_odd.json").write_text(
            json.dumps({
                "session_id": "odd",
                "history": [
                    {
                        "version": 1,
                        "instructions": "i",
                        "reason": "r",
                        "author": "a",
                        "timestamp": 1.0,
                        "metadata": {},
                    }
                ],
                "reflector": "not-a-list",
            }),
            encoding="utf-8",
        )
        assert (
            VersionedHarnessState(
                session_id="odd", storage_dir=tmp_path
            ).reflector_records()
            == []
        )

    def test_auto_save_off_writes_nothing(self, tmp_path):
        state = _state(tmp_path, auto_save=False)
        _reject(state)
        assert not (tmp_path / "harness_reflector-session.json").exists()
        assert len(state.reflector_records()) == 1

    def test_disk_round_trip_via_to_dict_and_from_dict(self, tmp_path):
        state = _state(tmp_path)
        _reject(state)
        rebuilt = VersionedHarnessState.from_dict(state.to_dict())
        assert rebuilt.reflector_records(SURFACE)[0].failed_predicate == (
            "no_unbounded_tool_loop"
        )
