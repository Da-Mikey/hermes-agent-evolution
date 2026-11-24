"""Tests for evolution_qc_review.py — agentic QC review loop (#796)."""

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from evolution_qc_review import (  # noqa: E402
    EXIT_UNSCORED,
    _record,
    adjudicate_review,
    build_config_fingerprint,
    build_critic_task,
    build_qc_review_task,
    verify_critic_grounding,
    main,
    parse_critic_report,
    parse_qc_report,
    record_critic_verdict,
    verify_evidence_ref,
)

_j = json.dumps


def test_build_task():
    task = build_qc_review_task("Implemented X", ["a.py", "b.py"], issue_number=796)
    assert task["role"] == "leaf" and "Implemented X" in task["goal"]
    assert (
        "a.py" in task["goal"]
        and "#796" in task["context"]
        and "file" in task["toolsets"]
    )


def test_build_no_files():
    assert "no files listed" in build_qc_review_task("s", [])["goal"]


def test_parse_pass():
    r = parse_qc_report(_j({"verdict": "pass", "has_blocking": False, "issues": [], "recommendations": ["x"]}))  # fmt: skip
    assert r["ok"] and r["verdict"] == "pass" and not r["has_blocking"]


def test_parse_fail_blocking():
    r = parse_qc_report(_j({"verdict": "fail", "has_blocking": True, "issues": [{"severity": "high"}]}))  # fmt: skip
    assert not r["ok"] and r["has_blocking"]


def test_parse_pass_blocking_not_ok():
    assert not parse_qc_report(_j({"verdict": "pass", "has_blocking": True}))["ok"]


def test_parse_markdown_fenced():
    raw = 'Review:\n```json\n{"verdict": "pass", "has_blocking": false}\n```\nDone.'
    assert parse_qc_report(raw)["ok"]


def test_parse_bare_json():
    raw = 'Done. {"verdict": "fail", "has_blocking": false, "issues": []} All.'
    r = parse_qc_report(raw)
    assert r["verdict"] == "fail" and not r["ok"]


def test_parse_garbage():
    for bad in ("", "no json", "```code\nnot json\n```", None):
        r = parse_qc_report(bad)
        assert not r["ok"] and r["verdict"] == "fail"


def test_cli_build(capsys):
    assert main(["x", "build", "--summary", "impl X", "--files", "a.py,b.py", "--issue", "796"]) == 0  # fmt: skip
    assert "impl X" in json.loads(capsys.readouterr().out)["goal"]
    assert main(["x", "build", "--files", "a.py"]) == 2  # missing --summary


