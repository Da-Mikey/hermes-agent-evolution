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


def _diff_from_args(args: List[str]) -> str:
    """Contents of ``--diff-file``, or ``""`` when absent/unreadable (#267).

    A missing diff is not an error: it just leaves evidence verification
    ``unmeasured``, which keeps the legacy declared-grounding rule rather than blocking
    the run on a flag nobody passed yet.
    """
    path = _flag(args, "--diff-file")
    if not path:
        return ""
    data, err = _load_text(path)
    return "" if err else data


def _digest_from_args(args: List[str]) -> Dict[str, str]:
    """``{path: sha256}`` for every ``--digest PATH`` (repeatable, comma-separated).

    Computed here rather than passed by the model: a digest the critic supplied would be
    the same self-report problem in another costume.
    """
    digests: Dict[str, str] = {}
    raw_values: List[str] = []
    for i, arg in enumerate(args):
        if arg == "--digest" and i + 1 < len(args):
            raw_values.append(args[i + 1])
        elif arg.startswith("--digest="):
            raw_values.append(arg.split("=", 1)[1])
    for raw in raw_values:
        for part in raw.split(","):
            candidate = part.strip()
            if not candidate:
                continue
            p = Path(candidate)
            try:
                if not p.is_file():
                    continue
                digests[str(p)] = hashlib.sha256(p.read_bytes()).hexdigest()
            except OSError:
                continue
    return digests


_CRITIC_OUTPUT_FORMAT = (
    "## Output Format\nReturn JSON:\n"
    '- "dissent": bool — true ONLY if you found a concrete failure mode\n'
    '- "evidence_ref": string — the file:line (`tools/x.py:41`) or diff hunk the '
    "objection resolves to (empty when you do not dissent)\n"
    '- "grounded": bool — true only if your objection cites specific evidence\n'
    '- "failure_mode": string — the concrete failure mode (empty if none)\n'
    '- "agreement_reason": string — why you could NOT object (empty if dissenting)\n'
)

# A diff this large stops being evidence and starts being a context problem.
_MAX_CRITIC_DIFF_CHARS = 12000


def _clip(text: str, limit: int) -> str:
    text = _s(text)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n… (diff truncated for the critic prompt)"


def _target_digest_block(target_digest: Optional[Dict[str, str]]) -> str:
    """The ``## Target Digests`` section, or nothing when no digests were supplied."""
    if not target_digest:
        return ""
    rows = "\n".join(
        f"  - {path} sha256:{digest}" for path, digest in sorted(target_digest.items())
    )
    return f"## Target Digests\n{rows}\n\n"


