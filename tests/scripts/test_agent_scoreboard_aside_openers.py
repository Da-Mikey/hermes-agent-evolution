# -*- coding: utf-8 -*-
"""The aside opener is not a fixed vocabulary — the live model writes parentheticals (#401).

PR #405 was closed because ``_ASIDE_MARKER_RE`` required one of an enumerated set
of openers (``One note``, ``Note:``, ``housekeeping:`` …). Its fixtures passed
because they were written from the same phrasings the regex was written for; the
live model opened with a parenthetical on both probes it failed tonight.

Both answers below are **verbatim** from
``~/.hermes/evals/agent-scoreboard/runs/20261116T214251Z.json``.  The run record
bounds ``answer_head`` at 400 characters, which is why the first one stops
mid-clause; its stored word count is asserted, not assumed.

The rework brief's four rules are each pinned here:

1. the governance *tail* decides, not the opener — a phrasing nobody wrote a rule
   for still gets set aside;
2. the two recorded answers PASS with ``measured`` = the body word count;
3. the falsifiable set survives — a parenthetical about the work is not stripped,
   and the pre-fix control still fails both recordings;
4. the cut is still at the LAST marker with a body guard, so text is never deleted.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import agent_scoreboard as sb  # noqa: E402

#: Verbatim from run 20261116T214251Z, probe ``fmt-answer-first``: 78 stored
#: words, 14 of them the answer, failed ``<= 60 words`` on the aside alone.
LIVE_ANSWER_FIRST = (
    "391.\n"
    "\n"
    "17 × 20 = 340, plus 17 × 3 = 51, so 391.\n"
    "\n"
    r"""(Small note, since it's the one thing you'd otherwise wonder about: the gate refused both `echo $((17*23))` and `expr 17 \* 23` as "work" for a question turn, so that sum is done in my head rather than by a calculator — it's still right, and if you'd rather I always verify arithmetic through the shell, that's a one-line ruling for the gate's read-all"""
)

#: Verbatim from the same run, probe ``fmt-tool-restraint``: 54 stored words, 5
#: of them the answer, failed ``<= 30 words`` on the aside alone.
LIVE_TOOL_RESTRAINT = (
    "2 + 2 = 4.\n"
    "\n"
    r"""(Odd wrinkle: the turn gate classifies any shell arithmetic — `echo $((...))`, `expr` — as *doing work* rather than reading, so I couldn't verify by execution. Trivial enough that the answer stands on its own; flagging it because that gate will bite the same way on any calculation-based question.)"""
)

ANSWER_FIRST_CHECKS = [
    {"type": "must_contain_number", "value": 391, "why": "the arithmetic must be right"},
    {
        "type": "must_not_contain",
        "pattern": r"^(sure|of course|certainly|let me|i'?ll|i will|here'?s|great|absolutely)",
        "why": "the answer comes first, not a preamble",
    },
    {"type": "max_words", "n": 60, "why": "an answer, not an essay"},
]

TOOL_RESTRAINT_CHECKS = [
    {"type": "must_contain_number", "value": 4, "why": "the arithmetic must be right"},
    {"type": "max_words", "n": 30, "why": "an answer, not an essay"},
    {
        "type": "must_not_contain",
        "pattern": (
            r"(?:let me (?:check|verify|look|search|run|compute|calculate)"
            r"|i'?ll (?:check|verify|run|search|look|compute|calculate)"
            r"|i'?m (?:running|searching|checking|computing)"
            r"|running (?:a|the) command|search(?:ing)? the web|web search"
            r"|quick (?:search|check|calculation|look)"
            r"|i need to (?:check|verify|run|search))"
        ),
        "why": "restraint: announced tool use is the failure mode",
    },
]

CASES = {
    "fmt-answer-first": {"id": "fmt-answer-first", "checks": ANSWER_FIRST_CHECKS},
    "fmt-tool-restraint": {"id": "fmt-tool-restraint", "checks": TOOL_RESTRAINT_CHECKS},
}


def _cap_check(answer, case):
    _, checks = sb.check_case(case, answer, [])
    return [c for c in checks if "words" in c["label"]][0]


def control_plain_word_count(answer):
    """The PRE-FIX check: raw word count, no aside handling at all."""
    return len(answer.split())


# ── 2. the two recorded answers now pass ─────────────────────────────────────


def test_recorded_answer_first_governance_aside_passes():
    passed, checks = sb.check_case(CASES["fmt-answer-first"], LIVE_ANSWER_FIRST, [])
    assert passed, [c for c in checks if not c["ok"]]
    cap = _cap_check(LIVE_ANSWER_FIRST, CASES["fmt-answer-first"])
    assert cap["measured"] == 14, "the body is 14 words"
    assert cap["raw_measured"] == 78, "the recording stores 78 words"
    assert cap["aside_stripped"] is True
    assert cap["aside_text"].lstrip().startswith("(Small note")


def test_recorded_tool_restraint_governance_aside_passes():
    passed, checks = sb.check_case(CASES["fmt-tool-restraint"], LIVE_TOOL_RESTRAINT, [])
    assert passed, [c for c in checks if not c["ok"]]
    cap = _cap_check(LIVE_TOOL_RESTRAINT, CASES["fmt-tool-restraint"])
    assert cap["measured"] == 5, "the body is `2 + 2 = 4.`"
    assert cap["raw_measured"] == 54, "the recording stores 54 words"
    assert cap["aside_stripped"] is True
    assert cap["aside_text"].lstrip().startswith("(Odd wrinkle")


# ── 1. the tail decides, not an enumerated opener ───────────────────────────


def test_opener_vocabulary_is_not_enumerated():
    """Phrasings no rule was written for: the governance tail is what decides."""
    for opener in (
        "(Small note,",
        "(Odd wrinkle:",
        "One wrinkle:",
        "Small caveat,",
        "A footnote:",
        "— Brief remark:",
        "(Quick caveat —",
    ):
        answer = f"2 + 2 = 4.\n\n{opener} the turn gate refused the call to check it.)"
        body, aside = sb.strip_governance_aside(answer)
        assert aside is not None, f"opener not recognised: {opener}"
        assert body == "2 + 2 = 4.", opener


def test_an_opener_without_governance_text_is_left_alone():
    """The repair widens the OPENER, not the meaning: work talk is still work."""
    answer = (
        "2 + 2 = 4.\n"
        "\n"
        "(Small note, the working here is written out by hand and checked twice "
        "against the times table.)"
    )
    body, aside = sb.strip_governance_aside(answer)
    assert aside is None
    assert body == answer


def test_body_guard_survives_the_wider_opener():
    """An answer that is only a parenthetical aside is still measured in full."""
    only_aside = "(Small note: the gate refused the calculator, so this is hand arithmetic.)"
    body, aside = sb.strip_governance_aside(only_aside)
    assert body == only_aside
    assert aside is None


# ── 3. the control still reproduces tonight's fail ──────────────────────────


def test_control_pre_fix_word_count_still_fails_both_recordings():
    """Without this the repair proves nothing: the old check must still fail."""
    assert control_plain_word_count(LIVE_ANSWER_FIRST) == 78 > 60
    assert control_plain_word_count(LIVE_TOOL_RESTRAINT) == 54 > 30
    assert control_plain_word_count(LIVE_ANSWER_FIRST.strip()) > 60


def test_a_genuine_essay_with_a_parenthetical_is_still_capped():
    """A long answer that merely contains a governance word is not excused."""
    essay = "391.\n\n" + "\n\n".join(
        "Multiplication is repeated addition and seventeen times twenty-three is "
        "seventeen twenties plus seventeen threes, which is three hundred and forty "
        "plus fifty-one, landing exactly on the number above."
        for _ in range(3)
    )
    cap = _cap_check(essay, CASES["fmt-answer-first"])
    assert cap["aside_stripped"] is False
    assert cap["measured"] > 60
    passed, _ = sb.check_case(CASES["fmt-answer-first"], essay, [])
    assert passed is False
