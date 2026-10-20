"""Tests for evolution_qc_review.py — agentic QC review loop (#796)."""

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from evolution_qc_review import (  # noqa: E402  # fmt: skip
    EXIT_UNSCORED,
    adjudicate_review,
    build_config_fingerprint,
    build_qc_review_task,
    main,
    parse_qc_report,
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


def test_parse_garbage_is_unscored_not_fail():
    """#234: nothing was measured. That is not a failure of the candidate, so it
    must not enter the failure rate — but it MUST still block acceptance."""
    for bad in ("", "no json", "```code\nnot json\n```", None):
        r = parse_qc_report(bad)
        assert not r["ok"], "unscored must never pass"
        assert r["has_blocking"], "unscored must block acceptance"
        assert r["outcome"] == "unscored"
        assert r["verdict"] == "unscored"
        assert r["reason"] == "reviewer_output_unparseable"


def test_parse_unrecognized_verdict_is_unscored():
    for raw in (_j({"verdict": "maybe"}), _j({"has_blocking": False})):
        r = parse_qc_report(raw)
        assert r["outcome"] == "unscored" and not r["ok"]
        assert r["reason"].startswith("reviewer_verdict_unrecognized:")


def test_measured_outcomes_unchanged():
    """The third outcome must not disturb what pass/fail already mean."""
    assert parse_qc_report(_j({"verdict": "pass", "has_blocking": False}))["outcome"] == "pass"
    assert parse_qc_report(_j({"verdict": "fail", "has_blocking": True}))["outcome"] == "fail"
    assert parse_qc_report(_j({"verdict": "pass", "has_blocking": True}))["outcome"] == "fail"


def test_every_record_carries_a_config_fingerprint():
    for raw in (_j({"verdict": "pass", "has_blocking": False}), "garbage"):
        fp = parse_qc_report(raw)["config_fingerprint"]
        assert set(fp) >= {"model", "prompt_hash", "toolset_hash"}
        assert fp["prompt_hash"] and fp["toolset_hash"]

    a = build_config_fingerprint(model="m1", toolsets=["file", "search"])
    b = build_config_fingerprint(model="m2", toolsets=["file", "search"])
    c = build_config_fingerprint(model="m1", toolsets=["file"])
    assert a["model"] != b["model"] and a["prompt_hash"] == b["prompt_hash"]
    assert a["toolset_hash"] != c["toolset_hash"]


def test_adjudicate_unscored_blocks_without_demanding_rework():
    """An unmeasured review is not a rework demand: the pipeline must not learn
    'the candidate was bad' from a reviewer that never arrived."""
    reviewer = parse_qc_report("reviewer crashed: no json here")
    critic = {"dissent": False, "grounded": False, "ok": True}
    v = adjudicate_review(reviewer, critic)
    assert v["block"] is True
    assert v["verdict"] == "unscored" and v["outcome"] == "unscored"
    assert "no measurement" in v["reason"]


def test_failure_rate_ignores_unscored():
    """The metric the pipeline optimises against: unscored records are not failures."""
    records = [
        parse_qc_report(_j({"verdict": "fail", "has_blocking": True})),
        parse_qc_report(_j({"verdict": "pass", "has_blocking": False})),
        parse_qc_report("timed out, no output"),
        parse_qc_report(""),
    ]
    measured = [r for r in records if r["outcome"] != "unscored"]
    failures = [r for r in measured if r["outcome"] == "fail"]
    unscored = [r for r in records if r["outcome"] == "unscored"]
    assert len(measured) == 2 and len(failures) == 1 and len(unscored) == 2
    assert len(failures) / len(measured) == 0.5, "unscored folded into the failure rate"


def test_ledger_records_outcome_and_fingerprint(tmp_path):
    from evolution.lib.stage_gate import record_review_critic_verdict

    ledger = tmp_path / "ledger.jsonl"
    fp = build_config_fingerprint(model="deepseek-v4", toolsets=["file"])
    record_review_critic_verdict(
        ledger, verdict="unscored", outcome="unscored", config_fingerprint=fp,
        reviewer_ok=True, dissent=False, grounded=False,
    )  # fmt: skip
    rec = json.loads(ledger.read_text().strip())
    assert rec["outcome"] == "unscored"
    assert rec["config_fingerprint"]["model"] == "deepseek-v4"
    assert rec["reviewer_ok"] is False, "unscored must not read as a passing review"


def test_cli_parse_exit_codes(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("complete garbage"))
    assert main(["x", "parse"]) == EXIT_UNSCORED == 3  # distinct from a measured fail
    out = json.loads(capsys.readouterr().out)
    assert out["outcome"] == "unscored"


def test_cli_parse_fingerprint_flags(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(_j({"verdict": "pass", "has_blocking": False})))  # fmt: skip
    assert main(["x", "parse", "--model", "m9", "--toolsets", "file,search"]) == 0
    fp = json.loads(capsys.readouterr().out)["config_fingerprint"]
    assert fp["model"] == "m9" and fp["toolsets"] == ["file", "search"]


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
