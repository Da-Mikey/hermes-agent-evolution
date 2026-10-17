# -*- coding: utf-8 -*-
"""Verifier independence from the candidate's own rationale (#213).

Source: arXiv 2610.12822 — *Reward Hacking as a Transmissible Strategy in
Multi-Agent Coding Systems*. Its finding is that reward hacking is
**transmissible**: it survives candidate aggregation, biases task routing toward
reward-hacking candidates, and *intensifies along a chain* because the artifacts
accumulate "more persuasive justifications". The artifact carries the rationalisation,
not just the edit.

That is the shape of this project's own loop: a candidate (a skill document, a
proposal, a distilled skill) is described in its own words and those words are
fed to the evaluator that scores it. A candidate that learns "say what the rubric
likes" then scores well without behaving well — and its rationale travels with
it into the next promotion.

This module enforces the two mitigations the paper's own evaluation uses:

1. **Verifier independence** — :func:`build_verifier_bundle` composes the only
   input a verifier may score from: ``task``, ``edit``, ``evidence``. The
   candidate's self-narrative is not part of that contract, and
   :func:`strip_rationale_sections` / :func:`strip_rationale_fields` remove it
   from prose and mappings on the way in.
2. **Re-encoding check on promotion** — :class:`RejectedRationaleIndex`
   fingerprints the rationale of candidates that were already rejected;
   :meth:`RejectedRationaleIndex.best_match` flags a promoted candidate whose
   rationale merely re-states a rejected one, so it is reviewed instead of
   auto-promoted.

Scope note (honesty beats coverage): similarity is the stronger of content-word
set overlap and character 4-gram overlap. That catches verbatim re-encodings and
light rewording (word swaps, reordering, added qualifiers), which is what a chain
of "more persuasive justifications" actually produces. It does NOT catch a
determined paraphrase in fresh vocabulary — treat a low score as "no evidence of
re-encoding", never as proof of independence.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = [
    "RATIONAL_SECTION_KEYWORDS",
    "VERIFIER_INPUT_KEYS",
    "RationaleMatch",
    "RejectedRationaleIndex",
    "build_verifier_bundle",
    "contains_rationale_key",
    "is_rationale_heading",
    "rationale_fingerprint",
    "rationale_similarity",
    "strip_rationale_fields",
    "strip_rationale_sections",
]

#: The complete input contract for a verifier. Nothing else may be scored.
VERIFIER_INPUT_KEYS: Tuple[str, ...] = ("task", "edit", "evidence")

#: Headings whose body is persuasion rather than specification. Matched on a
#: normalized heading, either as a prefix ("Why this matters to our stack") or
#: as the whole heading ("Impact").
RATIONAL_SECTION_KEYWORDS: Tuple[str, ...] = (
    "rationale",
    "justification",
    "justify",
    "why this",
    "why now",
    "why it matters",
    "why we should",
    "value proposition",
    "motivation",
    "benefits",
    "expected impact",
    "evidence of impact",
    "impact assessment",
    "self-assessment",
    "why trust this",
)

#: Exact headings that are rationale when they appear alone (too generic to
#: match as a substring — a skill's "## Impact" section is not a self-narrative,
#: but a proposal's "## Impact" score line is).
_RATIONALE_EXACT_HEADINGS = frozenset({
    "impact", "priority", "score", "value", "why", "pitch",
})

#: Mapping keys that carry a proposer's self-narrative.
_RATIONALE_FIELD_KEYS = frozenset({
    "rationale", "justification", "justifications", "why", "why_now",
    "why_it_matters", "why_this_matters", "why_it_matters_now",
    "motivation", "value_proposition", "self_narrative", "self_justification",
    "justification_text", "proposer_rationale", "benefits", "pitch",
})

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_WORD_RE = re.compile(r"[a-z0-9]+")
_CHAR_N = 4
_MAX_FIELD_DEPTH = 6


# ── detection ─────────────────────────────────────────────────────────────

def _normalise_heading(heading: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (heading or "").strip().lower()).strip()


def is_rationale_heading(heading: str) -> bool:
    """True when *heading* introduces a rationale / justification section."""
    norm = _normalise_heading(heading)
    if not norm:
        return False
    if norm in _RATIONALE_EXACT_HEADINGS:
        return True
    return any(norm.startswith(kw) or kw in norm for kw in RATIONAL_SECTION_KEYWORDS)


def contains_rationale_key(obj: Any, _depth: int = 0) -> bool:
    """True when *obj* (any nesting of mappings/lists) carries a rationale key."""
    if _depth > _MAX_FIELD_DEPTH:
        return False
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            if str(key).strip().lower() in _RATIONALE_FIELD_KEYS:
                return True
            if contains_rationale_key(value, _depth + 1):
                return True
    elif isinstance(obj, (list, tuple, set)):
        return any(contains_rationale_key(item, _depth + 1) for item in obj)
    return False


# ── stripping ─────────────────────────────────────────────────────────────

def strip_rationale_sections(text: str) -> Tuple[str, List[str]]:
    """Remove rationale sections from markdown *text*.

    Returns ``(clean_text, removed_headings)``. A rationale heading is dropped
    together with its body, up to the next heading of the same or higher level;
    deeper headings inside it go with it (they belong to that section, and are
    not listed separately in *removed_headings*). Text with no rationale
    headings comes back unchanged byte-for-byte — including its trailing
    newline — so this is safe to apply to every candidate document.

    Only ATX headings (``## …``) are recognised, and headings inside fenced code
    blocks are not treated specially — a skill document that illustrates a
    "## Rationale" heading inside a fence would lose it.
    """
    raw = text or ""
    lines = raw.splitlines()
    if not lines:
        return raw, []
    kept: List[str] = []
    removed: List[str] = []
    skip_level = 0
    for line in lines:
        match = _HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            if skip_level and level <= skip_level:
                skip_level = 0
            if not skip_level and is_rationale_heading(match.group(2)):
                skip_level = level
                removed.append(match.group(2).strip())
                continue
        if skip_level:
            continue
        kept.append(line)
    clean = "\n".join(kept)
    if kept and raw.endswith("\n"):
        clean += "\n"
    return clean, removed


def strip_rationale_fields(obj: Any, _depth: int = 0) -> Any:
    """Return a copy of *obj* with every rationale field removed, recursively."""
    if _depth > _MAX_FIELD_DEPTH:
        return obj
    if isinstance(obj, Mapping):
        return {
            key: strip_rationale_fields(value, _depth + 1)
            for key, value in obj.items()
            if str(key).strip().lower() not in _RATIONALE_FIELD_KEYS
        }
    if isinstance(obj, list):
        return [strip_rationale_fields(item, _depth + 1) for item in obj]
    if isinstance(obj, tuple):
        return tuple(strip_rationale_fields(item, _depth + 1) for item in obj)
    if isinstance(obj, str):
        return strip_rationale_sections(obj)[0]
    return obj


def build_verifier_bundle(
    candidate: Mapping[str, Any],
    *,
    task: Optional[str] = None,
    edit: Any = None,
    evidence: Any = None,
) -> Dict[str, Any]:
    """Compose the ONLY bundle a verifier may score: ``task``/``edit``/``evidence``.

    Fields are taken from *candidate* unless passed explicitly, and every one of
    them is scrubbed of rationale text/fields, so a candidate cannot smuggle its
    self-justification in through ``evidence``. Raises ``ValueError`` when the
    bundle would be empty enough that scoring it means scoring nothing.
    """
    source_task = task if task is not None else candidate.get("task")
    source_edit = edit if edit is not None else candidate.get("edit")
    if source_edit is None:
        source_edit = candidate.get("patch", candidate.get("content"))
    source_evidence = evidence if evidence is not None else candidate.get("evidence")

    clean_task = strip_rationale_fields(str(source_task or ""))
    clean_edit = strip_rationale_fields(source_edit)
    clean_evidence = strip_rationale_fields(source_evidence)

    if not clean_task and not clean_edit and not clean_evidence:
        raise ValueError(
            "verifier bundle would carry no task, edit or evidence — nothing to score")

    bundle: Dict[str, Any] = dict(zip(VERIFIER_INPUT_KEYS,
                                      (clean_task, clean_edit, clean_evidence)))
    # The contract is closed: no proposer-authored narrative may ride along.
    if contains_rationale_key(bundle):
        raise ValueError("verifier bundle still carries a rationale field")
    return bundle


# ── rationale fingerprinting (re-encoding check) ──────────────────────────

def _tokens(text: str) -> frozenset:
    """Content words (>=3 chars) — the vocabulary a re-encoding reuses."""
    return frozenset(w for w in _WORD_RE.findall((text or "").lower()) if len(w) > 2)


def _char_grams(text: str) -> frozenset:
    """Character 4-grams — tolerant of word-order changes and word-form swaps."""
    norm = " ".join((text or "").lower().split())
    if not norm:
        return frozenset()
    if len(norm) < _CHAR_N:
        return frozenset({norm})
    return frozenset(norm[i:i + _CHAR_N] for i in range(len(norm) - _CHAR_N + 1))


def _jaccard(left: frozenset, right: frozenset) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def rationale_fingerprint(text: str) -> str:
    """Stable 16-hex fingerprint of *text*'s character 4-gram set ('' when empty)."""
    grams = _char_grams(text)
    if not grams:
        return ""
    payload = "\n".join(sorted(grams)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def rationale_similarity(left: str, right: str) -> float:
    """How much *left* re-states *right*, ``0.0``..``1.0``.

    The stronger of two signals: content-word set overlap (the same vocabulary in
    a different order) and character 4-gram overlap (the same wording with light
    edits). Either is enough to mark a candidate as re-encoding — see the module
    docstring for what this deliberately does NOT catch.
    """
    return max(_jaccard(_tokens(left), _tokens(right)),
               _jaccard(_char_grams(left), _char_grams(right)))


@dataclass(frozen=True)
class RationaleMatch:
    """A promoted rationale that resembles one already rejected."""

    candidate_id: str
    similarity: float
    threshold: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class RejectedRationaleIndex:
    """Fingerprinted rationales of candidates the project already turned down.

    Used before promotion: a candidate whose rationale is a near-match of a
    rejected one is *flagged for review*, never auto-promoted — the mitigation
    the paper evaluates, applied at our promotion point.
    """

    def __init__(self, threshold: float = 0.6, entries: Optional[Mapping[str, str]] = None):
        if not 0.0 < threshold <= 1.0:
            raise ValueError("threshold must be in (0.0, 1.0]")
        self.threshold = float(threshold)
        self._entries: Dict[str, str] = dict(entries or {})

    def __len__(self) -> int:
        return len(self._entries)

    def add(self, candidate_id: str, rationale: str) -> None:
        """Remember a rejected candidate's rationale (empty ones are ignored)."""
        text = (rationale or "").strip()
        if not candidate_id or not text:
            return
        self._entries[str(candidate_id)] = text

    def extend(self, items: Iterable[Tuple[str, str]]) -> None:
        for candidate_id, rationale in items:
            self.add(candidate_id, rationale)

    def best_match(self, rationale: str) -> Optional[RationaleMatch]:
        """Highest-similarity rejected rationale at/above the threshold, else ``None``."""
        text = (rationale or "").strip()
        if not text or not self._entries:
            return None
        best: Optional[RationaleMatch] = None
        for candidate_id, rejected in self._entries.items():
            score = rationale_similarity(text, rejected)
            if score >= self.threshold and (best is None or score > best.similarity):
                best = RationaleMatch(candidate_id, round(score, 4), self.threshold)
        return best

    def check(self, rationale: str) -> List[str]:
        """Short human-readable reason list for a promotion gate (empty = clear)."""
        match = self.best_match(rationale)
        if match is None:
            return []
        return [
            f"rationale re-encodes rejected candidate {match.candidate_id!r} "
            f"(similarity {match.similarity:.2f} >= {match.threshold:.2f}) — "
            "flagged for review, not auto-promoted"
        ]

    def to_dict(self) -> Dict[str, Any]:
        return {"threshold": self.threshold, "entries": dict(self._entries)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RejectedRationaleIndex":
        return cls(
            threshold=float(data.get("threshold", 0.6)),
            entries={str(k): str(v) for k, v in (data.get("entries") or {}).items()},
        )
