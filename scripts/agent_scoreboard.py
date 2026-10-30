#!/usr/bin/env python3
"""Agent scoreboard — a fitness function for Prometheus, not for the pipeline.

WHY THIS EXISTS
---------------
The only scoreboard on this box judged the *evolution pipeline's internal stage
artifacts*, never the agent's own answers. Worse, it was not trustworthy: six of
eight daily reports read exactly 5.8% (3.0/52.0) - a deterministic fallback for
missing stages, not a measurement - and one day claimed 100% while flagging two
dimensions at 0.0/10.0 on the same line. A number that cannot be trusted cannot
gate anything.

So this scores the AGENT, and it scores it DETERMINISTICALLY. No LLM judge for
the headline number - that is the exact failure being replaced. (Cases that
genuinely need judgement can declare a "judge" block; they are reported
separately and never folded into the headline score.)

DESIGN RULES
------------
1. **Contracts, not snapshots.** A case never freezes a current value. Live-state
   cases declare a `ground_truth` command; the runner executes it at run time,
   extracts the expected token, and requires that token in the answer. Change the
   system and the case updates itself instead of going stale.
2. **A crash is a failure, not a skip.** Timeout, empty answer, or non-zero exit
   counts as a failed case. Silent skips are how a scoreboard lies.
3. **Cost is recorded.** Every case reports token cost, so the suite cannot
   quietly become expensive.
4. **Verdicts are auditable from the record alone.** A run record must let a
   later reader decide *why* a check passed or failed without re-running the
   model. So each result carries the full answer (bounded), and each check
   carries the pattern that matched plus the text it matched against. The
   ground-truth probe's own output is recorded too: a check that compares the
   answer to a live value is unfalsifiable unless the live value is in the
   record next to the answer.

USAGE
-----
  agent_scoreboard.py                      # run the whole suite
  agent_scoreboard.py --id live-ollama     # run one case
  agent_scoreboard.py --limit 3            # run the first N
  agent_scoreboard.py --no-history         # don't append to history.jsonl
  agent_scoreboard.py --list               # list cases, don't run

EXIT CODES
----------
  0  suite ran (whatever the score)
  1  setup problem (no cases, no `hermes` binary)
"""

import argparse
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone

HOME = os.path.expanduser("~")


def _hermes_bin():
    """Resolve the hermes CLI explicitly.

    Cron runs with a minimal PATH, so a bare "hermes" would raise
    FileNotFoundError there and the whole suite would report nothing.
    """
    found = shutil.which("hermes")
    if found:
        return found
    fallback = os.path.join(HOME, ".local", "bin", "hermes")
    return fallback if os.path.exists(fallback) else "hermes"


HERMES_BIN = _hermes_bin()
#: Overridable command prefix (argv list). Set by --hermes-cmd so a candidate
#: checkout (a git worktree) can be measured instead of the installed agent.
HERMES_CMD = [HERMES_BIN]
EVAL_DIR = os.path.join(HOME, ".hermes", "evals", "agent-scoreboard")
CASES = os.path.join(EVAL_DIR, "cases.json")
HISTORY = os.path.join(EVAL_DIR, "history.jsonl")
RUNS_DIR = os.path.join(EVAL_DIR, "runs")
USAGE_TMP = os.path.join(HOME, ".hermes", "cache", "scratch", "scoreboard-usage.json")

#: Record-size bounds (issue #319).  A run record is read by humans and by the
#: flip gate, so it must stay small enough to read and cheap enough to write 20
#: times a day -- but a verdict that cannot be re-derived from it is worse than
#: no verdict at all.  These caps are chosen per field rather than per record:
#:
#: * ``ANSWER_MAX_CHARS`` -- 10x the old 400-char head.  Chat answers are a few
#:   hundred to ~2k characters; 4000 holds a complete long answer (a table of
#:   cron jobs, a numbered list) while bounding the pathological stream case.
#: * ``MATCHED_TEXT_MAX_CHARS`` -- the matched substring itself.  A regex could
#:   in principle match a whole answer; 200 characters is far more than the
#:   sentence a human needs to adjudicate a disputed match.
#: * ``PROBE_RAW_MAX_CHARS`` -- the ground-truth command's stdout before token
#:   extraction.  2000 characters covers the widest live probe on this box
#:   (`systemctl`/`hermes cron list` style output) and truncation is marked.
ANSWER_MAX_CHARS = 4000
MATCHED_TEXT_MAX_CHARS = 200
PROBE_RAW_MAX_CHARS = 2000


