"""Evolution pipeline skills stay out of the chat catalog (council 2026-08-31)."""

from agent.evolution_skills import (
    evolution_skills_visible_in_catalog,
    is_evolution_pipeline_skill,
)
from agent.skill_commands import scan_skill_commands


def test_name_prefix_and_category():
    assert is_evolution_pipeline_skill("evolution-research")
    assert is_evolution_pipeline_skill("evolution/issues")
    assert is_evolution_pipeline_skill("other", {"category": "evolution"})
    assert not is_evolution_pipeline_skill("memory-audit")
    assert not is_evolution_pipeline_skill("github")


def test_catalog_hidden_in_chat(monkeypatch):
    monkeypatch.delenv("HERMES_EVOLUTION_CATALOG", raising=False)
    monkeypatch.delenv("HERMES_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    assert evolution_skills_visible_in_catalog() is False
    assert evolution_skills_visible_in_catalog("cli") is False


def test_catalog_visible_in_cron(monkeypatch):
    monkeypatch.delenv("HERMES_EVOLUTION_CATALOG", raising=False)
    assert evolution_skills_visible_in_catalog("cron") is True
    monkeypatch.setenv("HERMES_EVOLUTION_CATALOG", "1")
    assert evolution_skills_visible_in_catalog("cli") is True


def test_scan_skill_commands_omits_evolution_in_chat(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_EVOLUTION_CATALOG", raising=False)
    monkeypatch.delenv("HERMES_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    skills = tmp_path / "skills" / "evolution-research"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text(
        "---\nname: evolution-research\ndescription: Research other agents.\n---\n# x\n",
        encoding="utf-8",
    )
    (tmp_path / "skills" / "hello").mkdir()
    (tmp_path / "skills" / "hello" / "SKILL.md").write_text(
        "---\nname: hello-world\ndescription: Say hello.\n---\n# x\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    import agent.skill_commands as sc

    sc._skill_commands = {}
    sc._skill_commands_platform = None
    sc._skill_commands_home = None
    cmds = scan_skill_commands()
    names = {v.get("name") for v in cmds.values()}
    assert "hello-world" in names
    assert "evolution-research" not in names
    assert "/evolution-research" not in cmds
