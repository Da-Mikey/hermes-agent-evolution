"""Regression tests for the evolution Hydra orchestrator prompt.

The Hydra prompt is sent on every cron tick, so its size directly affects
API latency and timeout risk. These tests assert invariants rather than
freezing exact wording.
"""

from pathlib import Path

import yaml

HYDRA_YAML = Path(__file__).resolve().parents[2] / "cron" / "evolution" / "hydra.yaml"


def _load_prompt() -> str:
    data = yaml.safe_load(HYDRA_YAML.read_text(encoding="utf-8"))
    return data["prompt"]


class TestHydraPromptInvariants:
    def test_yaml_is_valid_and_has_prompt(self):
        prompt = _load_prompt()
        assert isinstance(prompt, str) and len(prompt) > 0

    def test_prompt_stays_small(self):
        """The prompt must remain compact enough for fast/cheap flash models."""
        prompt = _load_prompt()
        assert len(prompt) <= 2500, (
            f"Hydra prompt is {len(prompt)} chars; keep it under 2500 to avoid "
            "deepseek-v4-flash timeouts."
        )

    def test_prompt_requires_delegate_task_and_file_toolsets(self):
        prompt_lower = _load_prompt().lower()
        assert "delegate_task" in prompt_lower
        assert "toolsets" in prompt_lower

    def test_prompt_lists_all_evolution_stages(self):
        prompt_lower = _load_prompt().lower()
        for stage in (
            "research",
            "issues",
            "introspection",
            "analysis",
            "implementation",
            "integration",
            "upstream-sync",
        ):
            assert stage in prompt_lower, f"Hydra prompt missing stage: {stage}"

    def test_prompt_keeps_core_safety_rules(self):
        prompt_lower = _load_prompt().lower()
        assert "never dispatch the same stage twice" in prompt_lower
        assert "blocked" in prompt_lower and "github auth" in prompt_lower

    def test_yaml_toolsets_carry_terminal_down_to_children(self):
        """The invariant is a SUPERSET, not an exclusion.

        Subagent toolsets are intersected with the parent's
        (``tools/delegate_tool_toolsets.py``), so a Hydra without ``terminal``
        silently strips it from every child — which left the issues /
        implementation / integration stages unable to reach ``gh`` or ``git``
        for days. The Hydra carries no ``skills:`` and never runs a stage script
        itself; it still has to hand the toolsets down.

        This assertion used to demand the exact opposite ("``terminal`` must not
        be present"), contradicting the config's own documented requirement, so
        it red-lined every PR once the config was corrected.
        """
        data = yaml.safe_load(HYDRA_YAML.read_text(encoding="utf-8"))
        toolsets = [str(t).lower() for t in (data.get("toolsets") or [])]
        for required in ("file", "delegation", "terminal"):
            assert required in toolsets, (
                f"Hydra must pass `{required}` down to its subagents: child toolsets "
                f"are intersected with the parent's, so dropping it here removes it "
                f"from every stage. toolsets={toolsets}"
            )

    def test_yaml_declares_no_skills(self):
        """Being a pure delegator means no ``skills:`` — each stage owns its own."""
        data = yaml.safe_load(HYDRA_YAML.read_text(encoding="utf-8"))
        assert not data.get("skills"), (
            "The Hydra is a pure delegator; stage skills belong to the children."
        )
