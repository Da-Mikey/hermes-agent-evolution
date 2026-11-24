#!/usr/bin/env python3
"""Local triage pass for evolution-analysis (#783).

Reads local sidecar files (issues/, introspection/, research/) and
produces a thin-list triage JSON in the standard analysis format —
WITHOUT any GitHub API calls or private-tool dispatch.

This makes the analysis stage independently runnable: when private
tools are unavailable, the local triage still produces output so the
pipeline is not blind.

Usage:
    python scripts/evolution_local_triage.py [--evolution-dir DIR]

Output:
    Writes analysis/YYYY-MM-DD.json to the evolution directory.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from evolution.lib.stage_gate import decide as _gate_decide
    from evolution.lib.stage_gate import record_decision as _gate_record
    from evolution.lib.stage_result import StageResult

    _HAS_STAGE_RESULT = True
except ImportError:  # pragma: no cover - exercised by the standalone-copy tests
    # This script runs standalone from the access gate and from cron, where the
    # `evolution` package is frequently not importable (the gate copies just
    # this file into a working directory; see #1304/#1314 for the same lesson in
    # the harvest cron). Triage output is the contract here — the StageResult
    # tuple is additive telemetry, so its absence must not stop the file from
    # being written.
    _HAS_STAGE_RESULT = False

try:
    from evolution.lib.telemetry_guard import validate_health_text

    _HAS_TELEMETRY_GUARD = True
except ImportError:  # pragma: no cover - standalone copy without the guard
    _HAS_TELEMETRY_GUARD = False

# Sidecar subdirectories to scan
_SIDECAR_DIRS = ("issues", "introspection", "research")


def _latest_file(directory: Path) -> Path | None:
    """Return the most recently modified .json or .md file in a directory."""
    if not directory.is_dir():
        return None
    candidates = sorted(
        (f for f in directory.iterdir() if f.suffix in (".json", ".md")),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


def _read_sidecar(path: Path) -> dict:
    """Read a sidecar file (JSON or MD) and return a dict with metadata."""
    if path.suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"path": str(path), "error": "unreadable"}
        return {"path": str(path), "data": data}
    # MD files — just note existence (research reports are free-form)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {"path": str(path), "error": "unreadable"}
    return {"path": str(path), "char_count": len(text)}


_UNKNOWN_EFFORT_MAX_SELECTION = 4
"""Cap for selections whose effort the sidecar does not record.

