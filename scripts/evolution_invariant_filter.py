#!/usr/bin/env python3
"""Immutable invariant filter for forge rewrites (issue #68, slices 1+2).

Screens every skill/prompt/harness rewrite against a versioned set of
Cerberus/household constraints BEFORE it reaches the judge. A rewrite that
violates any hard invariant is short-circuited at zero fitness — it never
reaches evaluation or application. This is the "cannot violate" floor that
complements the existing verifier-gated edits (SIA) and the prompt-injection
scan (#1808).

Slices shipped here:

  * **Slice 1** — the constraint set lives as DATA in
    ``evolution_invariant_rules.json`` (a versioned, human-readable file), so
    the rules are reviewable, diffable, and extensible without touching code.
  * **Slice 2** — this deterministic filter loads that data and screens a
    rewrite dict against it. Wired into ``evolution_harness_gate.run_gate`` and
    ``run_cron_pass`` so a violating harness proposal is rejected with
    ``zero_fitness`` before the sandbox/regression gate ever runs.

The filter is deliberately CONSERVATIVE: it catches only unambiguous
violations (low false-positive floor), and it checks only top-level scalar
fields of a rewrite dict. It is a deterministic floor, not a semantic judge —
the LLM judge still does the open-ended evaluation; this module just vetoes
the candidates that must never get that far.

Design mirrors the other ``scripts/evolution_*.py`` helpers: pure, typed,
import-safe functions + a thin CLI. No LLM, no network, no clock, no IO beyond
reading the rules file.

CLI:

    evolution_invariant_filter.py proposal.json
    cat proposal.json | evolution_invariant_filter.py

Prints one JSON object ``{"ok", "violations", ...}`` and exits 0 when the
rewrite passes every invariant, 1 when it violates one — so a shell caller can
short-circuit on ``$?`` before invoking the judge.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_RULES_FILENAME = "evolution_invariant_rules.json"

# Check types the rules file may declare. Anything outside this vocabulary is
# ignored (never a crash) so a malformed/older rules file degrades safely.
_CHECKS = ("human_gating", "forbidden_paths", "forbidden_patterns")

# Default text fields scanned by ``forbidden_patterns`` when a rule does not
# declare its own ``text_fields``.
_DEFAULT_TEXT_FIELDS = (
    "delta",
    "text",
    "rationale",
    "title",
    "body",
    "content",
    "summary",
)

# Default path-bearing fields scanned by ``forbidden_paths``.
_DEFAULT_PATH_FIELDS = ("path", "file", "target", "target_file", "files")


def _rules_path() -> Path:
    """Locate the rules data file next to this module."""
    return Path(__file__).resolve().parent / _RULES_FILENAME


def load_invariant_rules(path: Optional[Path] = None) -> Dict[str, Any]:
    """Load + validate the versioned invariant rule set from disk.

    Returns ``{"version": int, "rules": [...]}``. Raises ``ValueError`` on a
    missing/invalid file, so a caller can fail closed (a missing rules file is
    an operator error, not a silent pass).
    """
    p = path or _rules_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read invariant rules {p}: {exc}") from exc
    except ValueError as exc:
        raise ValueError(f"invariant rules {p} are not valid JSON: {exc}") from exc
    version = data.get("version")
    if not isinstance(version, int) or version < 1:
        raise ValueError(f"invariant rules {p} missing a positive integer 'version'")
    rules = data.get("rules")
    if not isinstance(rules, list) or not rules:
        raise ValueError(f"invariant rules {p} has no 'rules' list")
    return {"version": version, "rules": rules}


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return str(value)


def _walk_text_fields(obj: Any, fields: Tuple[str, ...]) -> Dict[str, str]:
    """Collect the named scalar text fields (top-level) from a dict."""
    if not isinstance(obj, dict):
        return {}
    return {f: _as_text(obj[f]) for f in fields if f in obj}


def _walk_paths(obj: Any, fields: Tuple[str, ...]) -> List[str]:
    """Collect path-bearing fields (str or list-of-str) from a dict."""
    out: List[str] = []
    if not isinstance(obj, dict):
        return out
    for f in fields:
        if f not in obj:
            continue
        value = obj[f]
        if isinstance(value, str):
            out.append(value)
        elif isinstance(value, list):
            out.extend(_as_text(x) for x in value)
    return out


def _check_human_gating(
    proposal: Dict[str, Any], rule: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """Flag an explicit weakening of human gating. Absence is NOT a violation —
    only a truthy ``auto_apply`` or an explicit ``requires_human_review=False``
    is."""
    violations: List[Dict[str, Any]] = []
    if proposal.get("auto_apply"):
        violations.append({"rule": rule["id"], "reason": "auto_apply is truthy"})
    if (
        "requires_human_review" in proposal
        and proposal.get("requires_human_review") is False
    ):
        violations.append({
            "rule": rule["id"],
            "reason": "requires_human_review is explicitly falsy",
        })
    return violations


def _check_forbidden_paths(
    proposal: Dict[str, Any], rule: Dict[str, Any]
) -> List[Dict[str, Any]]:
    fields = tuple(rule.get("path_fields") or _DEFAULT_PATH_FIELDS)
    fragments = [str(f).lower() for f in rule.get("forbidden_fragments", [])]
    violations: List[Dict[str, Any]] = []
    for p in _walk_paths(proposal, fields):
        pl = p.lower()
        for frag in fragments:
            if frag and frag in pl:
                violations.append({
                    "rule": rule["id"],
                    "reason": f"path {p!r} hits forbidden fragment {frag!r}",
                })
                break
    return violations


def _check_forbidden_patterns(
    proposal: Dict[str, Any], rule: Dict[str, Any]
) -> List[Dict[str, Any]]:
    fields = tuple(rule.get("text_fields") or _DEFAULT_TEXT_FIELDS)
    patterns = [re.compile(str(p), re.IGNORECASE) for p in rule.get("patterns", [])]
    violations: List[Dict[str, Any]] = []
    for field, text in _walk_text_fields(proposal, fields).items():
        for pat in patterns:
            if pat.search(text):
                violations.append({
                    "rule": rule["id"],
                    "reason": (
                        f"field {field!r} matches forbidden pattern {pat.pattern!r}"
                    ),
                })
                break
    return violations


def check_proposal(
    proposal: Any, rules: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Screen ONE rewrite against the invariant rules.

    Returns ``{"ok": bool, "violations": [...]}`` where each violation is
    ``{"rule", "reason"}``. A non-dict proposal passes (nothing to screen) — the
    gate's own shape checks handle malformed input. If ``rules`` is omitted it
    is loaded from disk; a missing/invalid rules file fails CLOSED (a violation,
    not a pass).
    """
    if rules is None:
        try:
            rules = load_invariant_rules()
        except ValueError as exc:
            return {
                "ok": False,
                "violations": [{"rule": "rules-unavailable", "reason": str(exc)}],
            }
    if not isinstance(proposal, dict):
        return {"ok": True, "violations": []}

    violations: List[Dict[str, Any]] = []
    for rule in rules.get("rules", []):
        check = rule.get("check")
        if check == "human_gating":
            violations.extend(_check_human_gating(proposal, rule))
        elif check == "forbidden_paths":
            violations.extend(_check_forbidden_paths(proposal, rule))
        elif check == "forbidden_patterns":
            violations.extend(_check_forbidden_patterns(proposal, rule))
        # unknown check type -> ignored (never crash)
    return {"ok": not violations, "violations": violations}


def filter_proposals(
    proposals: List[Any], rules: Optional[Dict[str, Any]] = None
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    """Split proposals into ``(passing, rejected)``.

    Each ``rejected`` entry is ``{"proposal": <orig>, "violations": [...]}``.
    """
    passing: List[Any] = []
    rejected: List[Dict[str, Any]] = []
    for p in proposals:
        result = check_proposal(p, rules=rules)
        if result["ok"]:
            passing.append(p)
        else:
            rejected.append({"proposal": p, "violations": result["violations"]})
    return passing, rejected


def _load_proposal(path: Optional[str]) -> Tuple[Optional[Any], Optional[str]]:
    try:
        raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    except OSError as exc:
        return None, f"cannot read input: {exc}"
    try:
        data = json.loads(raw)
    except ValueError as exc:
        return None, f"input is not valid JSON: {exc}"
    return data, None


def main(argv: List[str]) -> int:
    positional = [a for a in argv[1:] if not a.startswith("-")]
    path = positional[0] if positional else None
    proposal, err = _load_proposal(path)
    if err:
        print(f"[evolution-invariant-filter] {err}", file=sys.stderr)
        return 2
    result = check_proposal(proposal)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
