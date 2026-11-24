"""#310: the cron model-drift guard must be a deterministic, auditable function.

Field defect: job ``aa0205764697`` ran 53 times in one window with an unchanged
``model_snapshot`` and unchanged config — 10 ticks hard-failed with
``RuntimeError: [drift_skip] ...`` and 43 ran normally, and ``cron_incidents``
held zero rows for the job.  Two properties were missing:

* the skip verdict was not a function of ``(snapshot, resolved config)``: the
  "current" operand was re-derived per tick from tick-volatile sources (the
  process environment, and a mid-tick fallback to a backup provider/model), so a
  transient provider blip could manufacture a "you drifted" skip;
* a skip left no audit trail at all, and the ``[drift_skip:silent]`` variant
  suppressed delivery, so lost ticks were invisible.

These tests drive the REAL path (``run_job`` / ``run_one_job`` inside the
scheduler, with the job store and the executions/incidents ledger redirected to
a temp profile) and assert the ruling, the incident row, and the recorded
values on the execution row.  No live host state is consulted.
"""

import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

import cron.executions as cron_executions
import cron.jobs as cron_jobs
import cron.scheduler as sched
from cron import drift_guard


JOB_ID = "drift-guard-310"


def _job(**overrides):
    job = {
        "id": JOB_ID,
        "name": "drift guard 310",
        "prompt": "hello",
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 15, "display": "every 15m"},
        "deliver": "local",
        "model": None,
        "provider": None,
        "provider_snapshot": "openrouter",
        "base_url": None,
    }
    job.update(overrides)
    return job


def _write_config(tmp_path, *, model="test-model", provider=None, fallback=False):
    lines = ["model:"]
    if provider:
        lines.append(f"  provider: {provider}")
    lines.append(f"  default: {model}")
    if fallback:
        lines.append("fallback_providers:")
        lines.append("  - provider: nous")
        lines.append("    model: fb-model")
    (tmp_path / "config.yaml").write_text("\n".join(lines) + "\n")


def _resolver(*, primary_fails=False):
    """resolve_runtime_provider stand-in with a controllable primary failure."""

    def fake(**kwargs):
        requested = str(kwargs.get("requested") or "").strip().lower()
        if requested == "nous":
            return {
                "api_key": "test-key",
                "base_url": "https://backup.invalid/v1",
                "provider": "nous",
                "api_mode": "chat_completions",
            }
        if primary_fails:
            from hermes_cli.auth import AuthError

            raise AuthError("403 invalid api key")
        return {
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "provider": "openrouter",
            "api_mode": "chat_completions",
        }

    return fake


def _tick(tmp_path, *, primary_fails=False, deliveries=None, job=None):
    """One real scheduler tick against a temp profile. Returns whether the agent ran."""
    deliveries = deliveries if deliveries is not None else []
    fake_db = MagicMock()

    def fake_deliver(jb, content, adapters=None, loop=None, **kwargs):
        deliveries.append(content)
        return None

    stored = job if job is not None else _stored_job(tmp_path)
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler._get_hermes_home", return_value=tmp_path), \
         patch("cron.scheduler._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state.SessionDB", return_value=fake_db), \
         patch("tools.mcp_tool_discovery.discover_mcp_tools", return_value=[]), \
         patch("hermes_cli.fallback_config.resolve_entry_api_key", return_value="test-key"), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider",
               side_effect=_resolver(primary_fails=primary_fails)), \
         patch.object(sched, "_deliver_result", side_effect=fake_deliver), \
         patch("run_agent.AIAgent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent_cls.return_value = mock_agent
        sched.run_one_job(stored)
    return mock_agent_cls.called


def _stored_job(tmp_path, job_id=JOB_ID):
    return dict([j for j in cron_jobs.load_jobs() if j["id"] == job_id][0])


def _db(tmp_path):
    return sqlite3.connect(tmp_path / "cron" / "executions.db")


def _executions(tmp_path):
    conn = _db(tmp_path)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM executions WHERE job_id=? ORDER BY claimed_at, id", (JOB_ID,)
            ).fetchall()
        ]
    finally:
        conn.close()


def _incidents(tmp_path):
    conn = _db(tmp_path)
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM cron_incidents WHERE job_id=?", (JOB_ID,)
            ).fetchall()
        ]
    finally:
        conn.close()


