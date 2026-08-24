"""Adaptive memory injection — calibrate memory "dosage" to model capability (#75).

Slices 1+2 of the #75 decomposition: per-tier injection profiles as CONFIG
DATA plus the retrieval policy that selects a profile by model tier. The
policy only ever SHORTENS the injected context — it never rewrites a byte of
what a provider returned, and it never touches the static guidance prefix
(``MEMORY_GUIDANCE`` in ``agent/prompt_builder.py``). That byte-stability is
the HARD gate for this feature (per-conversation prompt caching is sacred):
the dosage path removes provider blocks from the END of the merged prefetch
text and truncates the tail at the profile's char budget, so the retained
head is byte-identical to the undosed join. See
``tests/agent/test_memory_dosage.py::TestByteStabilityHardGate``.

Config (``agent.memory_injection`` in ``config.yaml`` /
``hermes_cli.config_defaults``), default OFF until calibration shows cost
savings without accuracy loss:

.. code-block:: yaml

   agent:
     memory_injection:
       enabled: false
       profiles:
         full:    {max_items: 8, max_chars: 6000}
         curated: {max_items: 4, max_chars: 3000}
         minimal: {max_items: 1, max_chars: 1000}
       tier_patterns:
         - [frontier, ["(?i)claude-opus", "(?i)gpt-4o", "(?i)gpt-5", "(?i)o[1-4]"]]
         - [compact,  ["(?i)flash", "(?i)mini", "(?i)nano"]]
       default_profile: curated

Tier semantics: *frontier* models get the FULL dosage (they can absorb the
context), *compact* models get MINIMAL (small windows, and weaker models are
distracted by excess memory), everything else gets CURATED. The wiring lives
in ``agent/memory_manager.py`` (``configure_dosage`` +
``prefetch_all(..., model_id=...)``) and ``agent/turn_context.py``.

Pure functions + explicit config boundary; import-safe and unit-testable.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# Per-tier injection profiles (config data). ``max_items`` = how many
# provider blocks may be injected; ``max_chars`` = total char budget for the
# merged injection (the tail is truncated at this boundary).
DEFAULT_PROFILES: Dict[str, Dict[str, int]] = {
    "full": {"max_items": 8, "max_chars": 6000},
    "curated": {"max_items": 4, "max_chars": 3000},
    "minimal": {"max_items": 1, "max_chars": 1000},
}

# Ordered (tier, [regex patterns]) list — FIRST match wins. Regexes are
# matched against the model id (case-insensitive, partial match). The
# elements are tuples here (code) but plain lists when parsed from YAML.
DEFAULT_TIER_PATTERNS: List[Any] = [
    (
        "frontier",
        [
            r"(?i)claude-opus",
            r"(?i)gpt-4o",
            r"(?i)gpt-5",
            r"(?i)o[1-4](?:\b|-)",
            r"(?i)gemini-2\.5-pro",
            r"(?i)deepseek-reasoner",
            r"(?i)sonnet",
        ],
    ),
    (
        "compact",
        [
            r"(?i)flash",
            r"(?i)mini",
            r"(?i)nano",
            r"(?i)tiny",
            r"(?i)(?:^|[^0-9])(?:1|3|7|8|9)b",
            r"(?i)small",
        ],
    ),
]

DEFAULT_DEFAULT_PROFILE = "curated"

# Tier name -> injection profile name. ``resolve_profile`` classifies a model
# into a TIER (frontier / compact); ``profile_for`` maps that tier to a
# PROFILE (full / curated / minimal) via this table. Tiers with no entry fall
# through to the profile of the same name, then to ``default_profile``.
DEFAULT_TIER_PROFILES: Dict[str, str] = {
    "frontier": "full",
    "compact": "minimal",
}

_SEP = "\n\n"


def resolve_profile(
    model_id: Optional[str],
    tier_patterns: Optional[List[Any]] = None,
    default_profile: str = DEFAULT_DEFAULT_PROFILE,
) -> str:
    """Classify a model id into an injection profile name.

    First tier whose pattern matches the (lowercased) model id wins. An
    unknown or empty model id falls back to ``default_profile`` — the dosage
    policy must never crash or refuse on an unclassifiable model.
    """
    patterns = tier_patterns if tier_patterns is not None else DEFAULT_TIER_PATTERNS
    model = (model_id or "").lower()
    for tier, regexes in patterns:
        for regex in regexes or []:
            try:
                if re.search(str(regex), model):
                    return str(tier)
            except re.error:
                continue
    return default_profile


def load_dosage_config(cfg: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize an ``agent.memory_injection`` config section.

    Returns ``None`` when disabled or malformed (the dosage path is then a
    no-op — default-off is the calibrated-rollout stance of #75). A valid
    config is returned as ``{"enabled": True, "profiles": {...},
    "tier_patterns": [[tier, [regexes]], ...], "default_profile": str}``.
    """
    if not isinstance(cfg, dict) or not cfg.get("enabled"):
        return None
    profiles = cfg.get("profiles") or {}
    normalized_profiles: Dict[str, Dict[str, int]] = {}
    for name, spec in profiles.items():
        if not isinstance(spec, dict):
            continue
        try:
            normalized_profiles[str(name)] = {
                "max_items": max(0, int(spec.get("max_items") or 0)),
                "max_chars": max(0, int(spec.get("max_chars") or 0)),
            }
        except (TypeError, ValueError):
            continue
    if not normalized_profiles:
        return None
    tier_patterns = cfg.get("tier_patterns") or DEFAULT_TIER_PATTERNS
    if not isinstance(tier_patterns, list) or not tier_patterns:
        tier_patterns = DEFAULT_TIER_PATTERNS
    tier_profiles = cfg.get("tier_profiles") or DEFAULT_TIER_PROFILES
    if not isinstance(tier_profiles, dict) or not tier_profiles:
        tier_profiles = DEFAULT_TIER_PROFILES
    default_profile = str(cfg.get("default_profile") or DEFAULT_DEFAULT_PROFILE)
    return {
        "enabled": True,
        "profiles": normalized_profiles,
        "tier_patterns": tier_patterns,
        "tier_profiles": {str(k): str(v) for k, v in tier_profiles.items()},
        "default_profile": default_profile,
    }