def build_critic_task(
    summary: Optional[str],
    files: List[str],
    *,
    issue_number: int = 0,
    toolsets: Optional[List[str]] = None,
    diff: str = "",
    trace_ref: str = "",
    target_digest: Optional[Dict[str, str]] = None,
    claim_blind: bool = False,
) -> Dict[str, Any]:
    """Build a leaf-subagent adversarial critic task for delegate_task (#92).

    Runs AFTER the reviewer has approved a diff: the critic's only job is to
    try to prove the change WRONG with one evidence-grounded, concrete failure
    mode. If it cannot construct such an objection, it must say so explicitly
    — the review then proceeds. This is the structured-disagreement pass that
    breaks reviewer/coder rubber-stamping (Adversarial Review, arXiv:2608.18167).

    ``claim_blind=True`` (#267) withholds the worker's own success summary. Self-report
    leakage is the defect: a judge handed the author's claim as its evidence grades the
    claim, not the change — withholding it lifted dangerous-misreport recall by 43.4pp on
    the same judge reading the same trace (arXiv:2610.29609). The blind pass therefore
    gets the *artefact* (diff, trace reference, target digests, file list) and never the
    claim. ``claim_blind=False`` keeps the legacy claim-aware prompt so both passes can be
    run over one artefact and their disagreement scored (``--claim-blind``).
    """
    file_list = "\n".join(f"  - {f}" for f in files) if files else "  (no files listed)"
    if claim_blind:
        framing = (
            "You are an adversarial code critic. A change is under review. You have "
            "NOT been told what its author claims it does: the diff, the tool-trace "
            "reference and the file list are your only account of it.\n\n"
            "Attempt to articulate ONE concrete, evidence-grounded failure mode: a "
            "specific way this change breaks, regresses, or introduces a bug, tied "
            "to the diff or a file below. Cite it in `evidence_ref`. Do NOT "
            "manufacture objections to seem rigorous — a fabricated disagreement is "
            "as harmful as rubber-stamped approval.\n\n"
        )
        evidence = (
            "## Tool Trace Reference\n"
            f"{trace_ref or '  (no trace reference supplied)'}\n\n"
            f"## Diff Under Review\n{_clip(diff, _MAX_CRITIC_DIFF_CHARS) or '  (no diff supplied)'}\n\n"
            f"{_target_digest_block(target_digest)}"
            f"## Files Changed\n{file_list}\n\n"
        )
    else:
        framing = (
            "You are an adversarial code critic. The reviewer has APPROVED the "
            "following implementation. Your job is to try to prove it WRONG.\n\n"
            "Attempt to articulate ONE concrete, evidence-grounded failure mode: a "
            "specific way this change breaks, regresses, or introduces a bug, tied "
            "to the summary or a file below. Do NOT manufacture objections to seem "
            "rigorous — a fabricated disagreement is as harmful as rubber-stamped "
            "approval.\n\n"
        )
        evidence = (
            "## Tool Trace Reference\n"
            f"{trace_ref or '  (no trace reference supplied)'}\n\n"
            f"## Diff Under Review\n{_clip(diff, _MAX_CRITIC_DIFF_CHARS) or '  (no diff supplied)'}\n\n"
            f"{_target_digest_block(target_digest)}"
            f"## Summary\n{_s(summary)}\n\n"
            f"## Files Changed\n{file_list}\n\n"
        )
    goal = framing + evidence + _CRITIC_OUTPUT_FORMAT
    context = (
        f"You are the adversarial critic gate. Issue #{issue_number}. "
        "Only dissent when you can point to a concrete, evidence-grounded "
        "failure mode; otherwise agree explicitly and explain why. "
        "Your `evidence_ref` must resolve to a file:line or a diff hunk."
    )
    return {
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets is not None else ["file", "search"],
        "role": "leaf",
        "claim_blind": bool(claim_blind),
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
            "evidence_ref": "",
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
        "evidence_ref": _s(raw.get("evidence_ref")),
        "failure_mode": failure_mode,
        "agreement_reason": agreement_reason,
        "ok": ok,
    }


# ── Evidence verification: `grounded` checked, not declared (#267) ─────────
# The critic used to declare its own grounding and the gate believed it, so the only
# thing standing between a leaky success claim and a merged PR was the model's assertion
# that it had evidence. These helpers make grounding a property of the *reference*: an
# objection counts only when its `evidence_ref` resolves to a changed hunk in the diff or
# to a file attested at a known target digest. Unresolvable evidence does not block.
GROUNDING_VERIFIED = "verified"
GROUNDING_UNGROUNDED = "ungrounded"
GROUNDING_UNMEASURED = "unmeasured"

_EVIDENCE_REF_RE = re.compile(
    r"(?<![\w/.-])([\w./\\-]+\.[A-Za-z0-9_]+)(?::L?(\d+)(?:\s*[-–]\s*L?(\d+))?)?"
)


