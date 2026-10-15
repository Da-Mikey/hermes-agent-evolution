# -*- coding: utf-8 -*-
"""Safety-review gate for self-modification merges + static analysis (#2575).

Misevolution guardrail (parent #2538; arXiv:2509.26354 "Your Agent May
Misevolve" — workflow pathway).

Adds a deterministic safety-review gate that runs **static analysis** on
generated/changed code before any self-modification merge is allowed:

1. Detects dangerous code patterns (``os.system``, unguarded ``subprocess``,
   ``eval``/``exec``, ``__import__``, unguarded file deletion, ``pickle.loads``
   on untrusted data, raw-IP network calls, etc.).
2. Returns violations that block the autonomous self-merge — the merge gate
   appends these to its existing policy pipeline.
3. Logs all changes with rollback to safe checkpoints (the merge gate's
   versioned snapshots + the existing ``SkillReuseGate.version`` mechanism).

#204 slice 1 — the rule set is DECLARED DATA, and it is checked.

The guard rules below are no longer an anonymous list of tuples: each rule
carries a stable ``rule_id``, so the rule set is a structure a deterministic
verifier can reason over and a reviewer can cite by name. ``find_rule_conflicts``
runs over that table and reports the two failure modes that are *decidable*
without a solver — a malformed rule (which fails open, reading as protection
while matching nothing) and a duplicate/shadowed rule (which cannot produce a
distinct verdict, so adding it changes behaviour only on paper).

``check_safety_gate`` runs that check first, so a bad rule edit is refused
fail-closed at merge time rather than text-approved. This slice deliberately
does NOT add an admission policy, does not tune any threshold, and does not
change which rules exist.

Pure, DI-testable, import-safe — all IO is explicit.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple

__all__ = [
    "SafetyGateVerdict",
    "GuardRule",
    "find_rule_conflicts",
    "scan_code_for_safety_violations",
    "check_safety_gate",
]


class GuardRule(NamedTuple):
    """One guard rule, expressed as data rather than an anonymous tuple (#204).

    ``rule_id`` is the stable handle a reviewer or a conflict report cites;
    ``severity`` is one of ``_VALID_SEVERITIES``.
    """

    rule_id: str
    pattern: re.Pattern
    description: str
    severity: str


_VALID_SEVERITIES = frozenset({"low", "medium", "high"})


# Dangerous-code patterns (static analysis on generated Python).
# Declared data: (rule_id, compiled_regex, description, severity).
_DANGEROUS_PATTERNS: List[GuardRule] = [
    GuardRule(
        "os-system",
        re.compile(r"\bos\.system\s*\("),
        "os.system() — shell injection risk; use subprocess with shell=False",
        "high",
    ),
    GuardRule(
        "eval",
        re.compile(r"\beval\s*\("),
        "eval() — arbitrary code execution; avoid in generated code",
        "high",
    ),
    GuardRule(
        "exec",
        re.compile(r"\bexec\s*\("),
        "exec() — arbitrary code execution; avoid in generated code",
        "high",
    ),
    GuardRule(
        "dunder-import",
        re.compile(r"\b__import__\s*\("),
        "__import__() — dynamic import; avoid in generated code",
        "medium",
    ),
    GuardRule(
        "subprocess-shell-true",
        re.compile(r"subprocess\.\w+\s*\([^)]*shell\s*=\s*True"),
        "subprocess with shell=True — shell injection risk",
        "high",
    ),
    GuardRule(
        "pickle-loads",
        re.compile(r"\bpickle\.loads?\s*\("),
        "pickle.loads() — deserialization of untrusted data is unsafe",
        "high",
    ),
    GuardRule(
        "unguarded-delete",
        re.compile(r"\bos\.remove\s*\(|\bos\.unlink\s*\(|\bshutil\.rmtree\s*\("),
        "unguarded file/directory deletion — destructive operation",
        "medium",
    ),
    GuardRule(
        "open-write-mode",
        re.compile(r"\bopen\s*\([^)]*['\"]w['\"]"),
        "file open in write mode — verify target path is not a system file",
        "low",
    ),
    GuardRule(
        "network-raw-ip",
        re.compile(
            r"\b(?:socket|urllib|requests|httpx|aiohttp)\.\w+\s*\([^)]*"
            r"(?:\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
        ),
        "network call to raw IP — potential exfiltration; use domain + verify",
        "medium",
    ),
    GuardRule(
        "obfuscated-exec",
        re.compile(r"\bbase64\.b64decode\s*\([^)]*\)\s*\.(?:decode|__import__)"),
        "base64-decoded payload used as code — obfuscated execution",
        "high",
    ),
    GuardRule(
        "process-exec",
        re.compile(r"(?:subprocess|os)\.(?:Popen|system|execvp?|execvpe?)\s*\("),
        "subprocess/os process execution — verify command is not constructed from user input",
        "medium",
    ),
]


def find_rule_conflicts(
    rules: Optional[Sequence[GuardRule]] = None,
) -> List[str]:
    """Deterministically check a guard-rule table for structural conflicts (#204).

    Returns one human-readable string per conflict, in table order, and ``[]``
    when the table is well-formed. Pure and total: the same table always yields
    the same list — no IO, no LLM, no heuristics, no solver.

    Two failure modes are decidable exactly, and both are ones a prose review
    step misses because they look correct:

    1. **Malformed rule** — a pattern that cannot express a usable rule (an empty
       or whitespace-only regex source), a blank ``rule_id``, or an unrecognised
       severity.
    2. **Silently neutralised rule** — two rules sharing a ``rule_id``, or the
       same pattern declared under two ids. The later rule can never produce a
       distinct verdict, so adding it changes nothing while appearing to have
       tightened the gate.

    Scope is deliberately *structural*: only these decidable properties are
    proven. A ``(rules=...)`` argument exists so the checker is testable against
    hand-built bad tables without mutating the shipped one.

    On mode 1: the check reads the regex *source* (empty, or whitespace-only),
    which is what a table can be checked for without a regex-language solver. It
    is not a "matches nothing" test, and the report no longer claims to be one —
    ``(?!)``, ``[^\\s\\S]`` and ``\\b\\B`` all match nothing and are NOT reported.
    Note the empty-source case is the opposite of silent: ``re.compile("")``
    matches every input, so it fails *loud* — flagging every scanned file — while
    still not being the rule it reads as.

    Note this claims nothing about semantic overlap between *different*
    patterns — e.g. ``os-system`` and ``process-exec`` both match
    ``os.system(``. That is intentional defence in depth, not a conflict, and a
    checker that guessed otherwise would be exactly the kind of unverifiable
    rule this slice avoids.
    """
    table = list(_DANGEROUS_PATTERNS if rules is None else rules)
    conflicts: List[str] = []
    seen_ids: Dict[str, int] = {}
    seen_patterns: Dict[str, str] = {}

    for index, rule in enumerate(table):
        where = f"rule #{index}"
        label = rule.rule_id or "unnamed"

        if not str(rule.rule_id).strip():
            conflicts.append(f"{where}: malformed — empty rule_id")
        source = rule.pattern.pattern
        if not source:
            conflicts.append(
                f"{where} ({label}): malformed — empty regex source, which matches "
                "every input, so the rule cannot discriminate; it is not the "
                "protection it reads as"
            )
        elif not source.strip():
            conflicts.append(
                f"{where} ({label}): malformed — whitespace-only regex source, which "
                "matches literal whitespace rather than the construct the rule reads as"
            )
        if rule.severity not in _VALID_SEVERITIES:
            conflicts.append(
                f"{where} ({label}): malformed — unknown severity "
                f"{rule.severity!r}, expected one of {sorted(_VALID_SEVERITIES)}"
            )

        if rule.rule_id:
            first_id_at = seen_ids.get(rule.rule_id)
            if first_id_at is not None:
                conflicts.append(
                    f"{where} ({label}): duplicate rule_id, already declared at "
                    f"rule #{first_id_at}"
                )
            else:
                seen_ids[rule.rule_id] = index

        source = rule.pattern.pattern
        if source:
            owner = seen_patterns.get(source)
            if owner is not None:
                conflicts.append(
                    f"{where} ({label}): pattern already declared by {owner} — "
                    "the later rule cannot produce a distinct verdict, so it "
                    "changes nothing while appearing to tighten the gate"
                )
            else:
                seen_patterns[source] = label

    return conflicts


@dataclass
class SafetyGateVerdict:
    """Result of the safety-review gate on a set of changed files."""

    safe: bool
    violations: List[str] = field(default_factory=list)
    files_scanned: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SafetyGateVerdict":
        return cls(
            safe=bool(d.get("safe", True)),
            violations=list(d.get("violations", []) or []),
            files_scanned=int(d.get("files_scanned", 0)),
        )


def scan_code_for_safety_violations(
    file_contents: Dict[str, str],
) -> List[str]:
    """Scan *file_contents* (path → content) for dangerous code patterns.

    Returns a list of violation strings, each formatted as:
    ``"<path>:<line>: <description> [<severity>]"``.

    Only scans ``.py`` files — non-Python files are skipped (the patterns
    target Python syntax). The scan is line-by-line for accurate line
    numbers and to avoid multi-line false positives.
    """
    violations: List[str] = []
    for path, content in file_contents.items():
        if not path.endswith(".py"):
            continue
        for line_no, line in enumerate((content or "").splitlines(), start=1):
            # Skip comments and strings that merely *mention* a pattern.
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for rule in _DANGEROUS_PATTERNS:
                if rule.pattern.search(line):
                    violations.append(
                        f"{path}:{line_no}: {rule.description} [{rule.severity}]"
                    )
    return violations


def check_safety_gate(
    files: Sequence[Dict[str, Any]],
    source_contents: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Run the safety-review gate and return blocking-violation strings.

    ``files`` is the ``gh pr view --json files`` shape. ``source_contents``
    is an optional map of source file path → full content (the same shape
    used by the reachability gate). When ``source_contents`` is ``None``
    or empty, the code scan is skipped (opt-in, same pattern as the flip/floor
    gates — skills without generated code merge as before).

    The guard-rule table's own well-formedness is checked FIRST and is not
    opt-out: a malformed or shadowed rule is a defect in the gate itself, so it
    is refused whether or not this PR has any Python to scan. On the shipped
    table the check yields nothing and behaviour is unchanged.
    """
    rule_conflicts = find_rule_conflicts()
    if rule_conflicts:
        return [f"SAFETY_GATE_RULE_CONFLICT: {c}" for c in rule_conflicts]

    if not source_contents:
        return []  # opt-out: no contents to scan

    # Only scan files that are actually in the PR's changed-file set.
    changed_paths = {str(f.get("path", "")) for f in files if isinstance(f, dict)}
    relevant = {
        path: content
        for path, content in source_contents.items()
        if path in changed_paths
    }

    violations = scan_code_for_safety_violations(relevant)
    return [f"SAFETY_GATE_VIOLATION: {v}" for v in violations]
