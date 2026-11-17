# -*- coding: utf-8 -*-
"""The ``max_words`` cap must measure the answer, not a governance aside (#401).

The defect, measured 2026-11-15 on one unchanged install: the ``fmt-answer-first``
probe asks "What is 17 multiplied by 23?" and requires the numeral 391, no
preamble, and ``<= 60 words``.  Three candidate runs an hour apart answered
``391.`` (1 word), ``Three hundred and ninety-one.`` (4 words) and::

    **391.**

    17 × 20 = 340, plus 17 × 3 = 51 → 391.

    —

    One note, since it's a fault I met: the turn gate refused my `terminal` call …

The third passed the arithmetic check and the no-preamble check and failed only
on length (86 words), because the trailing note explaining a *gate refusal* was
counted as if it were verbosity.  That block landed on PR #400, a
circuit-breaker-only change whose tool call was refused before execution: the
gate reported a regression on code that never ran.

What these tests pin:

* the recorded answers PASS, and the aside is reported as set aside;
* wrong answers still FAIL — the cap stays meaningful, it is not deleted;
* a CONTROL that reimplements the pre-fix behaviour (plain ``len(answer.split())``)
  reproduces tonight's ``fail`` on the same fixture, so the repair is shown to
  change the verdict only where it should;
* a mid-answer note cannot silently delete real content from the count.

The fixture is the recorded answer *as the run record stored it*: ``answer_head``
is bounded at 400 characters, so it holds 78 of the 86 measured words and 14 of
the answer body.  Both counts are asserted, not assumed.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import agent_scoreboard as sb  # noqa: E402

#: The probe's checks, copied from ~/.hermes/evolution/probes/agent-scoreboard.json
#: for probe id ``fmt-answer-first``.  The frozen id must stay in the set, so the
#: checks are reproduced here rather than read from the live probe file: this test
#: must not pass or fail because a config outside the repository was edited.
ANSWER_FIRST_CHECKS = [
    {
        "type": "must_contain_number",
        "value": 391,
        "why": "the arithmetic must be right — numeral or word, both answer the question",
    },
    {
        "type": "must_not_contain",
        "pattern": r"^(sure|of course|certainly|let me|i'?ll|i will|here'?s|great|absolutely)",
        "why": "the answer comes first, not a preamble",
    },
    {"type": "max_words", "n": 60, "why": "an answer, not an essay"},
]

CASE = {"id": "fmt-answer-first", "checks": ANSWER_FIRST_CHECKS}

#: Replies recorded on the night of 2026-11-15 (runs 214151Z / 215249Z / 220402Z).
RECORDED_TINY = "391."
RECORDED_WORDS = "Three hundred and ninety-one."
RECORDED_ANSWER_HEAD = (
    "**391.**\n"
    "\n"
    "17 × 20 = 340, plus 17 × 3 = 51 → 391.\n"
    "\n"
    "—\n"
    "\n"
    "One note, since it's a fault I met: the turn gate refused my `terminal` call "
    'for the sum ("a question buys an answer"), so there was no calculator to check '
    "against and no arithmetic tool exists in the deferred catalog. The answer above "
    "is hand arithmetic — certain here, but the gate should not be blocking a "
    "read-only calculation. Say the word an"
)

VERBOSE_ESSAY = "**391.**\n\n" + "\n\n".join(
    "Multiplication is repeated addition, and seventeen times twenty-three is "
    "the same quantity as seventeen twenties plus seventeen threes, which is "
    "three hundred and forty plus fifty-one, and that lands on three hundred "
    "and ninety-one exactly."
    for _ in range(2)
)


def _max_words_check(answer):
    _, checks = sb.check_case(CASE, answer, [])
    return [c for c in checks if c["label"].startswith("<= 60 words")][0]


def control_plain_word_count(answer):
    """The PRE-FIX check, reimplemented: raw word count, no aside handling.

    A repair is only shown to work if the old behaviour still reproduces the
    defect on the same input.  This is that control.
    """
    return len(answer.split()) <= 60


# ── the three recorded answers all pass ───────────────────────────────────────


def test_recorded_one_word_answer_passes():
    passed, checks = sb.check_case(CASE, RECORDED_TINY, [])
    assert passed, checks
    assert _max_words_check(RECORDED_TINY)["measured"] == 1


def test_recorded_worded_answer_passes():
    passed, checks = sb.check_case(CASE, RECORDED_WORDS, [])
    assert passed, checks
    assert _max_words_check(RECORDED_WORDS)["measured"] == 4


def test_recorded_answer_with_governance_aside_passes():
    """The case that blocked PR #400: 78 stored words, 14 of them the answer."""
    passed, checks = sb.check_case(CASE, RECORDED_ANSWER_HEAD, [])
    assert passed, [c for c in checks if not c["ok"]]

    cap = _max_words_check(RECORDED_ANSWER_HEAD)
    assert cap["measured"] == 14
    assert cap["raw_measured"] == 78
    assert cap["aside_stripped"] is True
    assert "One note" in cap["aside_text"]