# ---------------------------------------------------------------- run one case
def run_case(case, timeout):
    """Invoke the agent one-shot and return (answer, meta)."""
    prompt = case["prompt"]
    if os.path.exists(USAGE_TMP):
        try:
            os.remove(USAGE_TMP)
        except OSError:
            pass
    cmd = [*HERMES_CMD, "-z", prompt, "--usage-file", USAGE_TMP]
    # Give the child a PATH that can find its own tooling even under cron.
    env = dict(os.environ)
    env["PATH"] = env.get("PATH", "") + os.pathsep + os.path.join(HOME, ".local", "bin")
    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return None, {"error": f"timeout after {timeout}s", "seconds": time.time() - t0}
    elapsed = time.time() - t0
    meta = {"seconds": round(elapsed, 1), "exit_code": proc.returncode}
    if proc.returncode != 0:
        meta["error"] = f"exit {proc.returncode}: {proc.stderr.strip()[:200]}"
    if os.path.exists(USAGE_TMP):
        try:
            with open(USAGE_TMP, encoding="utf-8") as fh:
                u = json.load(fh)
            meta["cost_usd"] = u.get("estimated_cost_usd")
            meta["tokens"] = u.get("total_tokens")
            meta["model"] = u.get("model")
        except (OSError, ValueError):
            pass
    return proc.stdout.strip(), meta


# ------------------------------------------------------------- deterministic checks
#: English word forms for numbers, so a check that asks for a NUMBER does not
#: measure phrasing.  (Measured 2026-10-12: `live-paused-jobs` failed the correct
#: answer "Seven of your jobs show as paused" because the digit never appeared,
#: and the merge gate's flip check blocked PRs the candidates could not have
#: caused.  Same class in `fmt-tool-restraint`, which answered "Four." to
#: "What is 2 + 2?" and failed a `\b4\b` check - that probe flipped in 21 of 39
#: consecutive runs, i.e. it was coin-flipping on numerals-vs-words.)
_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = {
    20: "twenty",
    30: "thirty",
    40: "forty",
    50: "fifty",
    60: "sixty",
    70: "seventy",
    80: "eighty",
    90: "ninety",
}


def _word_forms(n):
    """English wordings of 0..9999, e.g. 'twenty-four', 'three hundred and ninety-one'."""
    if not 0 <= n <= 9999:
        return []
    if n < 20:
        return [_ONES[n]]
    if n < 100:
        tens, unit = divmod(n, 10)
        if not unit:
            return [_TENS[n]]
        return [
            "%s-%s" % (_TENS[tens * 10], _ONES[unit]),
            "%s %s" % (_TENS[tens * 10], _ONES[unit]),
        ]
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        base = "%s hundred" % _ONES[hundreds]
        if not rest:
            return [base]
        return ["%s and %s" % (base, w) for w in _word_forms(rest)] + [
            "%s %s" % (base, w) for w in _word_forms(rest)
        ]
    thousands, rest = divmod(n, 1000)
    base = "%s thousand" % _ONES[thousands]
    if not rest:
        return [base]
    return ["%s %s" % (base, w) for w in _word_forms(rest)]


def _word_pattern(word):
    """One wording as a regex, tolerant of hyphen-or-space ('twenty-four'|'twenty four')."""
    parts = re.split(r"[-\s]+", word.strip())
    return r"\b" + r"[\s-]+".join(re.escape(p) for p in parts) + r"\b"


