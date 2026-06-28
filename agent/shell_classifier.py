"""Shell command classification for the ``classify_all_shell`` feature.

When ``terminal.classify_all_shell: true`` is set in config.yaml, every
terminal command passes through a lightweight pre-execution classifier
that labels it ``safe``, ``risky``, or ``dangerous``.

Architecture
------------
* Two-tier: heuristic baseline (fast, always works) + optional LLM callback
  for more nuanced decisions. The caller (tool_executor.py) injects the LLM
  callback when configured.
* A per-session LRU cache avoids re-classifying identical commands.
* The result hooks into ``tool_executor.py`` alongside the existing
  ``_is_destructive_command`` checkpoint.

The module is stateless (beyond the cache) and does not import the agent
directly, making it trivially testable.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Classification result types
# ---------------------------------------------------------------------------


class ShellRisk(Enum):
    SAFE = "safe"
    RISKY = "risky"
    DANGEROUS = "dangerous"


@dataclass(frozen=True)
class ShellClassification:
    """Result of classifying a shell command."""

    risk: ShellRisk
    rationale: str

    def to_block_message(self) -> str | None:
        """Return a human-readable explanation if the command should be blocked."""
        if self.risk is ShellRisk.SAFE:
            return None
        if self.risk is ShellRisk.RISKY:
            return f"Risky command ({self.rationale}). Proceed only if you are sure."
        return (
            f"BLOCKED: dangerous command ({self.rationale}). "
            f"This command requires explicit approval to run."
        )


# ---------------------------------------------------------------------------
# LRU Cache
# ---------------------------------------------------------------------------


@dataclass
class ClassificationCache:
    """Per-session LRU cache of command classifications."""

    maxsize: int = 256
    _store: dict[str, ShellClassification] = field(default_factory=dict)

    def _key(self, command: str) -> str:
        return hashlib.sha256(command.encode("utf-8")).hexdigest()

    def get(self, command: str) -> ShellClassification | None:
        return self._store.get(self._key(command))

    def put(self, command: str, classification: ShellClassification) -> None:
        key = self._key(command)
        if len(self._store) >= self.maxsize:
            try:
                self._store.pop(next(iter(self._store)))
            except StopIteration:
                pass
        self._store[key] = classification

    def clear(self) -> None:
        self._store.clear()


# Singleton -- one cache per agent process lifespan.
_classification_cache = ClassificationCache()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_command(
    command: str,
    llm_classifier: Callable[[str], Optional["ShellClassification"]] | None = None,
    cache: ClassificationCache | None = None,
) -> ShellClassification:
    """Classify a shell command as ``safe``, ``risky``, or ``dangerous``.

    Parameters
    ----------
    command : str
        The shell command to classify.
    llm_classifier : Callable | None
        An optional callback that takes the raw command string and returns
        a ``ShellClassification`` (or ``None`` to fall back to heuristic).
        The caller (e.g. tool_executor.py) provides this from the agent's
        LLM infrastructure when ``classify_all_shell_model`` is configured.
    cache : ClassificationCache | None
        An optional cache instance. Defaults to the module-global singleton.

    Returns
    -------
    ShellClassification
    """
    cache = cache or _classification_cache

    # Check cache first.
    cached = cache.get(command)
    if cached is not None:
        logger.debug("Shell classifier cache hit for command: %.80s", command)
        return cached

    # LLM-based classification (if a callback was provided).
    if llm_classifier is not None:
        try:
            result = llm_classifier(command)
            if result is not None:
                cache.put(command, result)
                return result
        except Exception as exc:
            logger.warning("LLM classifier callback failed: %s", exc)

    # Heuristic fallback.
    result = _heuristic_fallback(command)
    cache.put(command, result)
    return result


# ---------------------------------------------------------------------------
# LLM classifier prompt (for use by the caller when building the callback)
# ---------------------------------------------------------------------------

LLM_CLASSIFIER_SYSTEM_PROMPT = (
    "You are a shell command safety classifier.\n"
    "Classify the given shell command and return ONLY a JSON object with two keys:\n"
    '- "risk": one of "safe", "risky", or "dangerous"\n'
    '- "rationale": a brief 1-sentence explanation\n'
    "\n"
    "Classification rules:\n"
    '- "safe": read-only, informational, or benign commands\n'
    "  (ls, cat, grep, find, cd, pwd, echo, git status/log/diff, pip list, etc.)\n"
    '- "risky": commands that could modify state but are common development\n'
    "  operations (git add/commit/push/pull, pip install, npm install, mkdir,\n"
    "  cp/mv inside project, docker build, make, etc.)\n"
    '- "dangerous": commands that delete data, overwrite critical files,\n'
    "  format disks, run unsigned code from the internet, escalate privileges,\n"
    "  or access/modify system configuration\n"
    "  (rm -rf, dd, format, :(){ :|:& };:, curl | bash, chmod -R 777 /,\n"
    "  sudo rm, DROP TABLE, > /dev/sda, etc.)\n"
    "\n"
    'Be conservative -- classify ambiguous commands as "risky" rather than\n'
    '"dangerous". Commands from package managers (pip, npm, apt, brew) are\n'
    '"risky", not "dangerous", unless they include destructive flags.\n'
    "\n"
    "Return ONLY the JSON. No other text."
)

LLM_CLASSIFIER_USER_TEMPLATE = "Classify this shell command:\n\n```\n{command}\n```"


# ---------------------------------------------------------------------------
# Heuristic fallback
# ---------------------------------------------------------------------------


def _heuristic_fallback(command: str) -> ShellClassification:
    """Heuristic fallback when no LLM is available."""
    from agent.tool_dispatch_helpers import _is_destructive_command

    if not command or not command.strip():
        return ShellClassification(ShellRisk.SAFE, "empty command")

    if _is_destructive_command(command):
        return ShellClassification(
            ShellRisk.DANGEROUS, "matches destructive pattern heuristic"
        )

    cmd_lower = command.strip().lower()

    # Pipe-to-shell pattern.
    if (
        "curl" in cmd_lower
        and "|" in cmd_lower
        and ("bash" in cmd_lower or "sh " in cmd_lower)
    ):
        return ShellClassification(
            ShellRisk.DANGEROUS, "pipe from curl to shell interpreter"
        )

    # Direct block device write.
    if "dd " in cmd_lower and ("of=" in cmd_lower or "if=" in cmd_lower):
        return ShellClassification(ShellRisk.DANGEROUS, "dd block device operation")

    # SQL destructive operations.
    if any(kw in cmd_lower for kw in ["drop table", "drop database", "truncate table"]):
        return ShellClassification(ShellRisk.DANGEROUS, "destructive SQL operation")

    # Fork bomb / resource exhaustion.
    if ":(){" in command or ":|:" in command:
        return ShellClassification(ShellRisk.DANGEROUS, "fork bomb pattern detected")

    # chmod -R 777 on root.
    if (
        "chmod" in cmd_lower
        and ("777" in cmd_lower or "0777" in cmd_lower)
        and "/" in cmd_lower
    ):
        return ShellClassification(
            ShellRisk.DANGEROUS, "overly permissive chmod on root path"
        )

    # Dangerous git operations.
    if "git push --force" in cmd_lower:
        return ShellClassification(
            ShellRisk.RISKY, "force push overwrites remote history"
        )

    # Package manager operations.
    if any(
        kw in cmd_lower
        for kw in [
            "pip install",
            "npm install",
            "apt install",
            "brew install",
            "cargo install",
            "go install",
            "gem install",
        ]
    ):
        return ShellClassification(
            ShellRisk.RISKY, "package manager install modifies system state"
        )

    # Default to safe.
    return ShellClassification(ShellRisk.SAFE, "no risk pattern detected")