# ── the control: the pre-fix check still fails the same input ─────────────────


def test_control_reproduces_tonights_fail_label():
    """Red-screen of the old behaviour — without this the repair proves nothing."""
    assert control_plain_word_count(RECORDED_ANSWER_HEAD) is False
    assert len(RECORDED_ANSWER_HEAD.split()) == 78
    assert sb.strip_governance_aside(RECORDED_ANSWER_HEAD)[0].split()[-1] == "391."


# ── the cap still bites ───────────────────────────────────────────────────────


def test_missing_number_still_fails():
    passed, checks = sb.check_case(CASE, "Roughly four hundred, near enough.", [])
    assert passed is False
    assert [c["ok"] for c in checks if c["label"].startswith("contains")] == [False]


def test_preamble_still_fails():
    passed, _ = sb.check_case(CASE, "Sure thing — the answer is 391.", [])
    assert passed is False


def test_genuine_wall_of_text_still_fails():
    """A real essay with no aside must still trip the cap, or it stopped measuring."""
    assert "One note" not in VERBOSE_ESSAY
    cap = _max_words_check(VERBOSE_ESSAY)
    assert cap["aside_stripped"] is False
    passed, _ = sb.check_case(CASE, VERBOSE_ESSAY, [])
    assert passed is False
    assert cap["measured"] > 60


# ── the stripper itself ───────────────────────────────────────────────────────


def test_no_aside_leaves_answer_untouched():
    body, aside = sb.strip_governance_aside("391.")
    assert body == "391."
    assert aside is None


def test_answer_that_is_only_a_note_is_not_stripped():
    only_note = (
        "Note: the gate refused the calculator call, so this is hand arithmetic."
    )
    body, aside = sb.strip_governance_aside(only_note)
    assert body == only_note
    assert aside is None


def test_mid_answer_note_about_the_work_is_not_stripped():
    """A note that is not about governance must not delete the working after it."""
    answer = (
        "**391.** Note: this is hand arithmetic.\n"
        "\n"
        "The full working is seventeen twenties plus seventeen threes, which is three "
        "hundred and forty plus fifty-one, and that is the number above."
    )
    body, aside = sb.strip_governance_aside(answer)
    assert body == answer
    assert aside is None
    assert "full working" in body


def test_mid_answer_governance_note_keeps_the_text_before_it():
    """Cut at the LAST marker: earlier text survives, so the body is never emptied."""
    answer = (
        "The answer is 391.\n"
        "\n"
        "Note: the turn gate refused my calculator call.\n"
        "\n"
        "The working is seventeen twenties plus seventeen threes."
    )
    body, aside = sb.strip_governance_aside(answer)
    assert body == "The answer is 391."
    assert aside is not None and "turn gate" in aside


def test_inline_dash_aside_is_stripped():
    answer = "**391.** 17 × 20 = 340, plus 17 × 3 = 51 → 391. — One note: the gate refused the call."
    body, aside = sb.strip_governance_aside(answer)
    assert body.endswith("391.")
    assert aside is not None and "One note" in aside