def _number_patterns(raw):
    """Patterns that accept a numeral's VALUE however it is written.

    A pure-integer token matches as the numeral between word boundaries — so the
    ground truth 7 is not satisfied by '17', '70' or a commit hash like
    'ba86d422' — AND as its English word form ('seven'; 'twenty-four'; 'three
    hundred and ninety-one').  A non-numeric token yields nothing, so every other
    token keeps the previous behaviour exactly.
    """
    text = str(raw).strip()
    if not text.isdigit():
        return []
    n = int(text)
    out = [r"\b" + re.escape(str(n)) + r"\b"]
    out.extend(_word_pattern(w) for w in _word_forms(n))
    return out


def _tok(raw, alias):
    """A ground-truth token plus the answer wordings that legitimately mean it.

    Synonyms matter: a system can be correctly described as 'unpaired' or 'not
    paired'. A scoreboard that accepts only one wording measures vocabulary, not
    knowledge.  So does one that accepts only the numeral for a count, which is
    why a numeric token also carries its word form(s).
    """
    pats = alias.get(raw) or alias.get(raw.lower())
    if not pats:
        pats = _number_patterns(raw) or [re.escape(raw)]
    return {"raw": raw, "patterns": pats}


def _bounded(text, limit):
    """Clip *text* to *limit* characters, saying so when it had to.

    Returns ``(clipped, truncated)``. The marker is explicit rather than silent:
    a reader must be able to tell "the answer ended here" from "we stopped
    recording here", or a clipped record becomes its own false evidence.
    """
    if text is None:
        return "", False
    text = str(text)
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _search_any(patterns, text, flags):
    """First pattern that matches *text*, with the text it matched.

    Returns ``(ok, matched_pattern, matched_text)``.  Recording *which* pattern
    matched is the whole point (issue #319): a bare ``ok: true`` next to an
    answer that states a different number cannot be adjudicated, because it
    cannot distinguish a genuine standalone-token match from a substring inside
    a longer number ('11' inside '118').

    Patterns are tried in order, so the recorded pattern is the one that
    actually decided the check.
    """
    if text is None:
        return False, None, None
    for pattern in patterns:
        m = re.search(pattern, text, flags)
        if m is not None:
            matched, _ = _bounded(m.group(0), MATCHED_TEXT_MAX_CHARS)
            return True, pattern, matched
    return False, None, None


