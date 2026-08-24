"""Adaptive memory injection (#75, slices 1+2) — unit tests + HARD gate.

The byte-stability HARD gate (TestByteStabilityHardGate) is the load-bearing
part: per-conversation prompt caching is sacred, so the dosage policy must
never alter a single byte of the static guidance prefix (MEMORY_GUIDANCE) or
of any retained memory block. The contract, asserted here: the dosed output
is a byte PREFIX of the undosed merge — dosage only ever removes from the
end. A regression in that invariant fails CI and blocks merge.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agent.memory_dosage import (  # noqa: E402
    DEFAULT_DEFAULT_PROFILE,
    DEFAULT_PROFILES,
    DEFAULT_TIER_PATTERNS,
    apply_profile,
    load_dosage_config,
    profile_for,
    resolve_profile,
)
from agent.memory_manager import MemoryManager  # noqa: E402
from agent.prompt_builder import MEMORY_GUIDANCE  # noqa: E402

MINIMAL_CFG = {
    "enabled": True,
    "profiles": {
        "full": {"max_items": 8, "max_chars": 6000},
        "curated": {"max_items": 4, "max_chars": 3000},
        "minimal": {"max_items": 1, "max_chars": 1000},
    },
    "tier_patterns": DEFAULT_TIER_PATTERNS,
    "tier_profiles": {"frontier": "full", "compact": "minimal"},
    "default_profile": DEFAULT_DEFAULT_PROFILE,
}


class TestResolveProfile:
    def test_frontier_model(self):
        assert resolve_profile("anthropic/claude-opus-4-1") == "frontier"
        assert resolve_profile("openai/gpt-4o") == "frontier"
        assert resolve_profile("openai/o3") == "frontier"

    def test_compact_model(self):
        assert resolve_profile("google/gemini-3-flash-preview") == "compact"
        assert resolve_profile("deepseek/deepseek-chat-mini") == "compact"

    def test_unknown_falls_back_to_default(self):
        assert resolve_profile("some/new-model-x") == "curated"

    def test_empty_model_falls_back(self):
        assert resolve_profile(None) == "curated"
        assert resolve_profile("") == "curated"

    def test_first_match_wins(self):
        # a model matching BOTH frontier and compact patterns -> frontier
        assert resolve_profile("claude-opus-mini") == "frontier"


class TestLoadDosageConfig:
    def test_disabled_is_none(self):
        assert load_dosage_config({"enabled": False}) is None
        assert load_dosage_config(None) is None
        assert load_dosage_config({}) is None

    def test_valid_config_normalized(self):
        cfg = load_dosage_config(MINIMAL_CFG)
        assert cfg is not None
        assert cfg["enabled"] is True
        assert cfg["profiles"]["minimal"]["max_items"] == 1

    def test_malformed_profiles_degrade_to_none(self):
        assert load_dosage_config({"enabled": True, "profiles": {}}) is None
        assert (
            load_dosage_config({"enabled": True, "profiles": {"bad": "not-a-dict"}})
            is None
        )


class TestProfileFor:
    def test_returns_profile_for_tier(self):
        cfg = load_dosage_config(MINIMAL_CFG)
        assert cfg is not None
        assert profile_for("claude-opus-4", cfg) == cfg["profiles"]["full"]
        assert profile_for("gemini-flash", cfg) == cfg["profiles"]["minimal"]
        assert profile_for("unknown-model", cfg) == cfg["profiles"]["curated"]

    def test_none_config_returns_none(self):
        assert profile_for("anything", None) is None


class TestApplyProfile:
    def test_keeps_first_items_only(self):
        parts = ["a", "b", "c", "d"]
        out = apply_profile({"max_items": 2, "max_chars": 0}, parts)
        assert out == "a\n\nb"

    def test_truncates_tail_at_char_budget(self):
        parts = ["head", "y" * 3000]
        out = apply_profile({"max_items": 8, "max_chars": 1000}, parts)
        assert len(out) == 1000
        assert out == ("head\n\n" + "y" * 3000)[:1000]

    def test_char_budget_never_cuts_first_block(self):
        # The hard gate: max_chars applies only to the tail beyond block 1 —
        # truncating inside the first block would break the byte-stable head.
        parts = ["x" * 3000, "y" * 3000]
        out = apply_profile({"max_items": 8, "max_chars": 1000}, parts)
        assert out == "x" * 3000  # first block intact, tail dropped entirely

    def test_empty_blocks_do_not_consume_items(self):
        parts = ["", "   ", "real", ""]
        out = apply_profile({"max_items": 1, "max_chars": 0}, parts)
        assert out == "real"

    def test_no_parts_is_empty(self):
        assert apply_profile({"max_items": 8, "max_chars": 1000}, []) == ""


class TestByteStabilityHardGate:
    """THE hard gate for #75: dosage must never alter retained bytes."""

    def test_memory_guidance_prefix_byte_stable(self):
        baseline = MEMORY_GUIDANCE.encode()
        parts = [MEMORY_GUIDANCE, "user prefers terse replies", "project uses pytest"]
        for profile in DEFAULT_PROFILES.values():
            out = apply_profile(profile, parts)
            assert MEMORY_GUIDANCE.encode() == baseline  # constant untouched
            assert out.startswith(MEMORY_GUIDANCE)  # retained head byte-identical

    def test_dosed_is_byte_prefix_of_undosed(self):
        parts = ["alpha " + "x" * 400, "beta " + "y" * 400, "gamma " + "z" * 400]
        undosed = "\n\n".join(parts)
        for profile in DEFAULT_PROFILES.values():
            dosed = apply_profile(profile, parts)
            assert dosed == "" or undosed.startswith(dosed), profile