``filed`` items in an issues sidecar carry a ``priority_score`` but no effort
figure (the sidecar's ``budget`` is a count of issues, not effort), so an
unknown-effort candidate enters the effort loop with ``effort_score = 0.0`` and
would never trip the budget cap. The implementation stage documents a maximum of
four issues per run; that is policy, not a guess, so it is the cap used here.
"""


def _proposals_from_filed_list(issues_document: dict) -> list[dict]:
    """Map the top-level ``filed[]`` list that issues passes actually write.

    ``issues_document`` is the parsed sidecar (``_read_sidecar`` wraps it under
    ``data``). Every issues sidecar under ``evolution/issues/`` stores its
    output as a top-level ``filed`` array of ``{letter, number, url, title,
    labels, source, hook, priority_score, why_it_survived}``; none of them
    carries the ``proposals`` container this module was originally written
    against (verified against 2026-10-21, 2026-10-25 and 2026-10-26-b). Reading
    only that container made every fallback cycle select zero issues while still
    emitting a schema-valid report — the exact silent-empty failure the local
    pass exists to prevent (#308).

    The mapping is lossless about what the sidecar records: effort is reported
    as unknown rather than invented, and the caller caps unknown-effort
    selections accordingly.
    """
    out = []
    for item in issues_document.get("filed") or []:
        if not isinstance(item, dict):
            continue
        number = item.get("number") or item.get("issue")
        if not number:
            continue
        out.append({
            "issue_number": number,
            "title": item.get("title", ""),
            "priority_score": item.get("priority_score", 0.0),
            "impact_score": item.get("impact", 0.0),
            "effort_score": 0.0,
            "effort_unknown": True,
            "category": item.get("category", ""),
            "selected_reason": "local-triage-filed-sidecar",
            "already_implemented": False,
            "status": "open",
            "reason": item.get("source", ""),
            "notes": item.get("why_it_survived", ""),
            "claimed_artifact": item.get("hook", ""),
        })
    return out


def _extract_proposals(
    issues_sidecar: dict, repo_root: Optional[Path] = None
) -> list[dict]:
    """Extract proposals from issues sidecar and reconcile already_implemented claims (#2468/#2494)."""
    data = issues_sidecar.get("data", {})
    proposals = data.get("proposals", [])
    raw_candidates = []
    for p in proposals:
        decision = str(p.get("decision") or "").lower()
        is_filed = decision == "filed" and p.get("issue")
        is_already_impl = (
            decision == "already_implemented"
            or bool(p.get("already_implemented"))
            or str(p.get("status") or "").lower() == "already_implemented"
        )
        if (is_filed or is_already_impl) and p.get("issue"):
            raw_candidates.append({
                "issue_number": p["issue"],
                "title": p.get("title", ""),
                "priority_score": p.get("priority_score", 0.0),
                "impact_score": p.get("impact", 0.0),
                "effort_score": p.get("effort", 0.0),
                "category": p.get("category", ""),
                "selected_reason": "local-triage",
                "already_implemented": is_already_impl,
                "status": "already_implemented" if is_already_impl else "open",
                "reason": p.get("reason", ""),
                "notes": p.get("notes", ""),
                "claimed_artifact": p.get("claimed_artifact", ""),
            })

    # #308: the `data.proposals` container is not the shape issues passes write.
    # Fall back to the top-level `filed[]` list rather than returning an empty
    # selection and calling it a quiet day.
    if not proposals:
        raw_candidates = _proposals_from_filed_list(data)

    # Wire verification into candidate selection pipeline (#2494)
    if repo_root is not None:
        try:
            from scripts.evolution_analysis_audit import (
                reconcile_implemented_candidates,
            )

            raw_candidates = reconcile_implemented_candidates(raw_candidates, repo_root)
        except ImportError:
            try:
                from evolution_analysis_audit import (
                    reconcile_implemented_candidates,
                )

                raw_candidates = reconcile_implemented_candidates(
                    raw_candidates, repo_root
                )
            except ImportError:
                pass

    filed = []
    for cand in raw_candidates:
        if not cand.get("already_implemented"):
            if cand.get("already_implemented_unverified"):
                cand["selected_reason"] = "reconciled-unverified-implementation"
            filed.append(cand)
    return filed


def _read_calibration(evolution_dir: Path) -> dict:
    """Read health and realized-impact sidecars for calibration."""
    cal = {"effort_budget": 3.0, "consolidation_mode": False}

    health_file = evolution_dir / "evolution-health.txt"
    if health_file.exists():
        text = health_file.read_text(encoding="utf-8")
        # #2637: fail closed on tampered steering telemetry.
        if _HAS_TELEMETRY_GUARD and not validate_health_text(text).safe:
            text = ""
        for token in ("effort_budget=1.5", "effort_budget=1.5"):
            if token in text:
                cal["effort_budget"] = 1.5
                break
        # Check for LOW_SELECTION_EFFICIENCY
        if "LOW_SELECTION_EFFICIENCY" in text:
            cal["effort_budget"] = 1.5

    realized_file = evolution_dir / "realized-impact.txt"
    if realized_file.exists():
        text = realized_file.read_text(encoding="utf-8")
        if "REALIZED_IMPACT_LOW" in text:
            cal["consolidation_mode"] = True

    return cal


def _resolve_repo_root(repo_dir: Optional[Path] = None) -> Optional[Path]:
    if repo_dir and repo_dir.is_dir():
        return repo_dir
    import os

    env_repo = os.environ.get("EVOLUTION_REPO_DIR")
    if env_repo and Path(env_repo).is_dir():
        return Path(env_repo)
    candidate = Path(__file__).resolve().parent.parent
    if (candidate / ".git").exists() or (candidate / "run_agent.py").exists():
        return candidate
    return None


def run_local_triage(evolution_dir: Path, repo_root: Optional[Path] = None) -> dict:
    """Run the local triage pass and return the analysis JSON dict."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    resolved_repo = _resolve_repo_root(repo_root)

    # Read latest sidecars
    sidecars = {}
    for subdir in _SIDECAR_DIRS:
        latest = _latest_file(evolution_dir / subdir)
        if latest:
            sidecars[subdir] = _read_sidecar(latest)

    # Extract proposals from issues sidecar
    proposals = []
    if "issues" in sidecars and "data" in sidecars["issues"]:
        proposals = _extract_proposals(sidecars["issues"], repo_root=resolved_repo)

    # Read calibration
    cal = _read_calibration(evolution_dir)

    # Sort by priority score (descending)
    proposals.sort(key=lambda p: p["priority_score"], reverse=True)

    # Apply effort budget cap
    max_effort = cal["effort_budget"]
    selected = []
    total_effort = 0.0
    unknown_effort_selected = 0
    for p in proposals:
        if total_effort + p["effort_score"] > max_effort:
            continue
        # #308: a candidate whose effort the sidecar never recorded costs 0.0
        # against the budget, so the effort cap alone cannot bound the list.
        # Bound it by the documented per-run maximum instead.
        if p.get("effort_unknown"):
            if unknown_effort_selected >= _UNKNOWN_EFFORT_MAX_SELECTION:
                continue
            unknown_effort_selected += 1
        selected.append(p)
        total_effort += p["effort_score"]

    # Build output in standard analysis format
    output = {
        "date": today,
        "local_triage": True,
        "sidecars_read": {k: v.get("path", "") for k, v in sidecars.items()},
        "effort_budget": max_effort,
        "consolidation_mode": cal["consolidation_mode"],
        "rejected": [],
        "selected_for_implementation": selected,
    }

    # #308: make a fallback cycle visible in the artifact. The stage gate cannot
    # tell a lost cycle from a quiet one (confidence 50 / branch `refine` is
    # emitted on every triage run), so a selection read from `filed[]` says so
    # here instead of looking like a normal, empty day.
    if any(p.get("selected_reason") == "local-triage-filed-sidecar" for p in selected):
        output["degraded"] = True
        output["degraded_reason"] = (
            "issues sidecar carried no data.proposals — selection was read from "
            "the top-level filed[] list; per-issue effort is not recorded there, "
            f"so selections with unknown effort were capped at "
            f"{_UNKNOWN_EFFORT_MAX_SELECTION} (#308)"
        )

    # Run claim audit on output (#2494)
    if resolved_repo is not None:
        try:
            from scripts.evolution_analysis_audit import audit_implemented_claims

            claim_warnings = audit_implemented_claims(output, resolved_repo)
            if claim_warnings:
                output["unverified_implementation_warnings"] = claim_warnings
        except ImportError:
            try:
                from evolution_analysis_audit import audit_implemented_claims

                claim_warnings = audit_implemented_claims(output, resolved_repo)
                if claim_warnings:
                    output["unverified_implementation_warnings"] = claim_warnings
            except ImportError:
                pass

    # Emit a StageResult at this boundary (AREX #1338 slice A).
    #
    # The envelope is attached to the same dict it describes, so its ``result``
    # field is dropped here: keeping it would make output["stage_result"]
    # ["result"] point back at output, and json.dumps raises
    # "Circular reference detected" on exactly that. Dropping it also avoids
    # serializing the whole triage payload twice. The rest of the tuple —
    # evidence pointers, confidence, stage, timestamp — is what a consumer of
    # this boundary actually needs; the result itself is the document it is
    # attached to.
    #
    # Skipped when the evolution package is not importable (see the import
    # guard above) — the triage output is the contract, this is telemetry.
    if _HAS_STAGE_RESULT:
        evidence = list(output["sidecars_read"].values())
        stage_result = StageResult.wrap(
            result=None,
            evidence_pointers=[p for p in evidence if p],
            confidence=50,
            stage="local_triage",
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        envelope = stage_result.to_dict()
        envelope.pop("result", None)
        output["stage_result"] = envelope

        # Consume the tuple through the Accept/Refine/Restart gate (#1339).
        # Advisory at this boundary: local triage is a read-only pre-pass whose
        # output the analysis stage consumes, so the gate records which branch
        # the boundary lands in rather than aborting the pass. Confidence 50
        # (evidence present, no LLM verification) sits below the conservative
        # default of 70, so a triage run with sidecars lands in `refine` and one
        # with none lands in `restart` — both surfaced for the next stage to act
        # on instead of being silently treated as a confident result.
        decision = _gate_decide(stage_result)
        output["stage_gate"] = decision.to_dict()
        # Persist for the per-boundary rate metrics (#1340). Fire-and-forget:
        # record_decision swallows IO errors so a metrics write can never take
        # the triage pass down.
        _gate_record(evolution_dir / "stage_gate.jsonl", decision)
    return output


def _load_check_effort_budget_shape():
    """Resolve the producer-side shape check without import fragility.

    The gate copies this script (sometimes alone, sometimes with the audit
    module) into an isolated working dir — see
    test_missing_access_gate_fails_closed. ``scripts.evolution_analysis_audit``
    is unresolvable there, so fall back to a bare import, then to loading the
    module by path next to this file. Returns None when the module is
    genuinely unavailable; the caller must degrade to a stderr warning and
    still write the report (#168) — a missing check must never abort triage.
    """
    try:
        from scripts.evolution_analysis_audit import check_effort_budget_shape

        return check_effort_budget_shape
    except ImportError:
        pass
    try:
        from evolution_analysis_audit import check_effort_budget_shape

        return check_effort_budget_shape
    except ImportError:
        pass
    try:
        import importlib.util

        audit_path = Path(__file__).resolve().parent / "evolution_analysis_audit.py"
        if audit_path.is_file():
            spec = importlib.util.spec_from_file_location(
                "evolution_analysis_audit", audit_path
            )
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module.check_effort_budget_shape
    except Exception:  # pragma: no cover - degraded install; any failure is fine
        pass
    return None


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Local triage pass (no GitHub API calls)"
    )
    parser.add_argument(
        "--evolution-dir",
        default=None,
        help="Path to the evolution directory (default: auto-detect from HERMES_HOME)",
    )
    args = parser.parse_args(argv)

    if args.evolution_dir:
        evolution_dir = Path(args.evolution_dir)
    else:
        import os

        hermes_home = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
        evolution_dir = Path(hermes_home) / "evolution"

    if not evolution_dir.is_dir():
        print(f"Error: evolution directory not found: {evolution_dir}", file=sys.stderr)
        return 1

    output = run_local_triage(evolution_dir)

    # Write to analysis/YYYY-MM-DD.json
    analysis_dir = evolution_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    output_path = analysis_dir / f"{output['date']}.json"

    # Don't overwrite if a full (non-local) analysis already exists for today
    if output_path.exists():
        existing = json.loads(output_path.read_text(encoding="utf-8"))
        if not existing.get("local_triage", False):
            # A real analysis exists — don't clobber it with a local-only pass
            print(
                f"Skipping: full analysis already exists at {output_path}",
                file=sys.stderr,
            )
            return 0

    # Producer-side schema check (#168): fail loudly AT WRITE TIME if the
    # report shape would crash a consumer later (the watchdog's meta-skill
    # trace died with TypeError: float() got 'dict' because a producer shipped
    # a nested effort_budget the consumer couldn't read). The audit module is
    # the single source of truth for the shape contract. A missing module
    # (isolated/degraded install — the gate copies this script alone) must
    # degrade to a warning, never abort the write: Phase 0 output is the
    # gate's core guarantee.
    check_shape = _load_check_effort_budget_shape()
    shape_violations = []
    if check_shape is None:
        print(
            "Warning: evolution_analysis_audit unavailable — skipping producer "
            "shape check; report still written (#168).",
            file=sys.stderr,
        )
    else:
        shape_violations = check_shape(output)
    if shape_violations:
        for v in shape_violations:
            print(f"REPORT_SHAPE_VIOLATION: {v}", file=sys.stderr)
        print(
            "Refusing to write an analysis report whose shape would crash "
            "consumers — fix the producer before it lands.",
            file=sys.stderr,
        )
        return 1

    output_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Local triage written to {output_path}")
    print(f"  Sidecars read: {', '.join(output['sidecars_read'].keys())}")
    print(f"  Selected: {len(output['selected_for_implementation'])} issues")
    print(f"  Effort budget: {output['effort_budget']}")
    gate = output.get("stage_gate")
    if gate:
        print(
            f"[stage-gate] {gate['stage']}: {gate['branch'].upper()} "
            f"(confidence={gate['confidence']}, threshold={gate['threshold']}) — {gate['reason']}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
