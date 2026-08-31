"""Evolution-pipeline skills: catalog visibility for chat vs cron.

Council 2026-08-31: the nine ``evolution-*`` skills must not appear in the
default chat catalog (slash completions, skills_list, system-prompt index).
Cron sessions still load them by name via the job's ``skills:`` list.

Explicit ``skill_view(name)`` is not filtered here — that is how cron (and a
user who types the name) loads the skill.
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional


def is_evolution_pipeline_skill(
    name: str,
    frontmatter: Optional[Mapping[str, Any]] = None,
) -> bool:
    """True for the self-evolution pipeline skills, not for unrelated names."""
    slug = (name or "").strip().lower().replace("_", "-").replace("/", "-")
    if slug.startswith("evolution-"):
        return True
    fm = frontmatter or {}
    if str(fm.get("category") or "").strip().lower() == "evolution":
        return True
    meta = fm.get("metadata") if isinstance(fm.get("metadata"), Mapping) else {}
    hermes = meta.get("hermes") if isinstance(meta, Mapping) else {}
    if isinstance(hermes, Mapping) and hermes.get("evolution_only"):
        return True
    return False


def evolution_skills_visible_in_catalog(session_platform: Optional[str] = None) -> bool:
    """Chat catalogs hide evolution skills; cron (and an explicit opt-in) show them."""
    if os.environ.get("HERMES_EVOLUTION_CATALOG", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }:
        return True
    plat = (
        session_platform
        or os.environ.get("HERMES_PLATFORM")
        or os.environ.get("HERMES_SESSION_PLATFORM")
        or ""
    ).strip().lower()
    return plat == "cron"