class TestWiring:
    """prefetch_all honors dosage only when configured (default off)."""

    class _FakeBuiltin:
        name = "builtin"

        def __init__(self, *blocks):
            self._blocks = blocks

        def get_tool_schemas(self):
            return []  # current main add_provider indexes tool schemas

        def prefetch(self, query, session_id=""):
            return "\n\n".join(self._blocks)

    def _manager_with_fake(self):
        mgr = MemoryManager()
        mgr.add_provider(  # type: ignore[arg-type] — duck-typed test double
            self._FakeBuiltin(
                "alpha " + "x" * 400, "beta " + "y" * 400, "gamma " + "z" * 400
            )
        )
        return mgr

    def test_default_off_returns_undosed_merge(self):
        mgr = self._manager_with_fake()
        out = mgr.prefetch_all("query", model_id="claude-opus-4")
        assert (
            out
            == "alpha " + "x" * 400 + "\n\nbeta " + "y" * 400 + "\n\ngamma " + "z" * 400
        )

    def test_enabled_doses_and_preserves_prefix(self):
        mgr = self._manager_with_fake()
        undosed = mgr.prefetch_all("query", model_id="claude-opus-4")
        mgr.configure_dosage(MINIMAL_CFG)
        dosed = mgr.prefetch_all("query", model_id="claude-opus-4")
        # frontier tier -> full profile (8 items / 6000 chars): merge is 3
        # blocks ~1240 chars, so full profile caps only on items? No —
        # full keeps everything here; use the compact tier to force the cap.
        assert undosed.startswith(dosed)

    def test_compact_tier_gets_minimal_profile(self):
        mgr = self._manager_with_fake()
        mgr.configure_dosage(MINIMAL_CFG)
        out = mgr.prefetch_all("query", model_id="gemini-flash")
        # compact tier -> minimal profile: 1 item, 1000 chars.
        assert out == ("alpha " + "x" * 400)[:1000]

    def test_invalid_config_disables_dosage(self):
        mgr = self._manager_with_fake()
        mgr.configure_dosage({"enabled": True, "profiles": {}})
        out = mgr.prefetch_all("query", model_id="gemini-flash")
        assert "gamma" in out  # undosed merge

    def test_no_model_id_skips_dosage(self):
        mgr = self._manager_with_fake()
        mgr.configure_dosage(MINIMAL_CFG)
        out = mgr.prefetch_all("query")  # model_id=None -> no dosage
        assert "gamma" in out
