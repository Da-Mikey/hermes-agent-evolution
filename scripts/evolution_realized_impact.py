#!/usr/bin/env python3
"""Realized-impact feedback — did a MERGED change actually help, or was it blind?

The funnel/metrics measure the pipeline up to the merge (proposed -> merged) and
its calibration (selection_efficiency = merged/selected). They do NOT answer the
question that closes the loop: *of what we merged, what actually delivered real
value?* Without that, evolution is blind — the agent optimizes a PREDICTED impact
it never checks against reality, and can keep shipping plausible-but-useless code.

This is the measurement spine for that loop. It is a LIGHT, deterministic
aggregator: the agent (integration + introspection skills) writes a ledger; this
script reads it and reports the realized-impact rate + flags. It does no judging
itself (no creds, no LLM) — the verdicts come from the agent verifying real
sessions; this turns those verdicts into an evidence signal the pipeline acts on.

Ledger: ``<evolution_dir>/realized/ledger.jsonl`` — one JSON object per line.
  * at merge (integration):   {"issue", "merged_at": "YYYY-MM-DD",
                               "predicted_impact": <0..1>, "target": "<one line>"}
  * at verification later (introspection):
                               {"issue", "verdict": "confirmed|no-signal|regressed",
                               "verified_at": "YYYY-MM-DD", "note": "<one line>"}
Lines with the same ``issue`` are folded (latest verdict wins; merge metadata
kept). A change is "matured" once ``maturity_days`` have passed since merge — only
then is the absence of a verdict a problem (the verification step didn't run).

Gate semantics (#320). ``check-close`` fails CLOSED: an issue with no merge
record never closes, because a missing record means nothing was measured — not
that the fix worked. ``reconcile-merges <date>`` checks the ledger against the
integration stage's ``merged`` list and exits 1 when a merged PR has no record.
A rate is only compared with one measured on the same basis
(``failure_rate_basis``); an unknown or mismatched basis disables the
rate-normalization override and the recorded verdict stands.

Pure functions + explicit IO so it is import-safe and unit-testable.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

VERDICTS_GOOD = {"confirmed"}
# ``VERDICTS_BAD`` is a real regression — the change made things worse and
# counts against ``realized_rate``. ``VERDICTS_NEUTRAL`` is a no-signal:
# unmeasurable in the session tool-failure digest (e.g. structural changes
# like skill provenance / compaction wiring). No-signal is NOT evidence the
# change failed — only evidence we could not observe its effect — so it is
# excluded from the realized-rate denominator to avoid depressing the rate
# with structural work. Both still block ``should_close_issue`` (the fix has
# not been positively confirmed for that specific issue).
VERDICTS_BAD = {"regressed"}
VERDICTS_NEUTRAL = {"no-signal"}
VERDICTS_ANY = VERDICTS_GOOD | VERDICTS_BAD | VERDICTS_NEUTRAL


def load_ledger(ledger_file: Path) -> List[Dict[str, Any]]:
    """Read the realized-impact ledger (one JSON object per line), oldest-first.

    Folds multiple lines for the same ``issue`` into one record: merge metadata
    from the first sighting, latest verdict/verified_at/note from the last.
    Malformed lines are skipped (the ledger must never crash the pipeline).
    """
    if not ledger_file.exists():
        return []
    folded: Dict[Any, Dict[str, Any]] = {}
    order: List[Any] = []
    for ln in ledger_file.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(rec, dict) or "issue" not in rec:
            continue
        key = rec["issue"]
        if key not in folded:
            folded[key] = {}
            order.append(key)
        # Later lines override earlier keys for this issue (verdict supersedes).
        for k, v in rec.items():
            if v is not None:
                folded[key][k] = v
    return [folded[k] for k in order]


def append_ledger_record(ledger_file: Path, record: Dict[str, Any]) -> None:
    """Append a single JSON line to the ledger atomically.

    Creates parent directories if missing. Malformed records are rejected with
    ValueError so callers cannot silently write invalid ledger lines.
    """
    if not isinstance(record, dict) or "issue" not in record:
        raise ValueError("ledger record must be a dict with an 'issue' key")
    ledger_file.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True)
    with open(ledger_file, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def record_merge(
    ledger_file: Path,
    issue: int,
    merged_at: str,
    predicted_impact: float,
    target: str,
    baseline_failure_rate: Optional[float] = None,
    baseline_failure_rate_by_reason: Optional[Dict[str, float]] = None,
    baseline_failure_rate_basis: Optional[str] = None,
) -> None:
    """Record a merge event in the realized-impact ledger.

    ``baseline_failure_rate`` (failures/session at merge time) is stored so later
    verdicts compare apples-to-apples rates, not raw counts that grow with
    session volume (#1324). Omitted/None keeps legacy records working.

    ``baseline_failure_rate_by_reason`` (#1325) is a finer-grained snapshot:
    ``{reason: failures/session}`` for the specific reason the merged change
    targets. The aggregate tool rate is dominated by unaffected reasons, so a
    reason-scoped baseline lets the verdict detect the targeted mode's drop.
    """
    rec: Dict[str, Any] = {
        "issue": issue,
        "merged_at": merged_at,
        "predicted_impact": predicted_impact,
        "target": target,
    }
    if baseline_failure_rate is not None:
        rec["baseline_failure_rate"] = baseline_failure_rate
    if baseline_failure_rate_by_reason:
        rec["baseline_failure_rate_by_reason"] = baseline_failure_rate_by_reason
    if baseline_failure_rate_basis:
        # Which denominator the baseline rates were measured over (#320).
        # ``should_close_issue`` refuses to compare against a current rate on a
        # different basis, so this must travel with the numbers it describes.
        rec["baseline_failure_rate_basis"] = baseline_failure_rate_basis
    append_ledger_record(ledger_file, rec)


def record_verdict(
    ledger_file: Path,
    issue: int,
    verdict: str,
    verified_at: str,
    note: str,
) -> None:
    """Record a post-merge verification verdict in the ledger."""
    if verdict not in VERDICTS_ANY:
        raise ValueError(f"verdict must be one of {VERDICTS_ANY}")
    append_ledger_record(
        ledger_file,
        {
            "issue": issue,
            "verdict": verdict,
            "verified_at": verified_at,
            "note": note,
        },
    )


def classify_verdict_by_rate(
    baseline_rate: Optional[float],
    current_rate: Optional[float],
    regression_threshold: float = 0.20,
) -> str:
    """Rate-normalized verdict so growing session volume can't fake a regression.

    Returns ``"regressed"`` only if the per-session failure rate rose by more
    than ``regression_threshold`` (default 20%) over the baseline — NOT merely
    because the raw count grew with the session denominator (#1324). Returns
    ``"confirmed"`` when the rate fell or held, and ``"no-signal"`` when either
    rate is missing (caller must still record a verdict; it just isn't forced to
    ``regressed`` by volume alone).
    """
    if baseline_rate is None or current_rate is None:
        return "no-signal"
    # Relative change vs baseline; treat a zero baseline as "improved if not up".
    if baseline_rate <= 0:
        return "confirmed" if current_rate <= 0 else "regressed"
    rel_delta = (current_rate - baseline_rate) / baseline_rate
    return "regressed" if rel_delta > regression_threshold else "confirmed"


# ── Post-merge signal-verification gate (#1140) ────────────────────────────
# Recovered from PR #1148 (closed unmerged — the functions were defined but
# never called). This module now also exposes a ``check-close`` CLI subcommand
# (see main()) so the introspection skill can invoke the gate right after
# recording a verdict and act on it — that wiring makes the gate live, not
# dead code. A flat/rising signal post-merge keeps the issue open for a
# focused regression rather than silently closing it.


def signal_verified(records: List[Dict[str, Any]], issue: int) -> bool:
    """Whether a merged issue has a CONFIRMED signal-drop verdict.

    Returns True only if the issue has a merge record AND a verdict of
    "confirmed" (the signal dropped post-merge). Returns False if no merge
    record exists, no verdict has been recorded yet, or the verdict is
    "no-signal" / "regressed" (the fix didn't help).
    """
    for rec in reversed(records):
        if rec.get("issue") == issue:
            return rec.get("verdict") in VERDICTS_GOOD
    return False


def _rate_basis_problem(
    baseline_basis: Optional[str], current_basis: Optional[str]
) -> Optional[str]:
    """Why two failure rates may NOT be compared, or None when they share a basis.

    ``None`` (unknown) is a problem, not a wildcard. A merge record stores a
    failures/SESSION baseline; the post-#318 introspection digest reports
    per-tool-CALL rates. A delta across two bases has no meaning, so the close
    gate refuses to compute one rather than re-checking a verdict on it (#320).
    """
    if not baseline_basis:
        return (
            "the merge record declares no failure-rate basis, so its baseline "
            "cannot be matched to the current rate"
        )
    if not current_basis:
        return "the current rate declares no basis"
    if baseline_basis != current_basis:
        return (
            f"basis mismatch: baseline {baseline_basis!r} vs current {current_basis!r}"
        )
    return None


def should_close_issue(
    records: List[Dict[str, Any]],
    issue: int,
    today: str,
    maturity_days: int = 5,
    current_failure_rate: Optional[float] = None,
    current_failure_rate_by_reason: Optional[Dict[str, float]] = None,
    regression_threshold: float = 0.20,
    current_failure_rate_basis: Optional[str] = None,
) -> tuple:
    """Gate for closing an issue after a fix PR merges.

    Returns ``(should_close, reason)``. The issue should close only when:
    - ``signal_verified`` returns True (the signal dropped), OR
    - the merge is older than ``maturity_days`` AND no verdict exists (the
      verification step didn't run — close with a note that it was never
      verified, rather than leaving it open indefinitely).

    An issue with NO merge record at all never closes: absence of a record is
    absence of evidence, not a passing signal (#320).

    If the verdict is "no-signal" or "regressed", returns ``(False, reason)``
    so the issue stays open for a focused regression — this is the core fix
    for the REALIZED_IMPACT_LOW failure loop.

    Rate normalization (#1324): when a ``current_failure_rate`` is supplied and
    the merge record stored a ``baseline_failure_rate``, a recorded ``regressed``
    verdict is re-checked against the *rate* delta — if the rate did NOT rise
    beyond ``regression_threshold``, the raw-count regression is treated as a
    false alarm and the issue may close. This stops growing session volume from
    forcing false HOLDs.

    Reason-scoped rate (#1325): when the merge record stored a
    ``baseline_failure_rate_by_reason`` and a ``current_failure_rate_by_reason``
    is supplied, each targeted reason is compared against its own baseline.
    An aggregate tool rate is dominated by unaffected reasons, so a fix that
    dropped one reason shows flat and re-opened the issue; reason-scoped
    comparison sees the real drop. Verdicts are ANDed — every targeted reason
    must improve to clear the gate; any flat/regressed reason keeps it open.
    """
    rec = None
    for r in reversed(records):
        if r.get("issue") == issue:
            rec = r
            break

    if rec is None:
        # Fail CLOSED (#320). Absence of a merge record is not evidence the fix
        # worked — it is evidence nothing was measured. This branch used to
        # return True, and on a host whose ledger held no issue-keyed lines it
        # closed EVERY issue, including one whose fix had merged the day before,
        # because the integration stage writes its change records into the same
        # path in a schema with no ``issue`` key (``load_ledger`` drops those).
        # The merge record is created by ``record-merge``; when it is missing the
        # gate must say so instead of reading the gap as permission.
        return (
            False,
            "no merge record in the realized ledger for this issue, so there is "
            "no signal to verify. Record the merge first "
            "(record-merge <issue> <date> <predicted_impact> <target>), then "
            "verify the signal.",
        )

    verdict = rec.get("verdict")
    if verdict in VERDICTS_GOOD:
        return True, "signal confirmed dropped post-merge"

    # Reason-scoped override (#1325): isolates the targeted failure mode by
    # comparing each reason against its own baseline. Runs before the aggregate
    # branch. A fix is credited only when EVERY targeted reason's rate DROPPED by
    # at least ``regression_threshold``; flat = no-signal = block (not "held").
    baseline_by_reason = rec.get("baseline_failure_rate_by_reason")
    if (
        verdict in (VERDICTS_BAD | VERDICTS_NEUTRAL)
        and baseline_by_reason
        and current_failure_rate_by_reason
    ):
        all_improved = True
        details = []
        for reason, base in baseline_by_reason.items():
            cur = current_failure_rate_by_reason.get(reason)
            if cur is None:
                all_improved = False
                details.append(f"{reason}: current rate missing")
                continue
            if base > 0:
                rel = (cur - base) / base
                improved = rel <= -regression_threshold
            else:
                # Baseline was already zero — nothing to fix; a new nonzero
                # rate is a regression for this reason.
                rel = 0.0
                improved = cur <= 0
            details.append(f"{reason} {cur:.4f} vs {base:.4f} (Δ{rel:+.0%})")
            if not improved:
                all_improved = False
        if all_improved:
            return (
                True,
                "verdict re-checked by reason-scoped rate (#1325): "
                + "; ".join(details)
                + " — every targeted reason dropped",
            )
        return (
            False,
            "reason-scoped: fix did not reduce every targeted reason (#1325): "
            + "; ".join(details),
        )

    # Rate-normalization override: a "regressed" verdict from a raw-count
    # comparison is a false alarm if the per-session rate held or fell (#1324).
    baseline_rate = rec.get("baseline_failure_rate")
    if (
        verdict == "regressed"
        and current_failure_rate is not None
        and baseline_rate is not None
    ):
        # Basis guard (#320). Two rates may only be compared when they share a
        # basis. The merge record stores failures/session; after #318 the
        # introspection digest publishes per-tool-CALL rates and stamps
        # ``failure_rate_basis: "per_tool_call"``. A delta across bases has no
        # meaning, and here a meaningless delta CLEARS a regression — so an
        # unknown or differing basis disables the override and the recorded
        # verdict stands (fail closed).
        problem = _rate_basis_problem(
            rec.get("baseline_failure_rate_basis"), current_failure_rate_basis
        )
        if problem:
            return (
                False,
                f"rates NOT comparable — {problem}; keeping the recorded "
                f"verdict ({verdict}) instead of re-checking it on a different "
                f"basis (#320).",
            )
        rate_verdict = classify_verdict_by_rate(
            baseline_rate,
            current_failure_rate,
            regression_threshold=regression_threshold,
        )
        if rate_verdict != "regressed":
            return (
                True,
                "regressed verdict re-checked by rate: per-session failure rate "
                f"{current_failure_rate:.4f} vs baseline "
                f"{rec['baseline_failure_rate']:.4f} — volume-only increase, "
                "not a real regression (#1324)",
            )

    if verdict in (VERDICTS_BAD | VERDICTS_NEUTRAL):
        return (
            False,
            f"signal NOT verified — verdict: {verdict}. Keep open for regression.",
        )

    # No verdict yet — check maturity.
    days_since = _days_between(rec.get("merged_at"), today)
    if days_since is not None and days_since >= maturity_days:
        return True, f"matured {days_since}d without verification — closing unverified"

    return (
        False,
        f"awaiting signal verification ({days_since or 0}d since merge, maturity={maturity_days}d)",
    )


def _days_between(a: Optional[str], b: Optional[str]) -> Optional[int]:
    """Whole days from ISO date ``a`` to ISO date ``b`` (both YYYY-MM-DD)."""
    if not a or not b:
        return None
    try:
        from datetime import date

        ya, ma, da = (int(x) for x in str(a)[:10].split("-"))
        yb, mb, db = (int(x) for x in str(b)[:10].split("-"))
        return (date(yb, mb, db) - date(ya, ma, da)).days
    except (ValueError, TypeError):
        return None


def compute_realized(
    records: List[Dict[str, Any]],
    today: str,
    last: int = 30,
    maturity_days: int = 5,
    streak_k: int = 3,
) -> Dict[str, Any]:
    """Aggregate the ledger into a realized-impact signal.

    ``today`` is passed in (never read the clock here — keeps it deterministic
    and testable, same discipline as the rest of the evolution scripts).

    Window is over MATURE merges only (age >= ``maturity_days``). A too-new
    merge is neither help nor hurt — it's simply unknown. Including unknowns
    in the ``last``-N slice would push older verdicted merges out, so during
    dense merging the verdicted sample shrinks and rotates rapidly toward
    whatever verdict batch happened to stamp most recently — a lag artifact
    that made the rate crater from ~55% to 20% in five merges' time on real
    data. Filtering first gives a stable, apples-to-apples sample.
    """

    def _is_mature(r: Dict[str, Any]) -> bool:
        days = _days_between(r.get("merged_at"), today)
        return days is not None and days >= maturity_days

    mature = [r for r in records if _is_mature(r)]
    window = mature[-last:] if last and last > 0 else list(mature)

    # ``verdicted`` = the MEASURABLE sample for realized_rate: confirmed +
    # regressed only. ``no-signal`` is unmeasurable (structural changes that
    # do not show up in the session tool-failure digest — skill provenance,
    # compaction wiring, etc.) and would falsely depress the rate if kept in
    # the denominator, so it is tracked separately as ``unverifiable``.
    verdicted = [
        r for r in window if r.get("verdict") in (VERDICTS_GOOD | VERDICTS_BAD)
    ]
    confirmed = [r for r in verdicted if r.get("verdict") in VERDICTS_GOOD]
    unverifiable = [r for r in window if r.get("verdict") in VERDICTS_NEUTRAL]

    # Every window entry is mature by construction, so any without ANY verdict
    # is matured-but-unverified — the verification step never recorded one.
    matured_unverified = [r for r in window if r.get("verdict") not in VERDICTS_ANY]

    realized_rate = (len(confirmed) / len(verdicted)) if verdicted else None

    # Consecutive-miss streak over the most recent VERDICTED changes only.
    # A ``no-signal`` breaks the streak (it's not evidence of failure) — we
    # iterate over records that carry ANY verdict and stop at anything other
    # than a real regression.
    verdict_records = [r for r in window if r.get("verdict") in VERDICTS_ANY]
    streak = 0
    for r in reversed(verdict_records):
        if r.get("verdict") in VERDICTS_BAD:
            streak += 1
        else:
            break

    flags: List[str] = []
    if len(verdicted) >= streak_k and streak >= streak_k:
        flags.append(
            f"REALIZED_IMPACT_LOW: last {streak} merged changes delivered no real "
            "value — shift to consolidation/refactor and require owner sign-off "
            "before new features"
        )
    if len(verdicted) >= 3 and realized_rate is not None and realized_rate < 0.5:
        flags.append(
            "REALIZED_RATE_LOW: <50% of measured merges actually helped — predicted "
            "impact is over-optimistic; raise the bar and recalibrate"
        )
    if len(matured_unverified) >= streak_k:
        flags.append(
            f"UNVERIFIED_BACKLOG: {len(matured_unverified)} matured merges never "
            "verified — the post-merge verification step is not running"
        )

    return {
        "merged_tracked": len(window),
        "verified": len(verdicted),
        "confirmed": len(confirmed),
        "unverifiable": len(unverifiable),
        "matured_unverified": len(matured_unverified),
        "realized_impact_rate": round(realized_rate, 3)
        if realized_rate is not None
        else None,
        "miss_streak": streak,
        "flags": flags,
    }


def _pct(x: Optional[float]) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def format_realized(h: Dict[str, Any]) -> str:
    tail = " | ".join(h["flags"]) if h["flags"] else "healthy"
    return (
        f"[evolution-realized-impact] tracked={h['merged_tracked']} "
        f"verified={h['verified']} confirmed={h['confirmed']} "
        f"unverifiable={h.get('unverifiable', 0)} "
        f"realized_rate={_pct(h['realized_impact_rate'])} "
        f"miss_streak={h['miss_streak']} unverified_matured={h['matured_unverified']} | {tail}"
    )


def _evolution_dir() -> Path:
    return Path(
        os.environ.get(
            "EVOLUTION_PROFILE_DIR",
            str(Path.home() / ".hermes" / "evolution"),
        )
    )


def _flag_value(args: List[str], flag: str) -> Optional[str]:
    """Value following ``flag`` in ``args``, or None (missing flag/value)."""
    if flag not in args:
        return None
    i = args.index(flag)
    if i + 1 >= len(args):
        return None
    return args[i + 1]


def _foreign_ledger_lines(ledger_file: Path) -> int:
    """Count ledger lines that are not realized-impact records (#320).

    ``load_ledger`` drops any line without an ``issue`` key, which is correct —
    it must never crash the pipeline on a foreign line. But silently dropping
    them hid the fact that this path has TWO writers: the integration stage
    writes change records here (``change_id``/``commit``/``pr_number``) and they
    are invisible to every reader. Counting them makes the schema collision
    auditable instead of inferred.
    """
    if not ledger_file.exists():
        return 0
    foreign = 0
    for ln in ledger_file.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln)
        except (json.JSONDecodeError, ValueError):
            foreign += 1
            continue
        if not isinstance(rec, dict) or "issue" not in rec:
            foreign += 1
    return foreign


def main(argv: List[str]) -> int:
    evolution_dir = _evolution_dir()
    ledger_file = evolution_dir / "realized" / "ledger.jsonl"
    args = argv[1:]

    # Subcommand: record a merge line
    if args and args[0] == "record-merge":
        try:
            issue = int(args[1])
            merged_at = args[2]
            predicted = float(args[3])
            target = args[4]
        except (IndexError, ValueError):
            print(
                "usage: evolution_realized_impact.py record-merge "
                "<issue> <YYYY-MM-DD> <predicted_impact> <target> "
                "[baseline_failure_rate] [--basis <failures/session|per_tool_call>]",
                file=sys.stderr,
            )
            return 2
        baseline_rate = None
        # Optional 6th positional arg: baseline failures/session at merge (#1324).
        if len(args) > 5 and not args[5].startswith("--"):
            try:
                baseline_rate = float(args[5])
            except ValueError:
                baseline_rate = None
        # ``--basis`` names the denominator the baseline was measured over
        # (#320). Without it the close gate cannot re-check a verdict by rate,
        # because a per-session baseline is not comparable to the per-tool-call
        # rate the introspection digest now publishes.
        basis = _flag_value(args, "--basis")
        record_merge(
            ledger_file,
            issue,
            merged_at,
            predicted,
            target,
            baseline_rate,
            baseline_failure_rate_basis=basis,
        )
        print(f"[realized-impact] recorded merge for issue #{issue}")
        return 0

    # Subcommand: record a verdict line
    if args and args[0] == "record-verdict":
        try:
            issue = int(args[1])
            verdict = args[2]
            verified_at = args[3]
            note = args[4]
        except (IndexError, ValueError):
            print(
                "usage: evolution_realized_impact.py record-verdict "
                "<issue> <confirmed|no-signal|regressed> <YYYY-MM-DD> <note>",
                file=sys.stderr,
            )
            return 2
        record_verdict(ledger_file, issue, verdict, verified_at, note)
        print(f"[realized-impact] recorded verdict for issue #{issue}")
        return 0

    # Subcommand: check the close-loop gate for an issue (#1140).
    # Invoked by the introspection skill right after record-verdict so the
    # close loop consults the realized-impact signal before allowing the
    # issue to be marked resolved. Prints ``CLOSE`` or ``HOLD`` + the reason
    # and exits 0 when the issue may close, 1 when it must stay open.
    if args and args[0] == "check-close":
        try:
            issue = int(args[1])
            today = args[2] if len(args) > 2 else None
        except (IndexError, ValueError):
            print(
                "usage: evolution_realized_impact.py check-close <issue> [<YYYY-MM-DD>]",
                file=sys.stderr,
            )
            return 2
        if not today:
            from datetime import datetime, timezone

            today = datetime.now(timezone.utc).date().isoformat()
        # Optional --current-rate <rate>: current per-session failure rate for the
        # rate-normalization override (#1324). Enables re-checking a "regressed"
        # verdict against the stored baseline rate instead of raw counts.
        current_rate = None
        if "--current-rate" in args:
            i = args.index("--current-rate")
            if i + 1 < len(args):
                try:
                    current_rate = float(args[i + 1])
                except ValueError:
                    current_rate = None
        records = load_ledger(ledger_file)
        should, reason = should_close_issue(
            records,
            issue,
            today,
            current_failure_rate=current_rate,
            current_failure_rate_basis=_flag_value(args, "--current-rate-basis"),
        )
        verdict = "CLOSE" if should else "HOLD"
        print(f"[realized-impact] #{issue} {verdict}: {reason}")
        return 0 if should else 1

    # Subcommand: reconcile the ledger against the integration stage (#320).
    # Every PR the integration stage lists under ``merged`` must have a ledger
    # line whose ``issue`` matches, or the close gate has nothing to read for it
    # and the merge goes unmeasured. Exit 1 lists the gaps.
    if args and args[0] == "reconcile-merges":
        date = args[1] if len(args) > 1 else None
        if not date:
            print(
                "usage: evolution_realized_impact.py reconcile-merges <YYYY-MM-DD>",
                file=sys.stderr,
            )
            return 2
        integ_file = evolution_dir / "integration" / f"{date}.json"
        if not integ_file.exists():
            print(
                f"[realized-impact] no integration record for {date} "
                f"({integ_file}) — nothing to reconcile"
            )
            return 0
        try:
            integ = json.loads(integ_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            print(f"[realized-impact] cannot read {integ_file}: {exc}", file=sys.stderr)
            return 2

        merged = integ.get("merged") or []
        recorded = set()
        for r in load_ledger(ledger_file):
            try:
                recorded.add(int(str(r.get("issue")).lstrip("#")))
            except (TypeError, ValueError):
                continue

        missing = []
        for entry in merged:
            if not isinstance(entry, dict):
                continue
            raw = entry.get("issue")
            if raw is None:
                continue
            try:
                num = int(str(raw).lstrip("#"))
            except (TypeError, ValueError):
                continue
            if num not in recorded:
                missing.append((num, entry.get("pr")))

        foreign = _foreign_ledger_lines(ledger_file)
        if foreign:
            # Not fatal: it is the integration stage's schema, not a lost merge
            # record. Surfaced so the two-writer collision is visible.
            print(
                f"[realized-impact] WARNING: {foreign} ledger line(s) carry no "
                f"'issue' key (foreign schema — ignored by every reader)",
                file=sys.stderr,
            )

        if missing:
            print(
                f"[realized-impact] RECONCILE FAIL {date}: {len(missing)} of "
                f"{len(merged)} merged PR(s) have NO merge record in the ledger: "
                + ", ".join(f"#{n} (PR {pr})" for n, pr in missing)
            )
            return 1
        print(
            f"[realized-impact] reconciled {date}: all {len(merged)} merged PR(s) "
            f"carry a merge record"
        )
        return 0

    last = 30
    if "--last" in args:
        i = args.index("--last")
        if i + 1 < len(args):
            try:
                last = int(args[i + 1])
            except ValueError:
                last = 30
    today = None
    if "--today" in args:
        i = args.index("--today")
        if i + 1 < len(args):
            today = args[i + 1]
    if not today:
        # The only clock read, and only in the CLI entrypoint (not the pure core).
        from datetime import date, timezone, datetime

        today = datetime.now(timezone.utc).date().isoformat()

    records = load_ledger(ledger_file)
    health = compute_realized(records, today=today, last=last)
    line = format_realized(health)
    print(line)

    if "--summary" in args:
        sidecar = evolution_dir / "realized-impact.txt"
        try:
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            sidecar.write_text(line + "\n", encoding="utf-8")
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