def test_cli_parse(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(_j({"verdict": "pass", "has_blocking": False})))  # fmt: skip
    assert main(["x", "parse"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(_j({"verdict": "fail", "has_blocking": True})))  # fmt: skip
    assert main(["x", "parse"]) == 1


def test_cli_usage():
    assert main(["x"]) == 2 and main(["x", "frob"]) == 2


# ---------------------------------------------------------------------------
# #234 three-valued gate outcomes (rework of the PR #244 review)
# ---------------------------------------------------------------------------


def test_prompt_hash_tracks_the_actual_prompt():
    """Defect 1: a version-stamped hash is one constant, so prompt drift was invisible."""
    a = build_config_fingerprint("review this diff", outcome="pass")
    b = build_config_fingerprint("review this diff, harder", outcome="pass")
    assert a["prompt_hash"] != b["prompt_hash"]
    assert (
        a["prompt_hash"]
        == build_config_fingerprint("review this diff", outcome="pass")["prompt_hash"]
    )
    # No prompt supplied is "unknown", never a hash that compares equal across records it
    # cannot vouch for.
    assert build_config_fingerprint(None, outcome="unscored")["prompt_hash"] is None


def test_unscored_record_carries_outcome_and_prompt_hash():
    r = parse_qc_report("no json here", prompt="P1")
    assert r["outcome"] == "unscored"
    assert not r["ok"]
    assert r["verdict"] == "fail"  # legacy field, unchanged for existing callers
    assert (
        r["config"]["prompt_hash"]
        == build_config_fingerprint("P1", outcome="unscored")["prompt_hash"]
    )


def test_unknown_verdict_is_unscored_not_fail():
    assert parse_qc_report(_j({"verdict": "maybe", "has_blocking": False}))[
        "outcome"
    ] == ("unscored")


def test_measured_verdicts_keep_their_outcome():
    assert (
        parse_qc_report(_j({"verdict": "pass", "has_blocking": False}))["outcome"]
        == "pass"
    )
    assert (
        parse_qc_report(_j({"verdict": "fail", "has_blocking": True}))["outcome"]
        == "fail"
    )


def test_mutating_a_returned_unscored_record_does_not_leak():
    """Defect 3: ``dict(_FAIL)`` shared its lists with the module-level template."""
    first = parse_qc_report("no json")
    first["issues"].append({"severity": "high"})
    first["recommendations"].append("nope")
    second = parse_qc_report("no json")
    assert second["issues"] == []
    assert second["recommendations"] == []


def test_unknown_outcome_raises_value_error():
    """Defect 4: a bare assert would be stripped under ``python -O``."""
    with pytest.raises(ValueError):
        _record("bogus")


def test_adjudicate_of_unscored_reviewer_is_unscored_but_blocking():
    """Defect 2: the decision path must not report non-measurement as a rework demand."""
    v = adjudicate_review(parse_qc_report("garbage output"), {"dissent": False})
    assert v["block"] is True  # fail safe: absent evidence never widens a gate
    assert v["outcome"] == "unscored"  # but it is not a measured rejection
    assert v["verdict"] == "unscored"


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_cli_parse_garbage_exits_unscored(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("no json at all"))
    assert main(["x", "parse"]) == EXIT_UNSCORED == 3


def test_cli_adjudicate_unscored_exits_3_not_1(tmp_path, capsys):
    rev = _write(tmp_path, "rev.json", "not json")
    crit = _write(tmp_path, "crit.json", _j({"dissent": False}))
    assert main(["x", "adjudicate", "--reviewer", rev, "--critic", crit]) == 3
    assert json.loads(capsys.readouterr().out)["outcome"] == "unscored"


def test_cli_adjudicate_measured_rework_still_exits_1(tmp_path, capsys):
    rev = _write(tmp_path, "rev.json", _j({"verdict": "fail", "has_blocking": True}))
    crit = _write(tmp_path, "crit.json", _j({"dissent": False}))
    assert main(["x", "adjudicate", "--reviewer", rev, "--critic", crit]) == 1


def test_cli_parse_prompt_file_feeds_the_fingerprint(tmp_path, capsys, monkeypatch):
    prompt = _write(tmp_path, "task.json", "the real reviewer prompt")
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(_j({"verdict": "pass", "has_blocking": False}))
    )
    assert main(["x", "parse", "--prompt", prompt]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (
        out["config"]["prompt_hash"]
        == build_config_fingerprint("the real reviewer prompt", outcome="pass")[
            "prompt_hash"
        ]
    )


def test_critic_ledger_keeps_unscored_distinct_from_measured_rework(tmp_path):
    """#257: an unscored block must not be logged as a measured rework.

    ``record_critic_verdict`` used to flatten the verdict to rework/proceed, so a
    reviewer that produced no measurable verdict at all was indistinguishable in
    the append-only ledger from a reviewer that measured a real failure — the exact
    conflation #234 removed upstream. Both still block and both keep the legacy
    ``rework`` vocabulary; ``outcome`` is what tells them apart.
    """
    ledger = tmp_path / "stage_gate.jsonl"
    cases = [
        ({"ok": False, "outcome": "unscored"}, "unscored"),  # nothing measurable
        ({"ok": False, "outcome": "fail"}, "fail"),  # a measured failure
    ]
    for reviewer_report, expected in cases:
        verdict = adjudicate_review(reviewer_report, {})
        assert verdict["block"], "both cases must still block"
        assert verdict["outcome"] == expected
        record_critic_verdict(
            str(ledger), verdict, reviewer_report=reviewer_report, critic_report={}
        )

    records = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [r["verdict"] for r in records] == ["rework", "rework"]  # compat kept
    assert [r["outcome"] for r in records] == ["unscored", "fail"]
    assert (records[0]["verdict"], records[0]["outcome"]) != (
        records[1]["verdict"],
        records[1]["outcome"],
    )


def test_critic_ledger_derives_outcome_for_a_verdict_without_one(tmp_path):
    """A verdict dict predating #234 has no ``outcome`` key — derive it, never crash."""
    ledger = tmp_path / "stage_gate.jsonl"
    record_critic_verdict(
        str(ledger), {"block": True}, reviewer_report={}, critic_report={}
    )
    record_critic_verdict(
        str(ledger), {"block": False}, reviewer_report={}, critic_report={}
    )
    # an outcome outside the three-valued set must not leak into the ledger either
    record_critic_verdict(
        str(ledger),
        {"block": True, "outcome": "bogus"},
        reviewer_report={},
        critic_report={},
    )
    records = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert [r["outcome"] for r in records] == ["fail", "pass", "fail"]


# ── #267: claim-blind critic pass + verified grounding ─────────────────────
_SAMPLE_DIFF = (
    "diff --git a/tools/x.py b/tools/x.py\n"
    "--- a/tools/x.py\n"
    "+++ b/tools/x.py\n"
    "@@ -10,6 +10,8 @@ def f():\n"
    "-    return 1\n"
    "+    return 2\n"
    "+    # a newly added comment line\n"
)


def _ok_reviewer():
    return parse_qc_report('{"verdict": "pass", "has_blocking": false}')


def _grounded_dissent(reference):
    return parse_critic_report(
        _j({
            "dissent": True,
            "grounded": True,
            "evidence_ref": reference,
            "failure_mode": "f() returns 2 now, callers assert 1",
            "agreement_reason": "",
        })
    )


def test_claim_blind_task_withholds_the_worker_summary():
    """The leak is the point: a blind pass must not be told what the worker claims."""
    task = build_critic_task(
        "WORKER CLAIM: all tests pass, nothing else touched",
        ["tools/x.py"],
        diff=_SAMPLE_DIFF,
        target_digest={"tools/x.py": "a" * 64},
        claim_blind=True,
    )
    assert "WORKER CLAIM" not in task["goal"]
    assert "## Summary" not in task["goal"]
    assert "## Diff Under Review" in task["goal"]
    assert "tools/x.py:12" not in task["goal"]  # no leak via the digest block
    assert "sha256:" + "a" * 64 in task["goal"]
    assert task["claim_blind"] is True


def test_claim_aware_task_still_carries_the_summary():
    """Legacy pass is unchanged, so both can be run over one artefact."""
    task = build_critic_task(
        "WORKER CLAIM: all tests pass", ["tools/x.py"], claim_blind=False
    )
    assert "WORKER CLAIM" in task["goal"]
    assert "## Summary" in task["goal"]
    assert task["claim_blind"] is False


def test_evidence_ref_inside_a_changed_hunk_is_verified():
    status, detail = verify_evidence_ref("tools/x.py:12", diff=_SAMPLE_DIFF)
    assert status == "verified", detail
    assert "changed hunk" in detail


def test_evidence_ref_outside_every_hunk_is_ungrounded():
    status, _ = verify_evidence_ref("tools/x.py:99", diff=_SAMPLE_DIFF)
    assert status == "ungrounded"


def test_evidence_ref_to_a_digest_attested_file_is_verified():
    status, detail = verify_evidence_ref(
        "tools/x.py:99", diff="", target_digest={"tools/x.py": "b" * 64}
    )
    assert status == "verified", detail
    assert "target digest" in detail


def test_evidence_ref_with_no_material_is_unmeasured():
    assert verify_evidence_ref("tools/x.py:12")[0] == "unmeasured"
    assert verify_evidence_ref("")[0] == "unmeasured"


def test_ungrounded_dissent_does_not_block_when_evidence_is_checkable():
    """The declared flag is no longer sufficient — the reference has to resolve."""
    verdict = adjudicate_review(
        _ok_reviewer(), _grounded_dissent("tools/x.py:99"), diff=_SAMPLE_DIFF
    )
    assert verdict["block"] is False
    assert verdict["grounding"]["aware"]["grounding_status"] == "ungrounded"


def test_verified_dissent_blocks_and_names_its_evidence():
    verdict = adjudicate_review(
        _ok_reviewer(), _grounded_dissent("tools/x.py:12"), diff=_SAMPLE_DIFF
    )
    assert verdict["block"] is True
    assert verdict["blocking_passes"] == ["aware"]
    assert verdict["grounding"]["aware"]["grounded_verified"] is True
    assert "verified" in verdict["reason"]


def test_declared_grounding_without_material_keeps_the_legacy_block():
    """Never widen a gate on absent evidence: unmeasurable grounding still blocks."""
    verdict = adjudicate_review(_ok_reviewer(), _grounded_dissent("tools/x.py:12"))
    assert verdict["block"] is True
    assert verdict["grounding"]["aware"]["grounding_status"] == "unmeasured"


def test_blind_pass_adds_an_objection_the_aware_pass_missed():
    """Disagreement is the signal that the aware pass was reading the claim."""
    aware = parse_critic_report(
        _j({"dissent": False, "grounded": False, "agreement_reason": "looks fine"})
    )
    verdict = adjudicate_review(
        _ok_reviewer(),
        aware,
        diff=_SAMPLE_DIFF,
        blind_critic_report=_grounded_dissent("tools/x.py:12"),
    )
    assert verdict["block"] is True
    assert verdict["blocking_passes"] == ["blind"]
    assert verdict["blind_aware_disagreement"] is True
    assert "callers assert 1" in verdict["reason"]


def test_blocking_reason_carries_the_failure_mode_for_either_pass():
    """A blocking gate must be readable: never an empty colon (#267 rework).

    The aware pass lost its ``failure_mode`` because ``verify_critic_grounding``'s
    dict did not carry it, so every aware block rendered as ``critic grounded
    objection [...] : ``. Pin the contract for BOTH passes so neither can regress.
    """
    aware_block = adjudicate_review(
        _ok_reviewer(),
        _grounded_dissent("tools/x.py:12"),
        diff=_SAMPLE_DIFF,
    )
    assert aware_block["block"] is True
    assert aware_block["blocking_passes"] == ["aware"]
    assert "callers assert 1" in aware_block["reason"]
    assert not aware_block["reason"].rstrip().endswith(":")

    blind_block = adjudicate_review(
        _ok_reviewer(),
        parse_critic_report(_j({"dissent": False, "agreement_reason": "looks fine"})),
        diff=_SAMPLE_DIFF,
        blind_critic_report=_grounded_dissent("tools/x.py:12"),
    )
    assert blind_block["blocking_passes"] == ["blind"]
    assert "callers assert 1" in blind_block["reason"]
    assert not blind_block["reason"].rstrip().endswith(":")


def test_grounding_gate_dict_exposes_the_failure_mode():
    """``adjudicate_review`` only sees the gate dict — it must carry the text."""
    gate = verify_critic_grounding(
        _grounded_dissent("tools/x.py:12"), diff=_SAMPLE_DIFF
    )
    assert gate["failure_mode"] == "f() returns 2 now, callers assert 1"


def test_both_passes_agreeing_records_no_disagreement():
    aware = parse_critic_report(
        _j({"dissent": False, "grounded": False, "agreement_reason": "looks fine"})
    )
    blind = parse_critic_report(
        _j({"dissent": False, "grounded": False, "agreement_reason": "looks fine too"})
    )
    verdict = adjudicate_review(
        _ok_reviewer(), aware, diff=_SAMPLE_DIFF, blind_critic_report=blind
    )
    assert verdict["block"] is False
    assert "blind_aware_disagreement" not in verdict


def test_cli_build_critic_claim_blind_needs_no_summary(capsys):
    """A blind pass takes no summary, so the CLI must not demand one."""
    assert main(["x", "build-critic", "--claim-blind", "--files", "tools/x.py"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claim_blind"] is True
    assert "## Summary" not in payload["goal"]


def test_cli_build_critic_without_summary_or_blind_flag_is_usage_error():
    assert main(["x", "build-critic", "--files", "tools/x.py"]) == 2