def profile_for(
    model_id: Optional[str], config: Dict[str, Any]
) -> Optional[Dict[str, int]]:
    """The injection profile dict for ``model_id`` under ``config``."""
    if not config:
        return None
    profiles = config.get("profiles") or {}
    tier = resolve_profile(
        model_id,
        tier_patterns=config.get("tier_patterns"),
        default_profile=config.get("default_profile", DEFAULT_DEFAULT_PROFILE),
    )
    # Tier -> profile mapping: explicit tier_profiles table, falling through
    # to a profile sharing the tier's name, then to the default profile.
    tier_profiles = config.get("tier_profiles") or {}
    profile_name = tier_profiles.get(tier, tier)
    return (
        profiles.get(profile_name)
        or profiles.get(tier)
        or profiles.get(DEFAULT_DEFAULT_PROFILE)
    )


def apply_profile(profile: Dict[str, int], parts: List[str]) -> str:
    """Apply a dosage profile to merged prefetch blocks.

    BYTE-STABILITY CONTRACT (the hard gate): this only ever removes — keeps
    the first ``max_items`` non-empty blocks in order, joins with ``"\\n\\n"``
    and truncates the tail at ``max_chars``. The result is therefore a byte
    PREFIX of the undosed join: every retained byte is identical to the
    baseline, so the static guidance prefix (``MEMORY_GUIDANCE``) can never
    be altered by dosage. Empty blocks are dropped before counting items so
    providers that return blank text cannot waste an item slot.
    """
    blocks = [p for p in (parts or []) if p and p.strip()]
    if not blocks:
        return ""
    kept = blocks[: int(profile.get("max_items") or len(blocks))]
    joined = _SEP.join(kept)
    max_chars = int(profile.get("max_chars") or 0)
    if max_chars and len(joined) > max_chars:
        # Never truncate INSIDE the first block: the head must stay
        # byte-identical (the hard gate — see TestByteStabilityHardGate), so
        # the char budget only ever applies to the tail beyond block 1.
        return joined[: max(max_chars, len(kept[0]))]
    return joined
