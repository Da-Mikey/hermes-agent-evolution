"""Exact-head independent receipts and real merge CLI admission (#3467)."""
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from evolution_admission import CLASS_GATES, VALIDATION_STEPS, record_admission, verify_receipt
import evolution_merge_gate as gate

NOW = datetime(2026, 10, 15, tzinfo=timezone.utc)
HEAD = "a" * 40
SLUG = "owner/repo"


def receipts():
    run = dict(id=10, run_attempt=1, head_sha=HEAD, path=".github/workflows/ci.yaml",
               event="pull_request", repository={"full_name": SLUG}, status="completed",
               conclusion="success", updated_at=NOW.isoformat(),
               pull_requests=[{"base": {"sha": "base"}, "head": {"sha": HEAD}}])
    jobs = [dict(name=n, run_id=10, head_sha=HEAD, status="completed", conclusion="success")
            for n in set().union(*CLASS_GATES.values())]
    for job in jobs:
        job["steps"] = [{"name": VALIDATION_STEPS.get(job["name"], "aggregate"),
                         "status": "completed", "conclusion": "success"}]
    jobs.append(dict(name="Python tests / Run tests slice 1/1", run_id=10, head_sha=HEAD,
                     status="completed", conclusion="success", steps=[{
                         "name": "Run tests (slice 1/1)", "status": "completed", "conclusion": "success"}]))
    return run, jobs


def runner_for(run, jobs):
    def runner(cmd):
        if "/compare/" in cmd[-1]:
            return 0, json.dumps([{"files": [{"filename": "tools/example.py", "status": "modified"}]}]), ""
        return 0, json.dumps([{"jobs": jobs} if "/jobs?" in cmd[-1] else {"workflow_runs": [run]}]), ""
    return runner


def test_mixed_classes_require_union_and_exact_head():
    run, jobs = receipts()
    files = [{"path": "skills/x/SKILL.md"}, {"path": "agent/x.py"}, {"path": "plugins/x.py"}]
    record, errors = verify_receipt(files, HEAD, SLUG, runner_for(run, jobs), now=NOW)
    assert not errors and set(record["classes"]) == {"memory-skill", "control", "plugin"}
    jobs.pop()
    assert verify_receipt(files, HEAD, SLUG, runner_for(run, jobs), now=NOW)[1]


@pytest.mark.parametrize("mutation", [
    {"head_sha": "b" * 40}, {"path": ".github/workflows/self-grade.yaml"},
    {"event": "workflow_dispatch"}, {"conclusion": "failure"},
    {"repository": {"full_name": "attacker/fork"}}, {"updated_at": "2020-01-01T00:00:00Z"},
])
def test_untrusted_or_stale_provenance_fails_closed(mutation):
    run, jobs = receipts()
    run.update(mutation)
    assert verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG, runner_for(run, jobs), now=NOW)[1]


def test_workflow_change_and_self_grade_cannot_attest():
    run, jobs = receipts()
    assert verify_receipt([{"path": ".github/workflows/ci.yaml"}], HEAD, SLUG, runner_for(run, jobs), now=NOW)[1]
    assert verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG,
                          lambda cmd: (0, json.dumps([{"self_grade": "pass"}]), ""), now=NOW)[1]
    jobs[0]["head_sha"] = "old"
    assert verify_receipt([{"path": "agent/x.py"}], HEAD, SLUG, runner_for(run, jobs), now=NOW)[1]


def test_cycle_budget_is_atomic_idempotent_and_profile_scoped(tmp_path, monkeypatch):
    record = {"head": HEAD, "regressions": 1, "regression_budget": 1}
    store = tmp_path / "budget.sqlite3"
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: record_admission(record, repository=SLUG, cycle="cycle", store=store), range(8)))
    assert all(r["admitted_heads"] == 1 and r["regressions"] == 1 for r in results)
    with pytest.raises(ValueError, match="budget"):
        record_admission(dict(record, head="other"), repository=SLUG, cycle="cycle", store=store)
    for profile in ("A", "B", "A"):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / profile))
        assert record_admission(record, repository=SLUG, cycle="cycle")["admitted_heads"] == 1
    with sqlite3.connect(store) as db:
        assert db.execute("SELECT COUNT(*) FROM admissions").fetchone()[0] == 1


def test_cli_blocks_self_graded_only_evolution_change_before_merge(monkeypatch):
    calls = []
    def runner(cmd):
        calls.append(cmd)
        if cmd[1:3] == ["pr", "view"]:
            return 0, json.dumps({"files": [{"path": "docs/note.md", "additions": 1}],
                                 "headRefOid": HEAD, "headRefName": "evolution/issue-3467"}), ""
        return 0, json.dumps([{"workflow_runs": []}]), ""
    monkeypatch.setattr(gate, "_run", runner)
    assert gate.main(["gate", "--pr", "1", "--repo", SLUG, "--merge"]) == 1
    assert not any("PUT" in c for c in calls)


def test_cli_verified_receipt_reaches_atomic_merge_and_records_cycle(monkeypatch, tmp_path):
    run, jobs = receipts()
    run["updated_at"] = datetime.now(timezone.utc).isoformat()
    calls = []
    def runner(cmd):
        calls.append(cmd)
        if cmd[1:3] == ["pr", "view"]:
            return 0, json.dumps({"files": [{"path": "docs/note.md", "additions": 1}],
                                 "headRefOid": HEAD, "headRefName": "evolution/issue-3467"}), ""
        if "PUT" in cmd:
            return 0, '{"merged": true}', ""
        return runner_for(run, jobs)(cmd)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(gate, "_run", runner)
    assert gate.main(["gate", "--pr", "1", "--repo", SLUG, "--merge"]) == 0
    assert any("PUT" in c and f"sha={HEAD}" in c for c in calls)
    with sqlite3.connect(tmp_path / "evolution" / "admission.sqlite3") as db:
        row = db.execute("SELECT cycle, receipt FROM admissions").fetchone()
    assert row[0] == "evolution/issue-3467" and json.loads(row[1])["run_id"] == run["id"]


def test_admission_enforcement_cannot_self_modify():
    assert any("HIGH_RISK_PATH" in error for error in gate.check_merge_policy(
        [{"path": "scripts/evolution_admission.py", "additions": 1}]))


@pytest.mark.parametrize("path,status", [("tests/test_old.py", "modified"),
    ("tests/fixture.json", "removed"), ("tests/conftest.py", "added"),
    ("scripts/run_tests_parallel.py", "modified")])
def test_candidate_cannot_rewrite_independent_validation(path, status):
    run, jobs = receipts()
    def runner(cmd):
        if "/compare/" in cmd[-1]:
            return 0, json.dumps([{"files": [{"filename": path, "status": status}]}]), ""
        return runner_for(run, jobs)(cmd)
    assert verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG, runner, now=NOW)[1]


def test_skipped_test_step_cannot_be_hidden_by_successful_job():
    run, jobs = receipts()
    jobs[-1]["steps"][0]["conclusion"] = "skipped"
    assert verify_receipt([{"path": "tools/x.py"}], HEAD, SLUG, runner_for(run, jobs), now=NOW)[1]