def _diff_line_ranges(diff: str) -> Dict[str, List[Tuple[int, int]]]:
    """``{new_side_path: [(start, end), ...]}`` for every hunk in a unified diff.

    New-side numbering is what a critic reading a file after the change will cite, so the
    ranges are the ``+`` side of each hunk header. A pure-delete hunk has a zero-length new
    side; it is still recorded (as a one-line window) so a critique of a deletion resolves.
    """
    ranges: Dict[str, List[Tuple[int, int]]] = {}
    current: Optional[str] = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(\S+) b/(\S+)", line)
            current = m.group(2) if m else None
            continue
        if line.startswith("+++ "):
            rest = line[4:].strip()
            if rest.startswith("b/"):
                current = rest[2:]
            elif rest and rest != "/dev/null":
                current = rest
            continue
        if line.startswith("@@") and current:
            m = re.match(r"@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start = int(m.group(2))
                count = int(m.group(3) or 1)
                ranges.setdefault(current, []).append((
                    start,
                    start + max(count, 1) - 1,
                ))
    return ranges


def _match_known_path(path: str, known: Any) -> Optional[str]:
    """Resolve a critic-written path against known paths (exact, then suffix)."""
    if path in known:
        return path
    for candidate in known:
        if candidate.endswith("/" + path) or path.endswith("/" + candidate):
            return candidate
    return None


def verify_evidence_ref(
    evidence_ref: str,
    *,
    diff: str = "",
    target_digest: Optional[Dict[str, str]] = None,
) -> Tuple[str, str]:
    """Does ``evidence_ref`` actually resolve? ``(status, detail)``.

    Status is one of ``verified`` (it names a changed hunk or a digest-attested file),
    ``ungrounded`` (verification material existed and the reference does not resolve —
    the objection does not block) or ``unmeasured`` (no diff and no digests were supplied,
    so nothing could be checked and the caller keeps the legacy behaviour).
    """
    ref = _s(evidence_ref).strip()
    digests = target_digest or {}
    if not ref:
        if not diff and not digests:
            return GROUNDING_UNMEASURED, "no evidence_ref and no verification material"
        return GROUNDING_UNGROUNDED, "no evidence_ref supplied"
    m = _EVIDENCE_REF_RE.search(ref)
    if not m:
        if not diff and not digests:
            return GROUNDING_UNMEASURED, "no verification material supplied"
        return GROUNDING_UNGROUNDED, f"evidence_ref names no file or hunk: {ref[:120]}"
    path = m.group(1).lstrip("./")
    line = int(m.group(2)) if m.group(2) else None
    ranges = _diff_line_ranges(diff) if diff else {}
    attested = _match_known_path(path, digests)
    if attested is not None:
        return GROUNDING_VERIFIED, f"{attested} is attested at its target digest"
    in_diff = _match_known_path(path, ranges)
    if in_diff is not None:
        spans = ranges[in_diff]
        if line is None:
            return GROUNDING_VERIFIED, f"{in_diff} appears in the diff"
        end = int(m.group(3)) if m.group(3) else line
        for start, stop in spans:
            if line >= start and end <= max(stop, start):
                return GROUNDING_VERIFIED, f"{in_diff}:{line} falls in a changed hunk"
        return (
            GROUNDING_UNGROUNDED,
            f"{in_diff}:{line} is outside every changed hunk in the diff",
        )
    if not diff and not digests:
        return GROUNDING_UNMEASURED, "no verification material supplied"
    return GROUNDING_UNGROUNDED, f"{path} is neither in the diff nor a target digest"


def verify_critic_grounding(
    critic_report: Dict[str, Any],
    *,
    diff: str = "",
    target_digest: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Turn a critic report's self-declared ``grounded`` into a checked verdict (#267).

    ``grounded_verified`` is true only when the critic dissented, claimed grounding, AND
    its reference resolves. ``grounding_status == "unmeasured"`` means the caller supplied
    no diff/digest, so the old declared-grounding behaviour is preserved rather than
    silently widening the gate on absent evidence.
    """
    dissent = bool(critic_report.get("dissent"))
    declared = bool(critic_report.get("grounded"))
    ref = _s(critic_report.get("evidence_ref"))
    # #267 rework: the gate dict is the ONLY thing ``adjudicate_review`` sees, so the
    # critic's stated ``failure_mode`` must travel with it. It was omitted here, which
    # structurally guaranteed an empty reason (``... : ``) for the aware pass and left a
    # blocking gate a human could not read.
    fm = _s(critic_report.get("failure_mode"))
    if not dissent:
        return {
            "dissent": False,
            "declared_grounded": declared,
            "grounded_verified": False,
            "grounding_status": "n/a",
            "evidence_ref": ref,
            "evidence_detail": "",
            "failure_mode": "",
        }
    status, detail = verify_evidence_ref(ref, diff=diff, target_digest=target_digest)
    return {
        "dissent": True,
        "declared_grounded": declared,
        "grounded_verified": declared and status == GROUNDING_VERIFIED,
        "grounding_status": status,
        "evidence_ref": ref,
        "evidence_detail": detail,
        "failure_mode": fm,
    }


def adjudicate_review(
    reviewer_report: Dict[str, Any],
    critic_report: Dict[str, Any],
    *,
    critic_enabled: bool = True,
    diff: str = "",
    target_digest: Optional[Dict[str, str]] = None,
    blind_critic_report: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Deterministic reviewer-to-critic gate (#92).

    Returns ``{"block", "verdict", "reason"}``:
    - reviewer not ok → block ("rework"), regardless of critic.
    - reviewer ok + critic disabled → proceed.
    - reviewer ok + grounded dissent → block ("rework").
    - reviewer ok + no grounded objection → proceed.

    Grounding is *verified*, not declared (#267): a dissent blocks only when its
    ``evidence_ref`` resolves against ``diff``/``target_digest``. When neither is supplied
    the check is ``unmeasured`` and the old declared-grounding rule stands, so an
    unchanged caller never has its gate silently widened. ``blind_critic_report`` is the
    claim-blind pass over the same artefact: supplying it makes the gate block if EITHER
    pass blocks, and records whether the two disagreed (``blind_aware_disagreement``) —
    a disagreement is the signal that the aware pass was reading the claim, not the code.
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

    def _blocks(gate: Dict[str, Any]) -> bool:
        """One pass blocks when it dissented AND its grounding survived checking."""
        if not gate.get("dissent"):
            return False
        if gate.get("grounding_status") == GROUNDING_UNMEASURED:
            return bool(gate.get("declared_grounded"))
        return bool(gate.get("grounded_verified"))

    gates: Dict[str, Dict[str, Any]] = {
        "aware": verify_critic_grounding(
            critic_report, diff=diff, target_digest=target_digest
        )
    }
    if blind_critic_report is not None:
        # #267: the claim-blind pass over the same artefact. Both are run and both are
        # recorded; the gate blocks if either does, because withholding a claim should
        # only ever add objections, never remove one already found.
        gates["blind"] = verify_critic_grounding(
            blind_critic_report, diff=diff, target_digest=target_digest
        )
    blocking_passes = [name for name, gate in gates.items() if _blocks(gate)]
    disagreement = len(gates) == 2 and bool(_blocks(gates["aware"])) != bool(
        _blocks(gates["blind"])
    )
    if blocking_passes:
        primary = gates[blocking_passes[0]]
        # The gate carries the critic's stated failure_mode (threaded through
        # verify_critic_grounding). Fall back to the source report as well, for BOTH
        # passes: the reason must never render as an empty colon for whichever pass
        # blocked — a gate a human cannot read is a gate they cannot act on (#267).
        sources = {"aware": critic_report, "blind": blind_critic_report}
        fm = _s(primary.get("failure_mode")) or _s(
            (sources.get(blocking_passes[0]) or {}).get("failure_mode")
        )
        status = primary.get("grounding_status")
        evidence = (
            f" [{blocking_passes[0]} pass, evidence {status}: {primary.get('evidence_detail', '')[:120]}]"
            if status in (GROUNDING_VERIFIED, GROUNDING_UNMEASURED)
            else f" [{blocking_passes[0]} pass]"
        )
        verdict = {
            "block": True,
            "outcome": "fail",
            "verdict": "rework",
            "reason": f"critic grounded objection{evidence}: {fm[:200]}",
            "grounding": {name: gate for name, gate in gates.items()},
            "blocking_passes": blocking_passes,
        }
        if disagreement:
            verdict["blind_aware_disagreement"] = True
        return verdict
    verdict = {
        "block": False,
        "outcome": "pass",
        "verdict": "proceed",
        "reason": "no grounded objection from critic",
        "grounding": {name: gate for name, gate in gates.items()},
        "blocking_passes": [],
    }
    if disagreement:
        verdict["blind_aware_disagreement"] = True
    return verdict


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

        # #257: carry the three-valued outcome into the ledger. Flattening to
        # ``rework``/``proceed`` alone logs an *unscored* block — a reviewer that
        # produced no measurable verdict at all — as a measured rework, which is the
        # exact conflation #234 removed upstream. ``verdict`` keeps the legacy
        # vocabulary for existing consumers; ``outcome`` makes the two cases
        # distinguishable in the append-only ledger.
        legacy_verdict = "rework" if verdict.get("block") else "proceed"
        outcome = _s(verdict.get("outcome"))
        if outcome not in _OUTCOMES:
            # Unknown/absent outcome: derive two-valued rather than leak junk into the
            # append-only ledger, which nothing downstream can repair.
            outcome = "fail" if legacy_verdict == "rework" else "pass"
        record_review_critic_verdict(
            Path(ledger_file),
            verdict=legacy_verdict,
            outcome=outcome,
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
        claim_blind = "--claim-blind" in args
        # A claim-blind pass takes no summary at all — that is the point of it (#267).
        if not _s(summary) and not claim_blind:
            return 2
        files = _files_from_args(args)
        try:
            issue_num = int(_flag(args, "--issue") or "0")
        except ValueError:
            issue_num = 0
        task = build_critic_task(
            summary or None,
            files,
            issue_number=issue_num,
            diff=_diff_from_args(args),
            trace_ref=_flag(args, "--trace-ref") or "",
            target_digest=_digest_from_args(args),
            claim_blind=claim_blind,
        )
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
        blind_path = _flag(args, "--blind-critic")
        blind_critic = None
        if blind_path:
            blind_data, blind_err = _load_text(blind_path)
            if blind_err:
                return 2
            blind_critic = parse_critic_report(blind_data)
        verdict = adjudicate_review(
            reviewer,
            critic,
            critic_enabled="--no-critic" not in args,
            diff=_diff_from_args(args),
            target_digest=_digest_from_args(args),
            blind_critic_report=blind_critic,
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
