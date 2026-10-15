# -*- coding: utf-8 -*-
"""Unit tests for the safety-review gate (#2575)."""

import re

from evolution.lib.safety_review_gate import (
    _DANGEROUS_PATTERNS,
    GuardRule,
    SafetyGateVerdict,
    check_safety_gate,
    find_rule_conflicts,
    scan_code_for_safety_violations,
)


def _safe_code() -> str:
    return (
        "def helper():\n"
        "    return 42\n"
        "\n"
        "def main():\n"
        "    result = helper()\n"
        "    print(result)\n"
    )


def _dangerous_code() -> str:
    return "import os\ndef main():\n    os.system('rm -rf /')\n    eval(user_input)\n"


class TestScanCodeForSafetyViolations:
    def test_safe_code_no_violations(self):
        violations = scan_code_for_safety_violations({"mod.py": _safe_code()})
        assert violations == []

    def test_dangerous_code_flagged(self):
        violations = scan_code_for_safety_violations({"mod.py": _dangerous_code()})
        assert len(violations) >= 2
        joined = "\n".join(violations)
        assert "os.system" in joined
        assert "eval" in joined

    def test_non_python_files_skipped(self):
        violations = scan_code_for_safety_violations({
            "script.sh": "os.system('rm -rf /')"
        })
        assert violations == []

    def test_line_numbers_reported(self):
        violations = scan_code_for_safety_violations({"mod.py": _dangerous_code()})
        assert any(v.startswith("mod.py:3:") for v in violations)
        assert any(v.startswith("mod.py:4:") for v in violations)

    def test_comment_lines_skipped(self):
        code = "# os.system('rm -rf /')\nprint('ok')\n"
        violations = scan_code_for_safety_violations({"mod.py": code})
        assert violations == []


class TestCheckSafetyGate:
    def test_opt_out_when_no_contents(self):
        files = [{"path": "mod.py", "additions": 1, "deletions": 0}]
        assert check_safety_gate(files, source_contents=None) == []
        assert check_safety_gate(files, source_contents={}) == []

    def test_blocks_dangerous_changed_file(self):
        files = [{"path": "mod.py", "additions": 1, "deletions": 0}]
        contents = {"mod.py": _dangerous_code()}
        violations = check_safety_gate(files, source_contents=contents)
        assert violations
        assert all(v.startswith("SAFETY_GATE_VIOLATION:") for v in violations)

    def test_only_scans_changed_files(self):
        files = [{"path": "mod.py", "additions": 1, "deletions": 0}]
        contents = {
            "mod.py": _safe_code(),
            "other.py": _dangerous_code(),  # not in the PR's changed set
        }
        assert check_safety_gate(files, source_contents=contents) == []

    def test_verdict_serialization(self):
        v = SafetyGateVerdict(safe=False, violations=["v1"], files_scanned=2)
        d = v.to_dict()
        restored = SafetyGateVerdict.from_dict(d)
        assert restored.safe is False
        assert restored.violations == ["v1"]
        assert restored.files_scanned == 2


