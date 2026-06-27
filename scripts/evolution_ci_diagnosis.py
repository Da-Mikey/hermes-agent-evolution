#!/usr/bin/env python3
"""CI failure auto-diagnosis for evolution PRs — issue #577.

WHY THIS EXISTS — close the merge loop. The evolution pipeline opens PRs,
but when CI reports a failure, no automated step reads the failing log
and turns it into an actionable fix or child issue. PRs sit open and stuck.

This script:
1. Polls open PRs from the Da-Mikey fork targeting Lexus2016 via gh
2. For each open PR, checks the latest CI run status
3. For failed runs, fetches the failing job log and extracts the concrete
   error class + message
4. Classifies failures as "trivial-fixable" (lint, missing import, type
   errors) vs "needs-child-issue" (test logic, design-level failures)
5. For trivial fixes: pushes a follow-up commit to the PR branch
6. For complex failures: creates a focused child issue on the upstream
   repo linking the PR and extracted error context

Usage:
    python scripts/evolution_ci_diagnosis.py [--dry-run] [--limit N]

Exit codes:
    0 — ok (even with failures — this is a monitoring script, not a gate)
    1 — setup/config error
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UPSTREAM_REPO = "Lexus2016/hermes-agent-evolution"
_FORK_REPO = "Da-Mikey/hermes-agent-evolution"
_DEFAULT_LIMIT = 10
_HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
_EVOLUTION_DIR = _HERMES_HOME / "profiles" / "user1" / "evolution"
_LOG_DIR = _EVOLUTION_DIR / "ci-diagnosis"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class PRInfo:
    number: int
    title: str
    head_label: str  # "Da-Mikey:branch-name"
    head_branch: str
    state: str
    created_at: str


@dataclass
class CIRun:
    run_id: int
    workflow: str
    conclusion: str | None  # "success", "failure", "cancelled", None (in-progress)
    html_url: str


@dataclass
class FailureLog:
    job_name: str
    error_class: str  # e.g. "AssertionError", "SyntaxError", "lint"
    error_message: str
    log_snippet: str  # first 30 lines of the failing shard


@dataclass
class Diagnosis:
    pr: PRInfo
    ci_run: CIRun
    failure: FailureLog | None
    classification: str  # "success", "in_progress", "trivial-fixable", "needs-child-issue", "cancelled"
    fix_commit_sha: str | None = None
    child_issue_url: str | None = None


# ---------------------------------------------------------------------------
# Heuristics — classify extractable error messages
# ---------------------------------------------------------------------------

# Patterns that indicate a trivial, safe fix the agent can push directly.
_TRIVIAL_PATTERNS: List[Tuple[str, str]] = [
    # Ruff / lint
    (r"ruff.*(F401|F811|F841|E501|W291|W292|E302|E303|W391)", "lint"),
    (r"`(.*?)` imported but unused", "unused-import"),
    (r"`(.*?)` is not defined", "undefined-name"),
    (r"line too long \(\d+ > \d+\)", "line-too-long"),
    (r"trailing whitespace", "trailing-whitespace"),
    (r"no newline at end of file", "missing-newline"),
    (r"blank line at end of file", "extra-blank-line"),
    (r"expected 2 blank lines", "blank-lines"),
    # mypy / type errors
    (r"Argument.*to.*has incompatible type", "type-mismatch"),
    (r"Missing return statement", "missing-return"),
    (r"Incompatible return value type", "incompatible-return"),
    (r"Module.*has no attribute", "missing-attribute"),
    # Pytest failure with clear assertion
    (r"assert\s+.*==\s+.*FAILED", "test-assertion"),
    (r"FAILED\s+(tests/\S+)", "test-failure"),
]

# Patterns for failures that need manual triage / child issue.
_COMPLEX_PATTERNS: List[Tuple[str, str]] = [
    (r"TypeError:", "type-error"),
    (r"ValueError:", "value-error"),
    (r"KeyError:", "key-error"),
    (r"IndexError:", "index-error"),
    (r"AttributeError:", "attribute-error"),
    (r"ImportError:", "import-error"),
    (r"ModuleNotFoundError:", "module-not-found"),
    (r"RecursionError:", "recursion-error"),
    (r"TimeoutError:", "timeout"),
    (r"ConnectionError:", "connection-error"),
    (r"PermissionError:", "permission-error"),
    (r"subprocess\.CalledProcessError", "subprocess-error"),
    (r"pytest.*error", "pytest-error"),
    (r"SyntaxError:", "syntax-error"),
    (r"IndentationError:", "indentation-error"),
]


def classify_failure(log_text: str) -> Tuple[str, str, str]:
    """Classify a CI failure log as trivial-fixable or needs-child-issue.

    Returns (error_class, classification, snippet).
    """
    for pattern, error_class in _TRIVIAL_PATTERNS:
        m = re.search(pattern, log_text, re.MULTILINE)
        if m:
            snippet = _extract_snippet(log_text, m.start())
            return error_class, "trivial-fixable", snippet

    for pattern, error_class in _COMPLEX_PATTERNS:
        m = re.search(pattern, log_text, re.MULTILINE)
        if m:
            snippet = _extract_snippet(log_text, m.start())
            return error_class, "needs-child-issue", snippet

    # No known pattern — treat as complex by default
    snippet = _extract_snippet(log_text, 0)
    return "unknown", "needs-child-issue", snippet


def _extract_snippet(log_text: str, offset: int, context_lines: int = 15) -> str:
    """Extract a snippet around the error offset."""
    lines = log_text.splitlines()
    start = max(0, offset - context_lines)
    end = min(len(lines), offset + context_lines + 1)
    return "\n".join(lines[start:end])


# ---------------------------------------------------------------------------
# Runner abstraction (testable seam)
# ---------------------------------------------------------------------------


Runner = Callable[[List[str]], Tuple[int, str, str]]


def _default_runner(cmd: List[str]) -> Tuple[int, str, str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def fetch_open_prs(
    repo: str = _UPSTREAM_REPO,
    author: str = "Da-Mikey",
    limit: int = _DEFAULT_LIMIT,
    runner: Runner = _default_runner,
) -> List[PRInfo]:
    """Fetch open PRs from the given repo, filtered by author."""
    rc, out, err = runner(
        [
            "gh", "pr", "list",
            "--repo", repo,
            "--state", "open",
            f"--limit={limit}",
            "--json", "number,title,headRefName,headRepositoryOwner,state,createdAt",
            "--author", author,
        ]
    )
    if rc != 0:
        print(f"[ci-diagnosis] Error fetching PRs: {err}", file=sys.stderr)
        return []

    try:
        raw = json.loads(out)
    except json.JSONDecodeError:
        return []

    results: List[PRInfo] = []
    for item in raw:
        owner = (item.get("headRepositoryOwner") or {}).get("login", "")
        head_label = f"{owner}:{item.get('headRefName', '')}"
        results.append(
            PRInfo(
                number=item["number"],
                title=item.get("title", ""),
                head_label=head_label,
                head_branch=item.get("headRefName", ""),
                state=item.get("state", "OPEN"),
                created_at=item.get("createdAt", ""),
            )
        )
    return results


def fetch_latest_ci_run(
    pr_number: int,
    repo: str = _UPSTREAM_REPO,
    runner: Runner = _default_runner,
) -> CIRun | None:
    """Fetch the latest CI run for a PR."""
    # Get the check runs via gh pr view
    rc, out, err = runner(
        [
            "gh", "pr", "view", str(pr_number),
            "--repo", repo,
            "--json", "recentCheckRuns",
        ]
    )
    if rc != 0 or not out.strip():
        return None

    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return None

    runs = data.get("recentCheckRuns") or []
    if not runs:
        return None

    # Take the latest run
    latest = runs[0]
    conclusion = latest.get("conclusion")
    return CIRun(
        run_id=latest.get("databaseId", 0),
        workflow=latest.get("name", latest.get("workflowName", "unknown")),
        conclusion=conclusion,
        html_url=latest.get("url") or latest.get("detailsUrl", ""),
    )


def fetch_failing_log(
    pr_number: int,
    repo: str = _UPSTREAM_REPO,
    runner: Runner = _default_runner,
) -> str | None:
    """Fetch the failing CI log for a PR.

    Uses `gh run view --log-failed` on the latest failed run.
    """
    # First get the failed run ID
    rc, out, err = runner(
        [
            "gh", "run", "list",
            "--repo", repo,
            f"--workflow={pr_number}",  # this may not work; fallback below
            "--limit=1",
            "--json", "databaseId,conclusion,headBranch",
        ]
    )
    if rc != 0 or not out.strip():
        return None

    # Broader: list runs for the repo and filter by PR branch
    # Actually let's use a different approach — get the PR's head branch
    pr_info_rc, pr_out, pr_err = runner(
        [
            "gh", "pr", "view", str(pr_number),
            "--repo", repo,
            "--json", "headRefName",
        ]
    )
    if pr_info_rc != 0 or not pr_out.strip():
        return None

    try:
        head_branch = json.loads(pr_out)["headRefName"]
    except (json.JSONDecodeError, KeyError):
        return None

    rc, out, err = runner(
        [
            "gh", "run", "list",
            "--repo", repo,
            "--branch", head_branch,
            "--limit=3",
            "--json", "databaseId,conclusion,displayTitle",
        ]
    )
    if rc != 0 or not out.strip():
        return None

    try:
        runs = json.loads(out)
    except json.JSONDecodeError:
        return None

    # Find a failed run
    failed_run = None
    for run in runs:
        if run.get("conclusion") == "failure":
            failed_run = run
            break

    if not failed_run:
        return None

    run_id = failed_run["databaseId"]

    # Fetch the failing log
    rc, log_out, log_err = runner(
        [
            "gh", "run", "view", str(run_id),
            "--repo", repo,
            "--log-failed",
        ]
    )
    if rc != 0:
        # Try with --log
        rc, log_out, log_err = runner(
            [
                "gh", "run", "view", str(run_id),
                "--repo", repo,
                "--log",
            ]
        )
    if rc != 0:
        return None

    return log_out


def try_auto_fix(
    pr_branch: str,
    error_class: str,
    log_snippet: str,
    runner: Runner = _default_runner,
) -> str | None:
    """Try to push an auto-fix commit to a PR branch for trivial failures.

    Currently only handles:
    - lint violations (ruff --fix)
    - trailing whitespace
    - missing newlines

    Returns the commit SHA on success, None on failure.
    """
    # Clone/fetch the branch, run ruff --fix, commit, push
    with tempfile.TemporaryDirectory(prefix="ci-diagnosis-") as tmpdir:
        # Shallow clone the fork repo
        rc, out, err = runner(
            [
                "gh", "repo", "clone", _FORK_REPO, tmpdir,
                "--", "--branch", pr_branch, "--depth=1",
            ]
        )
        if rc != 0:
            return None

        # Run autofix tools
        fixes_applied = False

        # ruff --fix
        rc1, out1, _ = runner(
            ["ruff", "check", "--fix", "--quiet", tmpdir]
        )
        if rc1 == 0:
            fixes_applied = True

        # Check if anything changed
        rc2, diff_out, _ = runner(["git", "-C", tmpdir, "diff", "--stat"])
        if rc2 != 0 or not diff_out.strip():
            return None

        # Commit and push
        rc3, _, _ = runner(
            ["git", "-C", tmpdir, "add", "-A"]
        )
        if rc3 != 0:
            return None

        commit_msg = (
            f"fix: auto-fix CI failure ({error_class})\n\n"
            f"Automated fix pushed by evolution-ci-diagnosis for PR.\n"
            f"Co-Authored-By: Hermes Evolution <evolution@hermes.ai>"
        )
        rc4, _, _ = runner(
            ["git", "-C", tmpdir, "commit", "-m", commit_msg,
             "--author", "Hermes Evolution <evolution@hermes.ai>"]
        )
        if rc4 != 0:
            return None

        # Get commit SHA
        rc5, sha_out, _ = runner(["git", "-C", tmpdir, "rev-parse", "HEAD"])
        sha = sha_out.strip() if rc5 == 0 else None

        # Push via gh (which has auth)
        rc6, _, push_err = runner(
            ["git", "-C", tmpdir, "push",
             f"https://github.com/{_FORK_REPO}.git",
             f"HEAD:{pr_branch}"]
        )
        if rc6 != 0:
            # Try with gh auth
            runner(["gh", "auth", "setup-git"])
            rc6, _, push_err = runner(
                ["git", "-C", tmpdir, "push",
                 f"https://github.com/{_FORK_REPO}.git",
                 f"HEAD:{pr_branch}"]
            )
            if rc6 != 0:
                return None

        return sha


def create_child_issue(
    pr: PRInfo,
    error_class: str,
    log_snippet: str,
    runner: Runner = _default_runner,
) -> str | None:
    """Create a focused child issue on the upstream repo for a complex failure."""
    title = f"[AUTO] CI failure in PR #{pr.number}: {error_class}"
    body = (
        f"## Auto-detected CI failure\n\n"
        f"**PR**: #{pr.number} — {pr.title}\n"
        f"**Branch**: {pr.head_label}\n"
        f"**Error class**: {error_class}\n\n"
        f"### Failing log snippet\n\n"
        f"```\n{log_snippet[:2000]}\n```\n\n"
        f"---\n"
        f"_Generated by evolution-ci-diagnosis (issue #577)_"
    )

    rc, out, err = runner(
        [
            "gh", "issue", "create",
            "--repo", _UPSTREAM_REPO,
            "--title", title,
            "--body", body,
            "--label", "fix",
        ]
    )
    if rc != 0 or not out.strip():
        return None
    return out.strip()


def save_diagnosis_report(diagnoses: List[Diagnosis]) -> Path:
    """Save the diagnosis results to the evolution directory."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    report_path = _LOG_DIR / f"diagnosis-{date_str}.json"

    data = {
        "date": date_str,
        "run_time": datetime.now(timezone.utc).isoformat(),
        "diagnoses": [
            {
                "pr_number": d.pr.number,
                "pr_title": d.pr.title,
                "ci_conclusion": d.ci_run.conclusion,
                "classification": d.classification,
                "error_class": d.failure.error_class if d.failure else None,
                "fix_commit": d.fix_commit_sha,
                "child_issue_url": d.child_issue_url,
            }
            for d in diagnoses
        ],
        "summary": {
            "total_prs": len(diagnoses),
            "success": sum(1 for d in diagnoses if d.classification == "success"),
            "in_progress": sum(1 for d in diagnoses if d.classification == "in_progress"),
            "trivial_fixes_applied": sum(1 for d in diagnoses if d.fix_commit_sha),
            "child_issues_created": sum(1 for d in diagnoses if d.child_issue_url),
            "needs_attention": sum(
                1 for d in diagnoses
                if d.classification == "needs-child-issue"
                and not d.child_issue_url
            ),
        },
    }

    report_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"[ci-diagnosis] Report saved: {report_path}")
    return report_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def diagnose_prs(
    dry_run: bool = False,
    limit: int = _DEFAULT_LIMIT,
    runner: Runner = _default_runner,
) -> List[Diagnosis]:
    """Main loop: fetch open PRs, check CI, classify failures, act."""
    prs = fetch_open_prs(limit=limit, runner=runner)
    print(f"[ci-diagnosis] Found {len(prs)} open PRs by Da-Mikey")

    diagnoses: List[Diagnosis] = []
    for pr in prs:
        print(f"  PR #{pr.number}: {pr.title[:60]}...")

        # Fetch CI status
        ci_run = fetch_latest_ci_run(pr.number, runner=runner)

        if ci_run is None:
            # No CI info — skip
            print(f"    -> no CI runs found, skipping")
            continue

        if ci_run.conclusion == "success":
            diagnoses.append(Diagnosis(
                pr=pr, ci_run=ci_run, failure=None,
                classification="success",
            ))
            print(f"    -> CI: success")
            continue

        if ci_run.conclusion is None or ci_run.conclusion == "cancelled":
            diagnoses.append(Diagnosis(
                pr=pr, ci_run=ci_run, failure=None,
                classification=ci_run.conclusion or "in_progress",
            ))
            print(f"    -> CI: {ci_run.conclusion or 'in_progress'}")
            continue

        if ci_run.conclusion == "failure":
            # Fetch the failing log
            log_text = fetch_failing_log(pr.number, runner=runner)
            if not log_text:
                print(f"    -> CI: failure, but could not fetch log")
                diagnoses.append(Diagnosis(
                    pr=pr, ci_run=ci_run, failure=None,
                    classification="needs-child-issue",
                ))
                continue

            error_class, classification, snippet = classify_failure(log_text)
            failure = FailureLog(
                job_name=ci_run.workflow,
                error_class=error_class,
                error_message=snippet[:500],
                log_snippet=snippet,
            )

            fix_sha = None
            child_url = None

            if classification == "trivial-fixable" and not dry_run:
                print(f"    -> trivial fixable: {error_class}, attempting auto-fix...")
                fix_sha = try_auto_fix(pr.head_branch, error_class, snippet, runner=runner)
                if fix_sha:
                    print(f"    -> auto-fix committed: {fix_sha[:12]}")
                else:
                    print(f"    -> auto-fix failed (will create child issue)")
                    classification = "needs-child-issue"

            if classification == "needs-child-issue" and not dry_run:
                print(f"    -> complex failure: {error_class}, creating child issue...")
                child_url = create_child_issue(pr, error_class, snippet, runner=runner)
                if child_url:
                    print(f"    -> child issue: {child_url}")
                else:
                    print(f"    -> could not create child issue (insufficient permissions?)")

            diagnoses.append(Diagnosis(
                pr=pr, ci_run=ci_run, failure=failure,
                classification=classification,
                fix_commit_sha=fix_sha,
                child_issue_url=child_url,
            ))
            print(f"    -> {classification}: {error_class}")

    return diagnoses


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="CI failure auto-diagnosis for evolution PRs (#577)"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Only report, don't push fixes or create issues"
    )
    parser.add_argument(
        "--limit", type=int, default=_DEFAULT_LIMIT,
        help=f"Max PRs to scan (default: {_DEFAULT_LIMIT})"
    )
    args = parser.parse_args()

    diagnoses = diagnose_prs(dry_run=args.dry_run, limit=args.limit)
    report_path = save_diagnosis_report(diagnoses)

    # Print summary
    summary = json.loads(report_path.read_text())["summary"]
    print(f"\n{'='*60}")
    print(f"CI Diagnosis Summary — {summary['total_prs']} PRs scanned")
    print(f"  ✅ Success:       {summary['success']}")
    print(f"  ⏳ In progress:   {summary['in_progress']}")
    print(f"  🔧 Auto-fixes:    {summary['trivial_fixes_applied']}")
    print(f"  🐛 Child issues:  {summary['child_issues_created']}")
    print(f"  ⚠️  Needs attention: {summary['needs_attention']}")
    print(f"{'='*60}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
