#!/usr/bin/env python3
"""Agentic QC review loop (#796): build a QC review task for delegate_task and
parse the subagent's report to gate completion. CLI call sites — no dead code."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# The three outcomes a QC gate can report (#234). ``unscored`` is not a third flavour of
# failure: it means the gate produced no measurement at all (unparseable reviewer output,
# an unrecognised verdict, a reviewer that never ran), so a caller can alarm on
# non-measurement instead of counting a non-result as a rejected candidate.
_OUTCOMES = ("pass", "fail", "unscored")

# Distinct exit code for "no measurement", so CI and callers can tell an unscored review
# apart from a measured rework demand (exit 1) — the point of the third value.
EXIT_UNSCORED = 3


def _record(
    outcome: str,
    *,
    verdict: str = "",
    has_blocking: bool = False,
    issues: Optional[List[Any]] = None,
    recommendations: Optional[List[Any]] = None,
    ok: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build one QC record around a validated three-valued outcome (#234).

    Every list is built fresh: a record handed to a caller must never share mutable state
    with a module-level template. ``dict(_FAIL)`` was a shallow copy, so one caller
    appending to ``issues`` leaked into every record built afterwards (PR #244 review,
    defect 3).
    """
    if outcome not in _OUTCOMES:
        # A bare ``assert`` vanishes under ``python -O``, silently disabling the only check
        # that keeps a typo out of every downstream aggregate (PR #244 review, defect 4).
        raise ValueError(f"unknown QC outcome: {outcome!r}")
    return {
        "outcome": outcome,
        "verdict": verdict,
        "has_blocking": bool(has_blocking),
        "issues": list(issues or ()),
        "recommendations": list(recommendations or ()),
        "ok": bool(ok),
        "config": dict(config) if config is not None else None,
    }


# ``_unscored`` marks the template the two-valued core returns when it cannot extract any
# JSON: a marker for "no measurement", never a measured verdict.
_FAIL = {**_record("fail", verdict="fail", has_blocking=True), "_unscored": True}


def _s(v: Any) -> str:
    return v.strip() if isinstance(v, str) else ""


def build_config_fingerprint(
    prompt: Optional[str],
    *,
    outcome: str,
    critic_enabled: bool = True,
) -> Dict[str, Any]:
    """Fingerprint the configuration a QC score was produced under (#234).

    Two scores are only comparable within one prompt and one gate configuration, so
    ``prompt_hash`` hashes the actual prompt text handed to the reviewer. Hashing a version
    string instead (``qc-review/v1``) makes it the same constant for every record forever,
    blinding the fingerprint to exactly the prompt drift it exists to catch (PR #244
    review, defect 1).

    ``None`` when no prompt was supplied: an honest "unknown", rather than a hash of
    nothing that would compare equal across records it cannot vouch for.
    """
    if outcome not in _OUTCOMES:
        raise ValueError(f"unknown QC outcome: {outcome!r}")
    return {
        "prompt_hash": (
            None
            if prompt is None
            else hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        ),
        "outcome": outcome,
        "gate": "critic-on" if critic_enabled else "critic-off",
    }