class TestGuardRuleTable:
    """#204 slice 1 — the rule set as declared data, plus a deterministic
    conflict check over it. No admission policy, no threshold tuning, and no
    change to which rules exist.
    """

    def test_shipped_table_is_conflict_free(self) -> None:
        """The table actually in force must pass its own checker. If this ever
        fails, a rule edit landed malformed or shadowed and the gate would
        refuse every merge (fail-closed) — asserted, not assumed."""
        assert find_rule_conflicts() == []

    def test_every_shipped_rule_is_well_formed(self) -> None:
        table = list(_DANGEROUS_PATTERNS)
        assert table, "a gate with no rules matches nothing"
        for rule in table:
            assert rule.rule_id.strip()
            assert rule.pattern.pattern.strip()
            assert rule.severity in {"low", "medium", "high"}
        ids = [r.rule_id for r in table]
        assert len(ids) == len(set(ids)), "rule ids must be unique handles"

    def test_empty_regex_source_is_reported(self) -> None:
        """An empty source is degenerate, not silent: `re.compile("")` matches
        every input, so the report says the rule cannot discriminate instead of
        claiming it matches nothing."""
        bad = [GuardRule("empty", re.compile(""), "no-op", "high")]
        conflicts = find_rule_conflicts(bad)
        assert len(conflicts) == 1
        assert "empty regex source" in conflicts[0]
        assert "matches nothing" not in conflicts[0]

    def test_whitespace_only_source_is_reported(self) -> None:
        """`re.compile("   ")` matches three literal spaces — it matches
        *something*, so it is reported as whitespace-only, not as matching
        nothing, and never as "fails open"."""
        bad = [GuardRule("blank", re.compile("   "), "no-op", "high")]
        conflicts = find_rule_conflicts(bad)
        assert len(conflicts) == 1
        assert "whitespace-only regex source" in conflicts[0]
        assert "matches nothing" not in conflicts[0]

    def test_semantically_empty_patterns_are_out_of_scope(self) -> None:
        """Documented boundary, not an oversight: these match nothing and are NOT
        reported, because proving emptiness needs a regex-language check, not a
        source test. Narrowing the report to "empty regex source" must not be
        read as a claim of coverage over these."""
        for source in (r"(?!)", r"[^\s\S]", r"\b\B"):
            bad = [GuardRule("never", re.compile(source), "no-op", "high")]
            assert find_rule_conflicts(bad) == [], source

    def test_duplicate_pattern_under_two_ids_is_reported(self) -> None:
        bad = [
            GuardRule("first", re.compile(r"\bos\.system\s*\("), "a", "high"),
            GuardRule("second", re.compile(r"\bos\.system\s*\("), "b", "high"),
        ]
        conflicts = find_rule_conflicts(bad)
        assert len(conflicts) == 1
        assert "already declared by first" in conflicts[0]

    def test_duplicate_rule_id_is_reported(self) -> None:
        bad = [
            GuardRule("dup", re.compile(r"\balpha\s*\("), "a", "high"),
            GuardRule("dup", re.compile(r"\bbeta\s*\("), "b", "high"),
        ]
        conflicts = find_rule_conflicts(bad)
        assert len(conflicts) == 1
        assert "duplicate rule_id" in conflicts[0]

    def test_unknown_severity_is_reported(self) -> None:
        bad = [GuardRule("sev", re.compile(r"\bgamma\s*\("), "c", "catastrophic")]
        assert "unknown severity" in find_rule_conflicts(bad)[0]

    def test_checker_is_pure_and_total(self) -> None:
        """Same table in, same list out — called twice, no state leaks."""
        bad = [GuardRule("", re.compile(""), "", "bogus")]
        first = find_rule_conflicts(bad)
        assert first == find_rule_conflicts(bad)
        assert len(first) == 3, "empty id + empty pattern + bad severity"

    def test_distinct_overlapping_patterns_are_not_a_conflict(self) -> None:
        """Defence in depth is not a conflict. `os-system` and `process-exec`
        both match `os.system(`, deliberately — flagging that would demand a
        rule deletion this slice is not allowed to make."""
        table = {r.rule_id: r for r in _DANGEROUS_PATTERNS}
        probe = "os.system('ls')"
        assert table["os-system"].pattern.search(probe)
        assert table["process-exec"].pattern.search(probe)
        assert find_rule_conflicts() == []

    def test_gate_refuses_when_the_rule_table_is_broken(self, monkeypatch) -> None:
        """`check_safety_gate` runs the table check FIRST, even when the code scan
        is opted out, so a malformed rule cannot slip through a PR with no Python
        to scan.

        This only proves internal ordering, which is why it is not the rework's
        proof: the gate itself is reached only when the caller supplies
        `source_contents`, and nothing on the production path does. The live-path
        proof is `TestMergeGateReachability` below (see the PR #212 brief)."""
        import evolution.lib.safety_review_gate as gate

        monkeypatch.setattr(
            gate,
            "_DANGEROUS_PATTERNS",
            [GuardRule("broken", re.compile(""), "no-op", "high")],
        )
        out = gate.check_safety_gate([], source_contents=None)
        assert out and out[0].startswith("SAFETY_GATE_RULE_CONFLICT:")
        assert "empty regex source" in out[0]