@pytest.fixture()
def profile(tmp_path, monkeypatch):
    """Temp profile: job store, executions ledger and incident table all local."""
    monkeypatch.setattr(cron_executions, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return tmp_path


# ── The decision itself is pure ───────────────────────────────────────────────


def test_verdict_is_a_pure_function_of_snapshot_and_config(monkeypatch):
    """The same snapshot + same resolved config must rule the same way every time.

    This is the property #310 found missing (10 skips / 43 runs). ``evaluate``
    reads no environment, clock or runtime state, so mutating the environment
    between calls cannot flip the verdict.
    """
    job = _job(model_snapshot="test-model", provider_snapshot=None)
    config = {"model": {"default": "test-model", "provider": "openrouter"}}
    baseline = drift_guard.resolve_baseline(config, environ={})

    first = drift_guard.evaluate(job, config=config, baseline=baseline)
    monkeypatch.setenv("HERMES_MODEL", "some-other-model")
    second = drift_guard.evaluate(job, config=config, baseline=baseline)

    assert first.verdict == second.verdict == drift_guard.VERDICT_MATCH
    assert first.detail_json() == second.detail_json()
    assert first.drifted is False


def test_matching_snapshot_never_drifts_and_unknown_baseline_is_indeterminate():
    config = {"model": {"default": "test-model", "provider": "openrouter"}}
    identical = drift_guard.evaluate(
        _job(model_snapshot="test-model", provider_snapshot=None),
        config=config,
        baseline=drift_guard.resolve_baseline(config, environ={}),
    )
    assert identical.verdict == drift_guard.VERDICT_MATCH
    assert identical.changes == ""

    # No baseline value at all -> the guard cannot prove drift, so it must not
    # claim it (a skip has to be provable to be legitimate).
    unknown = drift_guard.evaluate(
        _job(model_snapshot="test-model", provider_snapshot=None),
        config=config,
        baseline=drift_guard.Baseline(),
    )
    assert unknown.verdict == drift_guard.VERDICT_INDETERMINATE
    assert unknown.drifted is False


# ── Real path: a tick-volatile fallback walk must not fake a skip ─────────────


def test_provider_fallback_tick_does_not_manufacture_a_drift_skip(profile, monkeypatch):
    """A transient primary-provider failure is not global-config drift.

    Before #310 the guard compared the snapshot against the fallback-walk
    runtime (``setup.runtime['provider']``), so every tick where the primary
    resolve raised reported ``provider 'openrouter' -> 'nous'`` and hard-skipped
    the run. Same snapshot, same config, different verdict from tick to tick.
    """
    _write_config(profile, model="test-model", fallback=True)
    with cron_jobs.use_cron_store(profile):
        cron_jobs.save_jobs([_job()])

        healthy = []
        assert _tick(profile, deliveries=healthy) is True, "healthy tick must run"

        degraded = []
        ran = _tick(profile, primary_fails=True, deliveries=degraded)

    assert ran is True, "a fallback tick must run on its snapshot, not fake a drift skip"
    assert not any("[drift_skip]" in str(d) for d in degraded)

    rows = _executions(profile)
    assert len(rows) == 2
    details = [json.loads(r["drift_detail"]) for r in rows]
    assert all(d["axes"].get("provider", {}).get("drifted") is False for d in details)
    assert _incidents(profile) == []


# ── Real path: genuine drift skips, records one incident, records both values ──


def test_genuine_drift_skips_and_is_auditable(profile, monkeypatch):
    _write_config(profile, model="new-model")
    with cron_jobs.use_cron_store(profile):
        cron_jobs.save_jobs([_job(model_snapshot="old-model", provider_snapshot=None)])

        first = []
        assert _tick(profile, deliveries=first) is False, "drifted job must not spend"

        incidents = _incidents(profile)
        assert len(incidents) == 1, "a drift skip must open exactly one incident row"
        assert incidents[0]["state"] == "alerted"
        assert "drifted" in incidents[0]["error"]

        rows = _executions(profile)
        assert len(rows) == 1
        assert rows[0]["status"] == "failed"
        assert "[drift_skip]" in rows[0]["error"]
        detail = json.loads(rows[0]["drift_detail"])
        assert detail["verdict"] == "drift"
        assert detail["axes"]["model"] == {
            "snapshot": "old-model",
            "current": "new-model",
            "drifted": True,
        }
        # Both compared values are on the execution row, not only in a WARNING.
        assert "old-model" in rows[0]["error"] and "new-model" in rows[0]["error"]

        assert len(first) == 1, "the first skip of a change is delivered"
        stored = _stored_job(profile)
        assert stored["last_status"] == "drift_skip"

        # Second tick of the SAME drift: deduped (silent), but still auditable.
        second = []
        assert _tick(profile, deliveries=second) is False
        assert len(_incidents(profile)) == 1, "same drift refreshes one incident, not 53"
        assert second == [], "a repeat of the same drift does not re-ping"
        rows = _executions(profile)
        assert len(rows) == 2
        assert all("[drift_skip" in r["error"] for r in rows)
        assert all(json.loads(r["drift_detail"])["verdict"] == "drift" for r in rows)

        # A *changed* drift is a new change: it alerts again.
        cron_jobs.save_jobs([dict(_stored_job(profile), model_snapshot="older-model")])
        third = []
        assert _tick(profile, deliveries=third) is False
        assert len(third) == 1, "drift that changed must re-alert"
        assert len(_incidents(profile)) == 2


def test_passing_tick_records_the_comparison(profile):
    """Even a tick that runs leaves both compared values behind for diffing."""
    _write_config(profile, model="test-model", provider="openrouter")
    with cron_jobs.use_cron_store(profile):
        cron_jobs.save_jobs([_job(model_snapshot="test-model")])
        assert _tick(profile) is True

    rows = _executions(profile)
    assert len(rows) == 1
    detail = json.loads(rows[0]["drift_detail"])
    assert detail["verdict"] == "match"
    assert detail["axes"]["model"]["snapshot"] == "test-model"
    assert detail["axes"]["model"]["current"] == "test-model"
    assert detail["config_loaded"] is True
    assert _incidents(profile) == []