def build_qc_review_task(
    summary: str,
    files: List[str],
    *,
    issue_number: int = 0,
    toolsets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a leaf-subagent QC review task for delegate_task."""
    file_list = "\n".join(f"  - {f}" for f in files) if files else "  (no files listed)"
    goal = (
        "Review the following completed implementation for security, correctness, "
        f"test coverage, and adherence to requirements.\n\n## Summary\n{summary}\n\n"
        f"## Files Changed\n{file_list}\n\n"
        "## Output Format\nReturn JSON:\n"
        '- "verdict": "pass"|"fail", "has_blocking": bool, "issues": [...], "recommendations": [...]\n'
    )
    context = (
        f"You are a quality-control reviewer. Issue #{issue_number}. "
        "Only mark fail+has_blocking for issues that MUST be fixed before merge."
    )
    return {"goal": goal, "context": context, "toolsets": list(toolsets) if toolsets is not None else ["file", "search"], "role": "leaf"}  # fmt: skip


def _extract_json(text: str) -> Optional[dict]:
    """Try direct parse, then ```json fenced block, then bare { ... } block."""
    candidates = [text]
    m1 = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
    m2 = re.search(r"\{.*\}", text, re.DOTALL)
    if m1:
        candidates.append(m1.group(1))
    if m2:
        candidates.append(m2.group(0))
    for c in candidates:
        try:
            raw = json.loads(c)
            if isinstance(raw, dict):
                return raw
        except (ValueError, TypeError):
            pass
    return None


def _parse_qc_two_valued(subagent_output: str) -> Dict[str, Any]:
    """Two-valued core, kept verbatim; ``parse_qc_report`` wraps it in three values.

    Returns a ``_FAIL`` copy carrying the ``_unscored`` marker when no JSON could be
    extracted — the caller, not this function, decides what non-measurement means.
    ok=True only when verdict=='pass' and not has_blocking.
    """
    raw = _extract_json(_s(subagent_output))
    if raw is None:
        return dict(_FAIL)
    verdict = _s(raw.get("verdict")).lower()
    hb = bool(raw.get("has_blocking", False))
    issues = raw.get("issues") if isinstance(raw.get("issues"), list) else []
    recs = raw.get("recommendations") if isinstance(raw.get("recommendations"), list) else []  # fmt: skip
    return {"verdict": verdict, "has_blocking": hb, "issues": issues, "recommendations": recs, "ok": verdict == "pass" and not hb}  # fmt: skip


def parse_qc_report(
    subagent_output: str,
    *,
    prompt: Optional[str] = None,
    critic_enabled: bool = True,
) -> Dict[str, Any]:
    """Parse QC subagent output into a three-valued record (#234).

    ``ok`` keeps its old meaning (verdict=='pass' and not has_blocking). Output that
    cannot be measured — no extractable JSON, or a verdict that is neither pass nor fail —
    becomes ``outcome="unscored"`` instead of being silently counted as a fail.
    ``verdict`` keeps its legacy value ("fail") on those records so existing callers do not
    change behaviour; ``outcome`` and the exit code carry the distinction.
    """
    legacy = _parse_qc_two_valued(subagent_output)
    ok = bool(legacy.get("ok"))
    verdict = _s(legacy.get("verdict")).lower()
    measurable = not legacy.get("_unscored") and verdict in ("pass", "fail")
    outcome = ("pass" if ok else "fail") if measurable else "unscored"
    return _record(
        outcome,
        verdict=verdict or "fail",
        has_blocking=legacy.get("has_blocking", False),
        issues=legacy.get("issues"),
        recommendations=legacy.get("recommendations"),
        ok=ok,
        config=build_config_fingerprint(
            prompt, outcome=outcome, critic_enabled=critic_enabled
        ),
    )


def _files_from_args(args: List[str]) -> List[str]:
    files_str = _flag(args, "--files") or ""
    return [f.strip() for f in files_str.split(",") if f.strip()]


def build_critic_task(
    summary: str,
    files: List[str],
    *,
    issue_number: int = 0,
    toolsets: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build a leaf-subagent adversarial critic task for delegate_task (#92).

    Runs AFTER the reviewer has approved a diff: the critic's only job is to
    try to prove the change WRONG with one evidence-grounded, concrete failure
    mode. If it cannot construct such an objection, it must say so explicitly
    — the review then proceeds. This is the structured-disagreement pass that
    breaks reviewer/coder rubber-stamping (Adversarial Review, arXiv:2608.18167).
    """
    file_list = "\n".join(f"  - {f}" for f in files) if files else "  (no files listed)"
    goal = (
        "You are an adversarial code critic. The reviewer has APPROVED the "
        "following implementation. Your job is to try to prove it WRONG.\n\n"
        "Attempt to articulate ONE concrete, evidence-grounded failure mode: a "
        "specific way this change breaks, regresses, or introduces a bug, tied "
        "to the summary or a file below. Do NOT manufacture objections to seem "
        "rigorous — a fabricated disagreement is as harmful as rubber-stamped "
        "approval.\n\n"
        f"## Summary\n{summary}\n\n"
        f"## Files Changed\n{file_list}\n\n"
        "## Output Format\nReturn JSON:\n"
        '- "dissent": bool — true ONLY if you found a concrete failure mode\n'
        '- "grounded": bool — true only if your objection cites specific evidence\n'
        '- "failure_mode": string — the concrete failure mode (empty if none)\n'
        '- "agreement_reason": string — why you could NOT object (empty if dissenting)\n'
    )
    context = (
        f"You are the adversarial critic gate. Issue #{issue_number}. "
        "Only dissent when you can point to a concrete, evidence-grounded "
        "failure mode; otherwise agree explicitly and explain why."
    )
    return {
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets is not None else ["file", "search"],
        "role": "leaf",
    }


def parse_critic_report(subagent_output: str) -> Dict[str, Any]:
    """Parse the adversarial critic subagent output (#92).

    A report is ``ok`` when it is coherent: either a grounded dissent (a
    concrete ``failure_mode``) or an explicit agreement with a reason. A
    dissent WITHOUT a failure_mode is un-grounded noise and is not ok.
    """
    raw = _extract_json(_s(subagent_output))
    if raw is None:
        return {
            "dissent": False,
            "grounded": False,
            "failure_mode": "",
            "agreement_reason": "",
            "ok": False,
        }
    dissent = bool(raw.get("dissent", False))
    failure_mode = _s(raw.get("failure_mode"))
    grounded = dissent and bool(failure_mode)
    agreement_reason = _s(raw.get("agreement_reason"))
    ok = grounded if dissent else bool(agreement_reason)
    return {
        "dissent": dissent,
        "grounded": grounded,
        "failure_mode": failure_mode,
        "agreement_reason": agreement_reason,
        "ok": ok,
    }


def adjudicate_review(
    reviewer_report: Dict[str, Any],
    critic_report: Dict[str, Any],
    *,
    critic_enabled: bool = True,
) -> Dict[str, Any]:
    """Deterministic reviewer-to-critic gate (#92).

    Returns ``{"block", "verdict", "reason"}``:
    - reviewer not ok → block ("rework"), regardless of critic.
    - reviewer ok + critic disabled → proceed.
    - reviewer ok + grounded dissent → block ("rework").
    - reviewer ok + no grounded objection → proceed.
    """
    # An unscored reviewer is a non-measurement, not a judgement. It must still block —
    # never widen a gate on absent evidence — but it must not be reported as a measured
    # rework demand, or every unparseable reviewer inflates the rework rate (#234).
    if _s(reviewer_report.get("outcome")) == "unscored":
        return {
            "block": True,
            "outcome": "unscored",
            "verdict": "unscored",
            "reason": "reviewer produced no measurable verdict",
        }
    reviewer_ok = bool(reviewer_report.get("ok"))
    if not reviewer_ok:
        return {
            "block": True,
            "outcome": "fail",
            "verdict": "rework",
            "reason": "reviewer did not approve",
        }
    if not critic_enabled:
        return {
            "block": False,
            "outcome": "pass",
            "verdict": "proceed",
            "reason": "adversarial critic disabled",
        }
    dissent = bool(critic_report.get("dissent"))
    grounded = bool(critic_report.get("grounded"))
    if dissent and grounded:
        fm = _s(critic_report.get("failure_mode"))
        return {
            "block": True,
            "outcome": "fail",
            "verdict": "rework",
            "reason": f"critic grounded objection: {fm[:200]}",
        }
    return {
        "block": False,
        "outcome": "pass",
        "verdict": "proceed",
        "reason": "no grounded objection from critic",
    }


def record_critic_verdict(
    ledger_file: str,
    verdict: Dict[str, Any],
    *,
    reviewer_report: Dict[str, Any],
    critic_report: Dict[str, Any],
) -> None:
    """Record the adversarial-critic verdict into the stage-gate ledger (#92).

    Best-effort instrumentation: a failed import or write must never take the
    review flow down, so any exception is swallowed.
    """
    try:
        from evolution.lib.stage_gate import record_review_critic_verdict

        record_review_critic_verdict(
            Path(ledger_file),
            verdict="rework" if verdict.get("block") else "proceed",
            reviewer_ok=bool(reviewer_report.get("ok")),
            dissent=bool(critic_report.get("dissent")),
            grounded=bool(critic_report.get("grounded")),
            failure_mode=_s(critic_report.get("failure_mode")),
            agreement_reason=_s(critic_report.get("agreement_reason")),
        )
    except Exception:  # noqa: BLE001 - instrumentation must not raise
        pass


def _load_text(path: Optional[str]) -> Tuple[str, Optional[str]]:
    try:
        raw = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
        return raw, None
    except OSError as exc:
        return "", str(exc)


def _flag(args: List[str], name: str) -> Optional[str]:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return None


def _prompt_text(args: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """Load the ``--prompt`` task file whose text the config fingerprint hashes (#234).

    The prompt the reviewer was actually given is the only thing that makes two scores
    comparable, so it is read here and hashed into the record — never version-stamped.
    """
    path = _flag(args, "--prompt")
    if not path:
        return None, None
    return _load_text(path)


def main(argv: List[str]) -> int:
    if len(argv) < 2 or argv[1] in ("-h", "--help"):
        print(
            "usage: evolution_qc_review.py "
            "{build,parse,build-critic,parse-critic,adjudicate} ...",
            file=sys.stderr,
        )
        return 2
    cmd, args = argv[1], argv[2:]
    if cmd == "build":
        summary = _flag(args, "--summary") or ""
        if not _s(summary):
            return 2
        files = _files_from_args(args)
        try:
            issue_num = int(_flag(args, "--issue") or "0")
        except ValueError:
            issue_num = 0
        task = build_qc_review_task(summary, files, issue_number=issue_num)
        print(json.dumps(task, ensure_ascii=False))
        return 0
    if cmd == "build-critic":
        summary = _flag(args, "--summary") or ""
        if not _s(summary):
            return 2
        files = _files_from_args(args)
        try:
            issue_num = int(_flag(args, "--issue") or "0")
        except ValueError:
            issue_num = 0
        task = build_critic_task(summary, files, issue_number=issue_num)
        print(json.dumps(task, ensure_ascii=False))
        return 0
    if cmd == "parse":
        path = args[0] if args and not args[0].startswith("-") else None
        data, err = _load_text(path)
        if err:
            return 2
        prompt, perr = _prompt_text(args)
        if perr:
            return 2
        report = parse_qc_report(
            data, prompt=prompt, critic_enabled="--no-critic" not in args
        )
        print(json.dumps(report, ensure_ascii=False))
        if report["outcome"] == "unscored":
            return EXIT_UNSCORED
        return 0 if report["ok"] else 1
    if cmd == "parse-critic":
        path = args[0] if args and not args[0].startswith("-") else None
        data, err = _load_text(path)
        if err:
            return 2
        report = parse_critic_report(data)
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["ok"] else 1
    if cmd == "adjudicate":
        reviewer_path = _flag(args, "--reviewer")
        critic_path = _flag(args, "--critic")
        if not reviewer_path or not critic_path:
            return 2
        rev_data, rev_err = _load_text(reviewer_path)
        crit_data, crit_err = _load_text(critic_path)
        if rev_err or crit_err:
            return 2
        prompt, perr = _prompt_text(args)
        if perr:
            return 2
        reviewer = parse_qc_report(rev_data, prompt=prompt)
        critic = parse_critic_report(crit_data)
        verdict = adjudicate_review(
            reviewer, critic, critic_enabled="--no-critic" not in args
        )
        ledger = _flag(args, "--ledger")
        if ledger:
            record_critic_verdict(
                ledger, verdict, reviewer_report=reviewer, critic_report=critic
            )
        print(json.dumps(verdict, ensure_ascii=False))
        # ``adjudicate`` is the path a caller actually decides on, so the distinct
        # non-measurement code has to be returned here too — otherwise an unscored review
        # exits 1, identical to a measured rework demand (PR #244 review, defect 2).
        if verdict.get("outcome") == "unscored":
            return EXIT_UNSCORED
        return 1 if verdict["block"] else 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