class TestMergeGateReachability:
    """The rework fix, and the DoD for it: a conflicted rule table must stop a
    merge through the REAL entry point.

    `find_rule_conflicts()` used to hang off `check_safety_gate`, whose only
    non-test caller on `scripts/evolution_merge_gate.py` is guarded by
    `if source_contents is not None:` — a condition nothing on the production
    path satisfies. The checker was therefore unreachable and protected nothing.
    It is now called directly from `check_merge_policy_with_quality`, which is
    driven here (not `check_safety_gate([], source_contents=None)`, which would
    only re-test internal ordering).
    """

    @staticmethod
    def _merge_gate():
        import sys as _sys
        from pathlib import Path as _Path

        _sys.path.insert(0, str(_Path(__file__).resolve().parents[2] / "scripts"))
        import evolution_merge_gate

        return evolution_merge_gate

    def test_conflicted_table_blocks_the_live_entry_point(self, monkeypatch) -> None:
        import evolution.lib.safety_review_gate as gate

        monkeypatch.setattr(
            gate,
            "_DANGEROUS_PATTERNS",
            [GuardRule("broken", re.compile(""), "no-op", "high")],
        )
        merge_gate = self._merge_gate()
        violations = merge_gate.check_merge_policy_with_quality(
            [{"path": "scripts/foo.py", "additions": 3, "deletions": 0}]
        )
        blocking = [v for v in violations if v.startswith("GUARD_RULE_CONFLICT:")]
        assert blocking, f"a conflicted table must stop the merge; got {violations}"
        assert "empty regex source" in blocking[0]

    def test_conflicted_table_blocks_without_source_contents(self, monkeypatch) -> None:
        """The exact regression: no `source_contents` at all, which is what the
        production path does, and the block must still fire."""
        import evolution.lib.safety_review_gate as gate

        monkeypatch.setattr(
            gate,
            "_DANGEROUS_PATTERNS",
            [
                GuardRule("dup", re.compile(r"\balpha\s*\("), "a", "high"),
                GuardRule("dup", re.compile(r"\bbeta\s*\("), "b", "high"),
            ],
        )
        merge_gate = self._merge_gate()
        violations = merge_gate.check_merge_policy_with_quality(
            [], source_contents=None
        )
        assert any(
            v.startswith("GUARD_RULE_CONFLICT:") and "duplicate rule_id" in v
            for v in violations
        ), violations

    def test_shipped_table_is_clean_on_the_live_entry_point(self) -> None:
        """Live AND green on the real path: the check runs, and reports nothing
        for the table actually in force."""
        merge_gate = self._merge_gate()
        violations = merge_gate.check_merge_policy_with_quality(
            [{"path": "scripts/foo.py", "additions": 3, "deletions": 0}]
        )
        assert not [v for v in violations if v.startswith("GUARD_RULE_CONFLICT:")]

    def test_violation_format_unchanged(self) -> None:
        """Regression guard: declaring the rules as data must not alter the
        violation string the merge gate already formats.

        Two lines are emitted for one source line because `os.system(` is
        matched by both `os-system` and `process-exec` — the intended overlap
        asserted above, pinned here at the string level too.
        """
        violations = scan_code_for_safety_violations(
            {"m.py": "import os\nos.system('ls')\n"}
        )
        assert violations == [
            "m.py:2: os.system() — shell injection risk; use subprocess with "
            "shell=False [high]",
            "m.py:2: subprocess/os process execution — verify command is not "
            "constructed from user input [medium]",
        ]
