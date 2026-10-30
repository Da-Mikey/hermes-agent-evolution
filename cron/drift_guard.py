"""Deterministic, auditable cron model-drift decisions (#310).

The #44585 guard fails closed when an unpinned job's stored provider/model
snapshot no longer matches the global inference assignment.  The field defect
behind #310 was that the *same* job at the *same* stored snapshot skipped on 10
of 53 ticks of one window while the other 43 ran: the "current" operand was
re-derived per tick from sources that are not part of the drift question at all
— the process environment, the runtime-provider resolution (including a
mid-tick fallback to a backup provider/model), and a swallowed config-read
failure that substituted an env value.  Two of the skips also used the
``[drift_skip:silent]`` variant, and *no* skip left a ``cron_incidents`` row,
so a job could lose a third of its ticks without any audit trail.

This module makes the decision a pure function of ``(stored snapshot, resolved
config)`` and gives it an audit seam:

* :func:`resolve_baseline` resolves the global assignment ONCE per run from the
  loaded config mapping.  The caller may pass values resolved earlier in the run
  (``resolved_provider``/``resolved_model``) but must NOT pass tick-volatile
  values such as a fallback-walk runtime — the scheduler deliberately passes
  ``""`` for those, so a transient provider/auth blip can no longer manufacture
  a "you drifted" verdict.
* :func:`evaluate` compares the snapshot against that baseline.  It reads no
  environment, no clock and no runtime state, so identical inputs always yield
  an identical verdict.  An axis whose current value cannot be established is
  reported as ``indeterminate`` — never as drift — because a skip has to be
  provable to be legitimate.
* :func:`record_observation` records the compared values for EVERY tick the
  guard rules on (execution row + log), so a passing tick and a skipping tick
  can be diffed afterwards.
* :func:`record_skip` opens/refreshes a ``cron_incidents`` row for a skip and
  reports whether the operator was already alerted for this exact change, so a
  skip is never invisible and a *new* change re-alerts.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

VERDICT_DRIFT = "drift"
VERDICT_MATCH = "match"
VERDICT_INDETERMINATE = "indeterminate"

AXES: Tuple[str, ...] = ("provider", "model")

# A "current" value that can never equal a stored snapshot: asking
# ``cron_model_drift_axes`` with it enumerates every axis the guard could ever
# rule on (snapshot present, axis not covered by a per-job pin or fleet default)
# without duplicating that exclusion rule here.
_PROBE_CURRENT = "\x00drift-guard-probe"

_MAX_DETAIL_CHARS = 900


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()


@dataclass(frozen=True)
class Baseline:
    """The global inference assignment a stored snapshot is compared against."""

    provider: str = ""
    model: str = ""
    source: str = "unknown"

    @property
    def provider_known(self) -> bool:
        return bool(self.provider)

    @property
    def model_known(self) -> bool:
        return bool(self.model)


@dataclass(frozen=True)
class AxisComparison:
    """One axis of the comparison: what the job stored vs what config says now."""

    axis: str
    snapshot: str
    current: str
    drifted: bool

    @property
    def text(self) -> str:
        return f"{self.axis} '{self.snapshot}' -> '{self.current}'"


@dataclass(frozen=True)
class DriftVerdict:
    """The guard's ruling for one tick. Pure data — safe to record and re-check."""

    verdict: str
    axes: Tuple[AxisComparison, ...] = ()
    unknown_axes: Tuple[str, ...] = ()
    baseline_source: str = "unknown"

    @property
    def drifted(self) -> bool:
        return self.verdict == VERDICT_DRIFT

    @property
    def drifted_axes(self) -> Tuple[str, ...]:
        return tuple(a.axis for a in self.axes if a.drifted)

    @property
    def changes(self) -> str:
        """The human remediation detail (unchanged #44585 wording)."""
        return "; ".join(a.text for a in self.axes if a.drifted)

    @property
    def signature(self) -> str:
        """Stable identity of this *change* — a new change must re-alert."""
        return self.changes

    def observation(self, *, config_loaded: bool = True) -> Dict[str, Any]:
        return {
            "verdict": self.verdict,
            "baseline": self.baseline_source,
            "config_loaded": bool(config_loaded),
            "axes": {
                a.axis: {"snapshot": a.snapshot, "current": a.current, "drifted": a.drifted}
                for a in self.axes
            },
            "unknown_axes": list(self.unknown_axes),
        }

    def detail_json(self, *, config_loaded: bool = True) -> str:
        """Bounded single-line JSON for the execution row's ``drift_detail``."""
        blob = json.dumps(self.observation(config_loaded=config_loaded), sort_keys=True)
        return blob[:_MAX_DETAIL_CHARS]


def resolve_baseline(
    config: Any,
    *,
    resolved_provider: Any = "",
    resolved_model: Any = "",
    environ: Optional[Dict[str, str]] = None,
) -> Baseline:
    """Resolve the global ``(provider, model)`` the guard compares snapshots against.

    Persisted config wins.  ``resolved_provider``/``resolved_model`` are only a
    fallback for installs that configure the assignment outside ``config.yaml``,
    and callers must leave them empty when the values came from a tick-volatile
    path (a fallback-walk runtime, a lazily derived provider) — that variance is
    exactly what #310 saw as a non-deterministic skip verdict.
    """
    cfg_provider = cfg_model = ""
    try:
        from hermes_cli.config import resolve_cron_model_drift_defaults

        cfg_provider, cfg_model = resolve_cron_model_drift_defaults(config, environ=environ)
    except Exception as exc:  # pragma: no cover - defensive: config helpers never raise for us
        logger.debug("Drift baseline: config resolution failed (%s)", exc)

    provider = _norm(cfg_provider) or _norm(resolved_provider)
    model = _norm(cfg_model) or _norm(resolved_model)
    if _norm(cfg_provider) or _norm(cfg_model):
        source = "config"
    elif provider or model:
        source = "resolved"
    else:
        source = "unknown"
    return Baseline(provider=provider, model=model, source=source)