def ground_truth(case):
    """Execute a case's ground_truth command -> list of expected tokens.

    Returns (tokens, error, raw_stdout). Tokens are what the answer must contain.
    This is what keeps live-state cases from going stale.

    ``raw_stdout`` is the probe's own output before token extraction.  It is
    returned (not just consumed) so the run record can carry it: the expected
    token alone says what the answer had to contain, never what the live system
    actually said, and an audit of a disputed ground-truth verdict needs both.
    """
    gt = case.get("ground_truth")
    if not gt:
        return [], None, ""
    try:
        out = subprocess.run(
            gt["cmd"], shell=True, capture_output=True, text=True, timeout=60
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        return [], f"ground_truth command failed: {exc}", ""
    alias = gt.get("alias") or {}
    tokens = []
    pattern = gt.get("extract")
    if pattern:
        for m in re.finditer(pattern, out, re.M):
            tok = m.group(1) if m.groups() else m.group(0)
            if tok.strip():
                tokens.append(_tok(tok.strip(), alias))
    else:
        for line in out.splitlines():
            if line.strip():
                tokens.append(_tok(line.strip(), alias))
    if not tokens:
        return [], f"ground_truth produced no tokens (raw: {out.strip()[:120]!r})", out
    return tokens, None, out


#: Answers in which the runtime REFUSED to attempt the task rather than
#: answering it wrongly.  A refusal is not a wrong answer: the candidate was
#: never given a fair run, so the case is INCONCLUSIVE, never a failure
#: (issue #287 — two unrelated PRs were blocked because a safety-gate refusal
#: to run `ollama list` was scored as the candidate's regression).
_REFUSAL_RE = re.compile(
    r"(?i)(?:"
    r"safety gate refus\w*"
    r"|refused the .{0,24}(?:query|request|tool|command)"
    r"|blocked before i could"
    r"|blocked by (?:the )?safety gate"
    r"|(?:harm|safety) gate\b.{0,48}\b(?:block|refus)\w*"
    r")"
)

#: How many times to re-sample a case whose sample came back inconclusive.
#: A refusal is environmental, so one more attempt usually settles it; when all
#: k attempts refuse, the case is recorded `inconclusive: true` and the flip
#: gate reports it separately instead of blaming the candidate.
INCONCLUSIVE_RETRIES = 3


def is_refusal(answer):
    """True when the answer is the runtime refusing to attempt the task."""
    return bool(answer) and _REFUSAL_RE.search(answer) is not None


def _check(label, ok, why="", matched_pattern=None, matched_text=None, **extra):
    """One check outcome, shaped for the run record.

    ``label``/``ok`` are the fields existing readers use (`scoreboard_report.py`
    lists failed labels, the flip gate reads ``pass``); everything after them is
    audit evidence added for issue #319, so a disputed verdict can be re-derived
    from the record instead of from a memory of the model call.
    """
    entry = {
        "label": label,
        "ok": bool(ok),
        "why": why or "",
        "matched_pattern": matched_pattern,
        "matched_text": matched_text,
    }
    entry.update(extra)
    return entry


def check_case(case, answer, gt_tokens):
    """Return (passed, [check dicts: label, ok, why, matched_pattern, ...])."""
    results = []
    if answer is None:
        return False, [_check("ran", False, "no answer returned")]

    flags = re.I | re.M
    for chk in case.get("checks", []):
        kind = chk["type"]
        if kind == "must_contain":
            ok, pat, text = _search_any([chk["pattern"]], answer, flags)
            results.append(
                _check(
                    f"contains /{chk['pattern']}/", ok, chk.get("why", ""), pat, text
                )
            )
        elif kind == "must_not_contain":
            found, pat, text = _search_any([chk["pattern"]], answer, flags)
            # The evidence for an exclusion is the offending text, when there is
            # any: "excludes /x/ -> ok" with matched_pattern None means the
            # pattern was absent, which is exactly the claim being made.
            results.append(
                _check(
                    f"excludes /{chk['pattern']}/",
                    not found,
                    chk.get("why", ""),
                    pat,
                    text,
                )
            )
        elif kind == "must_contain_any":
            pats = chk["patterns"]
            ok, pat, text = _search_any(pats, answer, flags)
            results.append(
                _check(
                    f"contains any of {pats}",
                    ok,
                    chk.get("why", ""),
                    pat,
                    text,
                    patterns_tried=list(pats),
                )
            )
        elif kind == "must_contain_number":
            # A prompt that asks for a NUMBER must accept the numeral OR the way it
            # is worded: "Four." answers "What is 2 + 2?" as surely as "4" does.
            # The check for a value differs from `must_contain` only in that the
            # wording may not be assumed.
            val = str(chk["value"]).strip()
            pats = _number_patterns(val) or [re.escape(val)]
            ok, pat, text = _search_any(pats, answer, flags)
            results.append(
                _check(
                    f"contains the number {val} (numeral or word)",
                    ok,
                    chk.get("why", ""),
                    pat,
                    text,
                    patterns_tried=list(pats),
                )
            )
        elif kind == "must_contain_n_of":
            # "name three devices" needs three DISTINCT hits, not one specific trio.
            # One pattern per device (aliases grouped inside it) so two mentions of a
            # single device cannot be counted as two devices.
            pats = chk["patterns"]
            n = int(chk.get("n", 1))
            hits = [p for p in pats if re.search(p, answer, flags)]
            ok = len(hits) >= n
            results.append(
                _check(
                    f"names >= {n} of {len(pats)} accepted device patterns "
                    f"(got {len(hits)})",
                    ok,
                    chk.get("why", ""),
                    hits[0] if hits else None,
                    None,
                    patterns_tried=list(pats),
                    matched_patterns=list(hits),
                )
            )
        elif kind == "max_words":
            n = len(answer.split())
            ok = n <= chk["n"]
            results.append(
                _check(
                    f"<= {chk['n']} words (got {n})",
                    ok,
                    chk.get("why", ""),
                    None,
                    None,
                    measured=n,
                    limit=int(chk["n"]),
                )
            )
        elif kind == "min_words":
            n = len(answer.split())
            ok = n >= chk["n"]
            results.append(
                _check(
                    f">= {chk['n']} words (got {n})",
                    ok,
                    chk.get("why", ""),
                    None,
                    None,
                    measured=n,
                    limit=int(chk["n"]),
                )
            )
        else:
            results.append(_check(f"unknown check {kind}", False, "runner bug"))

    for tok in gt_tokens:
        ok, pat, text = _search_any(tok["patterns"], answer, re.I)
        # A failing ground-truth check records the patterns that were tried. A
        # pass/fail that says only "not present" leaves the reader unable to tell
        # a real miss from a bad expectation.
        results.append(
            _check(
                f"ground truth '{tok['raw']}' present",
                ok,
                "live value must be reflected in the answer",
                pat,
                text,
                token=tok["raw"],
                patterns_tried=list(tok["patterns"]),
            )
        )

    passed = all(r["ok"] for r in results) and bool(results)
    return passed, results


# ------------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="run a single case by id")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument(
        "--hermes-cmd", help="override the agent command (e.g. a candidate worktree)"
    )
    args = ap.parse_args()

    if args.hermes_cmd:
        global HERMES_CMD
        HERMES_CMD = shlex.split(args.hermes_cmd)
        print(f"[scoreboard] using candidate command: {args.hermes_cmd}")
    if not os.path.exists(CASES):
        print(f"scoreboard: no cases file at {CASES}")
        return 1
    with open(CASES, encoding="utf-8") as fh:
        cases = json.load(fh)["cases"]

    if args.list:
        for c in cases:
            flags = " [judge]" if c.get("judge") else ""
            print(
                f"  {c['id']:34s} {c.get('domain', '?'):10s}{flags}  {c['prompt'][:60]}"
            )
        print(f"\n  {len(cases)} cases")
        return 0

    if args.id:
        cases = [c for c in cases if c["id"] == args.id]
        if not cases:
            print(f"scoreboard: no case with id {args.id}")
            return 1
    elif args.limit:
        cases = cases[: args.limit]

    # Partial runs must never contaminate the trend: the score is computed over the
    # cases RUN, so a 1-case run scoring 100% is not comparable to a 16-case run.
    # A reporter that compared them would cry wolf on its first week - and a
    # scoreboard that cries wolf gets ignored, which is the whole failure mode.
    partial = bool(args.id or args.limit)

    os.makedirs(RUNS_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(
        f"=== agent scoreboard · {stamp} · {len(cases)} cases"
        + ("  [PARTIAL - excluded from the trend]" if partial else "")
        + " ===\n"
    )

    scored, judged, total_cost, scored_weight, passed_weight = [], [], 0.0, 0.0, 0.0
    inconclusive_cases = []
    for case in cases:
        gt_tokens, gt_err, gt_raw = ground_truth(case)
        answer, meta = run_case(case, args.timeout)
        if gt_err:
            ok, detail = False, [_check("ground truth available", False, gt_err)]
        else:
            ok, detail = check_case(case, answer, gt_tokens)

        # A refusal is re-sampled, not scored: the runtime declined to attempt
        # the task, which says nothing about the candidate (issue #287). Only a
        # genuinely wrong answer is a failure on the first sample.
        inconclusive = False
        tries = 1
        while (
            not ok
            and not gt_err
            and is_refusal(answer)
            and tries < INCONCLUSIVE_RETRIES
        ):
            tries += 1
            print(f"        .. refused, re-sampling ({tries}/{INCONCLUSIVE_RETRIES})")
            answer, meta = run_case(case, args.timeout)
            ok, detail = check_case(case, answer, gt_tokens)
        if not ok and not gt_err and is_refusal(answer):
            inconclusive = True

        weight = float(case.get("weight", 1))
        tag = "INCL" if inconclusive else ("PASS" if ok else "FAIL")
        cost = meta.get("cost_usd")
        if cost:
            total_cost += cost
        print(
            f"[{tag}] {case['id']}  ({meta.get('seconds', '?')}s"
            + (f", ${cost:.4f}" if cost else "")
            + ")"
        )
        for chk in detail:
            if not chk["ok"]:
                print(
                    f"        ✗ {chk['label']}"
                    + (f" — {chk['why']}" if chk.get("why") else "")
                )

        # The recorded answer must be the thing the checks actually ran on:
        # `answer_head` stays for compact summaries, `answer` (bounded, with an
        # explicit truncation marker) is what makes a verdict auditable later.
        recorded_answer = answer or meta.get("error", "") or ""
        answer_body, answer_cut = _bounded(recorded_answer, ANSWER_MAX_CHARS)
        entry = {
            "id": case["id"],
            "domain": case.get("domain"),
            "pass": ok,
            "inconclusive": inconclusive,
            "attempts": tries,
            "weight": weight,
            "cost_usd": cost,
            "seconds": meta.get("seconds"),
            "checks": detail,
            "answer_head": recorded_answer[:400],
            "answer": answer_body,
            "answer_chars": len(recorded_answer),
            "answer_truncated": answer_cut,
        }
        # The probe's own output, for live-state cases: without it the expected
        # token is an assertion with no evidence behind it.
        gt = case.get("ground_truth")
        if gt:
            probe_raw, probe_cut = _bounded(gt_raw or "", PROBE_RAW_MAX_CHARS)
            entry["probe"] = {
                "cmd": gt.get("cmd"),
                "extract": gt.get("extract"),
                "tokens": [t["raw"] for t in gt_tokens],
                "raw": probe_raw,
                "raw_truncated": probe_cut,
                "error": gt_err,
            }
        if case.get("judge"):
            judged.append(entry)
        elif inconclusive:
            # Not a measurement: the runtime never attempted the task, so it can
            # neither earn nor cost score. Recorded and reported, so a PERSISTENT
            # refusal still shows up instead of hiding as a quiet failure.
            inconclusive_cases.append(entry)
        else:
            scored.append(entry)
            scored_weight += weight
            passed_weight += weight if ok else 0.0

    score = (passed_weight / scored_weight * 100) if scored_weight else 0.0
    print(
        f"\n=== SCORE {score:.1f}%  ({sum(1 for e in scored if e['pass'])}/"
        f"{len(scored)} deterministic cases) ==="
    )

    # Trend against the previous run.
    prev = None
    if os.path.exists(HISTORY):
        try:
            with open(HISTORY, encoding="utf-8") as fh:
                lines = [json.loads(l) for l in fh if l.strip()]
            if lines:
                prev = lines[-1]
        except (OSError, ValueError):
            prev = None
    if prev:
        delta = score - prev.get("score", 0.0)
        arrow = "▲" if delta > 0 else ("▼" if delta < 0 else "=")
        print(
            f"  previous run {prev.get('score', 0):.1f}% "
            f"({prev.get('at', '?')[:10]})  ->  {arrow} {delta:+.1f}"
        )

    if judged:
        jp = sum(1 for e in judged if e["pass"])
        print(
            f"  (judged cases, reported separately and NOT in the score: "
            f"{jp}/{len(judged)} passed)"
        )
    if inconclusive_cases:
        print(
            f"  (inconclusive cases, NOT in the score — the runtime refused "
            f"the task: {len(inconclusive_cases)}: "
            f"{', '.join(e['id'] for e in inconclusive_cases[:5])})"
        )
    print(f"  total cost this run: ${total_cost:.4f}")

    record = {
        "at": stamp,
        "score": score,
        "cases": len(scored),
        "passed": sum(1 for e in scored if e["pass"]),
        "cost_usd": round(total_cost, 6),
        "partial": partial,
        "results": scored + judged + inconclusive_cases,
        "judged_count": len(judged),
        "inconclusive_count": len(inconclusive_cases),
    }
    with open(os.path.join(RUNS_DIR, f"{stamp}.json"), "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    if not args.no_history and not partial:
        with open(HISTORY, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({
                    k: record[k] for k in ("at", "score", "cases", "passed", "cost_usd")
                })
                + "\n"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
