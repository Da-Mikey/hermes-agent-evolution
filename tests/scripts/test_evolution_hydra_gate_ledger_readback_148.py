# -*- coding: utf-8 -*-
"""Consumer-side tests for the Hydra ledger reader (issue #148, slice A+).

Slice A shipped ``scripts/orchbench_ledger.py`` and was bounced for dead code:
nothing read it. The rework brief and the follow-up split both ask for exactly
one caller — the gate that writes the ledger reads the tick it just wrote back
through the locked reader and reports it. These tests hold that contract:

* the readback reports the tick the gate recorded,
* an absent ledger is not an error,
* a corrupt ledger is reported and swallowed (observability must never change
  the wake verdict), and
* ``_has_work`` actually calls the readback on the dispatch path.
"""

from __future__ import annotations

from pathlib import Path

from scripts import evolution_hydra_gate as hydra

EDGE = "analysis→implementation"


def _write_stage(evo_dir: Path, stage: str, content: str) -> None:
    d = evo_dir / stage
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{hydra._today()}.md").write_text(content, encoding="utf-8")


class TestReadback:
    def test_reports_the_tick_the_gate_recorded(self, tmp_path, capsys):
        _write_stage(tmp_path, "analysis", "analysis for today")
        hydra._record_dispatch(
            tmp_path,
            EDGE,
            hydra._latest_output(tmp_path, "analysis"),
            hydra._stage_content_hash(tmp_path, "analysis"),
        )
        capsys.readouterr()  # drop anything written above

        hydra._report_ledger_tick(tmp_path, (EDGE,))

        err = capsys.readouterr().err
        assert "ledger readback: 1 edge(s)" in err
        assert f"latest {hydra._today()}" in err
        assert f"this tick: {EDGE}={hydra._today()}" in err

    def test_absent_ledger_reports_zero_edges(self, tmp_path, capsys):
        hydra._report_ledger_tick(tmp_path, ())

        err = capsys.readouterr().err
        assert "ledger readback: 0 edge(s)" in err
        assert "latest none" in err
        assert "this tick" not in err

    def test_corrupt_ledger_is_reported_and_swallowed(self, tmp_path, capsys):
        (tmp_path / ".hydra-dispatch-ledger.json").write_text(
            "{not json", encoding="utf-8"
        )

        hydra._report_ledger_tick(tmp_path, (EDGE,))  # must not raise

        err = capsys.readouterr().err
        assert "ledger readback failed" in err
        assert "not valid JSON" in err

    def test_off_schema_ledger_names_the_offending_edge(self, tmp_path, capsys):
        (tmp_path / ".hydra-dispatch-ledger.json").write_text(
            '{"analysis→implementation": {"date": "2026-10-27"}}', encoding="utf-8"
        )

        hydra._report_ledger_tick(tmp_path, (EDGE,))

        err = capsys.readouterr().err
        assert "ledger readback failed" in err
        assert "analysis→implementation" in err
        assert "missing required field" in err


class TestHasWorkCallsTheReader:
    def test_dispatch_path_invokes_the_readback(self, tmp_path, monkeypatch, capsys):
        """The call site itself, not just the helper: prevents a re-bounce."""
        _write_stage(tmp_path, "analysis", "analysis for today")
        calls = []
        monkeypatch.setattr(
            hydra, "_check_pool", lambda evo_dir: {EDGE: True}
        )
        monkeypatch.setattr(
            hydra,
            "_report_ledger_tick",
            lambda evo_dir, edges: calls.append((evo_dir, edges)),
        )

        has_work, reason = hydra._has_work(tmp_path)

        assert has_work is True
        assert EDGE in reason
        assert calls == [(tmp_path, (EDGE,))]

    def test_dispatch_path_reports_without_stubbing(self, tmp_path, monkeypatch, capsys):
        _write_stage(tmp_path, "analysis", "analysis for today")
        monkeypatch.setattr(hydra, "_check_pool", lambda evo_dir: {EDGE: True})

        hydra._has_work(tmp_path)

        err = capsys.readouterr().err
        assert "ledger readback: 1 edge(s)" in err