def evaluate(
    job: Any, *, config: Any = None, baseline: Optional[Baseline] = None
) -> DriftVerdict:
    """Rule on drift for *job*.  A pure function of the snapshot and *baseline*.

    Reads no environment, clock, filesystem or runtime state: the same job dict
    and baseline always produce the same verdict, which is the property #310
    found missing (10 skips / 43 runs on an identical snapshot).
    """
    from hermes_cli.config import cron_model_drift_axes

    baseline = baseline or Baseline()
    if not isinstance(job, dict):
        return DriftVerdict(VERDICT_MATCH, (), (), baseline.source)

    current = {"provider": baseline.provider, "model": baseline.model}
    try:
        drifted_axes = set(
            cron_model_drift_axes(
                job,
                current_provider=baseline.provider,
                current_model=baseline.model,
                config=config,
            )
        )
        candidate_axes = set(
            cron_model_drift_axes(
                job,
                current_provider=_PROBE_CURRENT,
                current_model=_PROBE_CURRENT,
                config=config,
            )
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Drift evaluation unavailable for job %s: %s", job.get("id"), exc)
        return DriftVerdict(VERDICT_INDETERMINATE, (), AXES, baseline.source)

    comparisons = []
    unknown = []
    for axis in AXES:
        if axis not in candidate_axes:
            continue
        snapshot = _norm(job.get(f"{axis}_snapshot"))
        comparisons.append(
            AxisComparison(
                axis=axis,
                snapshot=snapshot,
                current=current[axis],
                drifted=axis in drifted_axes,
            )
        )
        if not current[axis]:
            unknown.append(axis)

    if drifted_axes:
        verdict = VERDICT_DRIFT
    elif unknown:
        verdict = VERDICT_INDETERMINATE
    else:
        verdict = VERDICT_MATCH
    return DriftVerdict(verdict, tuple(comparisons), tuple(unknown), baseline.source)


def record_observation(
    job: Dict[str, Any], verdict: DriftVerdict, *, config_loaded: bool = True
) -> Optional[str]:
    """Record the compared values for this tick (log + execution row). Best effort."""
    detail = verdict.detail_json(config_loaded=config_loaded)
    job_id = (job or {}).get("id")
    if verdict.drifted:
        logger.warning("Job '%s': drift guard ruled drift: %s", job_id, detail)
    else:
        logger.info("Job '%s': drift guard ruled %s: %s", job_id, verdict.verdict, detail)
    execution_id = (job or {}).get("execution_id")
    if not execution_id:
        return None
    try:
        from cron.executions import record_drift_detail

        record_drift_detail(str(execution_id), detail)
        return detail
    except Exception as exc:
        logger.debug("Drift detail not recorded for job %s: %s", job_id, exc)
        return None


def skip_body(verdict: DriftVerdict, remediation: str = "") -> str:
    """The operator-facing skip text (marker-free; the caller adds the marker)."""
    body = (
        "Skipped to prevent unintended spend: global inference config drifted "
        f"since this job was created ({verdict.changes}), and this job is "
        "unpinned. No inference call was made."
    )
    if remediation:
        body += f" {remediation}"
    return body


def record_skip(
    job: Dict[str, Any], verdict: DriftVerdict, *, remediation: str = ""
) -> Tuple[bool, Optional[str]]:
    """Open/refresh the incident for this skip.

    Returns ``(already_alerted_for_this_change, incident_id)``.  A skip is a
    failed run the operator must be able to see: every skip leaves a
    ``cron_incidents`` row (deduped on the change text, so consecutive ticks of
    the same drift refresh one row instead of minting 53).  The first skip of a
    given change delivers; later ticks of the *same* change stay silent; a
    changed drift is a new signature and alerts again.
    """
    job_id = str((job or {}).get("id") or "")
    body = skip_body(verdict, remediation)
    incident_id: Optional[str] = None
    is_new = True
    try:
        from cron.incidents import get_incident, upsert_incident

        incident_id, is_new = upsert_incident(
            job_id, body, job_name=(job or {}).get("name")
        )
        state = (get_incident(incident_id) or {}).get("state")
    except Exception as exc:
        logger.debug("Incident store unavailable for drift skip on %s: %s", job_id, exc)
        state = None

    prior_alert = False
    try:
        from cron.jobs import mark_drift_alerted

        prior_alert = mark_drift_alerted(job_id)
    except Exception:
        pass  # fail open: better a duplicate alert than none
    (job or {})["drift_alerted"] = True

    already_alerted = bool(prior_alert) and not is_new and state != "resolved"
    if not already_alerted and incident_id:
        # We are about to deliver this change; remember that so the next tick of
        # the same drift is silent while the row stays auditable.
        try:
            from cron.incidents import set_incident_state

            set_incident_state(incident_id, "alerted")
        except Exception:
            pass
    return already_alerted, incident_id
