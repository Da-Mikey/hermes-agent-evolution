# -*- coding: utf-8 -*-
"""The Hydra dispatch-ledger reader must fail LOUD, not fail empty (#148).

Slice A of #148 adds ``scripts/orchbench_ledger.py``: a deterministic reader +
schema lock over the per-edge ledger that ``evolution_hydra_gate`` writes. The
behaviour that matters is the asymmetry with the gate's own reader. The gate
**fails open** (``{}`` on a corrupt file) because a damaged ledger must never
block a real stage wake-up. An analysis reader that did the same would score a
broken tick as "dispatched nothing" — a data fault laundered into a clean
result. So: absent ledger is ``{}``; present-but-off-schema raises.

The fixture below is the record shape read off the live ledger on 2026-10-12
(keys ``date`` / ``upstream_mtime`` / ``upstream_hash`` / ``verdict``), not an
invented one.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import orchbench_ledger as obl  # noqa: E402

SHA = "3b503fdcf0052ece27c74714883b4bdee23d609115f10a5bf81bf5e6388dd0bd"

LIVE_SHAPE = {
    "analysis→implementation": {
        "date": "2026-09-01",
        "upstream_mtime": 1788218339.84029,
        "upstream_hash": SHA,
        "verdict": None,
    },
    "issues→analysis": {
        "date": "2026-08-31",
        "upstream_mtime": 1788173269.8696458,
        "upstream_hash": "8f1d4952c2a6479a0b90c4db604885c6ba28bb849c301c4c7681c7b7767a6b7a",
        "verdict": "advance",
    },
}


def _write(tmp_path: Path, payload) -> Path:
    """Write *payload* as the ledger inside *tmp_path* and return the dir."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    (tmp_path / obl.LEDGER_FILENAME).write_text(text, encoding="utf-8")
    return tmp_path


class TestReadLedger:
    def test_absent_ledger_is_empty_not_an_error(self, tmp_path):
        assert obl.read_ledger(tmp_path) == {}

    def test_live_record_shape_parses(self, tmp_path):
        records = obl.read_ledger(_write(tmp_path, LIVE_SHAPE))
        assert set(records) == {"analysis→implementation", "issues→analysis"}
        rec = records["analysis→implementation"]
        assert rec.date == "2026-09-01"
        assert rec.upstream_hash == SHA
        assert rec.upstream_mtime == pytest.approx(1788218339.84029)
        assert rec.verdict is None
        assert records["issues→analysis"].verdict == "advance"

    def test_edge_label_is_carried_on_each_record(self, tmp_path):
        records = obl.read_ledger(_write(tmp_path, LIVE_SHAPE))
        assert records["issues→analysis"].edge == "issues→analysis"

    def test_explicit_version_one_accepted(self, tmp_path):
        payload = dict(LIVE_SHAPE, version=1)
        assert len(obl.read_ledger(_write(tmp_path, payload))) == 2

    def test_version_key_is_not_treated_as_an_edge(self, tmp_path):
        records = obl.read_ledger(_write(tmp_path, dict(LIVE_SHAPE, version=1)))
        assert "version" not in records

    def test_integer_mtime_is_accepted_and_normalised_to_float(self, tmp_path):
        payload = {
            "a→b": {
                "date": "2026-09-01",
                "upstream_mtime": 42,
                "upstream_hash": SHA,
                "verdict": None,
            }
        }
        rec = obl.read_ledger(_write(tmp_path, payload))["a→b"]
        assert isinstance(rec.upstream_mtime, float)

    def test_extra_keys_are_preserved_not_rejected(self, tmp_path):
        payload = {
            "a→b": {
                "date": "2026-09-01",
                "upstream_mtime": 1.0,
                "upstream_hash": SHA,
                "verdict": None,
                "future_field": {"k": 1},
            }
        }
        rec = obl.read_ledger(_write(tmp_path, payload))["a→b"]
        assert rec.extra == {"future_field": {"k": 1}}
        assert rec.to_dict()["future_field"] == {"k": 1}

    def test_round_trip_through_to_dict(self, tmp_path):
        rec = obl.read_ledger(_write(tmp_path, LIVE_SHAPE))["issues→analysis"]
        rebuilt = obl.LedgerRecord.from_dict(rec.edge, rec.to_dict())
        assert rebuilt == rec


class TestSchemaLockFailsLoud:
    """Every one of these must raise, naming the problem."""

    def test_unknown_version_is_rejected_loudly(self, tmp_path):
        with pytest.raises(obl.LedgerSchemaError) as exc:
            obl.read_ledger(_write(tmp_path, dict(LIVE_SHAPE, version=2)))
        assert "unknown ledger schema version 2" in str(exc.value)

    def test_non_integer_version_is_rejected(self, tmp_path):
        with pytest.raises(obl.LedgerSchemaError) as exc:
            obl.read_ledger(_write(tmp_path, dict(LIVE_SHAPE, version="1")))
        assert "must be an integer" in str(exc.value)

    def test_top_level_list_is_rejected(self, tmp_path):
        with pytest.raises(obl.LedgerSchemaError) as exc:
            obl.read_ledger(_write(tmp_path, [1, 2, 3]))
        assert "JSON object" in str(exc.value)

    def test_non_object_record_is_rejected(self, tmp_path):
        with pytest.raises(obl.LedgerSchemaError) as exc:
            obl.read_ledger(_write(tmp_path, {"a→b": "2026-09-01"}))
        assert "must be a JSON object" in str(exc.value)

    def test_missing_field_names_the_field_and_the_edge(self, tmp_path):
        with pytest.raises(obl.LedgerSchemaError) as exc:
            obl.read_ledger(_write(tmp_path, {"a→b": {"date": "2026-09-01"}}))
        message = str(exc.value)
        assert "a→b" in message
        assert "upstream_hash" in message and "verdict" in message

    @pytest.mark.parametrize("bad_date", ["20260901", "2026-9-1", "", 20260901])
    def test_bad_date_is_rejected(self, tmp_path, bad_date):
        payload = {
            "a→b": {
                "date": bad_date,
                "upstream_mtime": 1.0,
                "upstream_hash": SHA,
                "verdict": None,
            }
        }
        with pytest.raises(obl.LedgerSchemaError) as exc:
            obl.read_ledger(_write(tmp_path, payload))
        assert "date" in str(exc.value)

    @pytest.mark.parametrize(
        "bad_hash", [SHA[:63], SHA.upper(), "zz" + SHA[2:], 12345, None]
    )
    def test_bad_upstream_hash_is_rejected(self, tmp_path, bad_hash):
        payload = {
            "a→b": {
                "date": "2026-09-01",
                "upstream_mtime": 1.0,
                "upstream_hash": bad_hash,
                "verdict": None,
            }
        }
        with pytest.raises(obl.LedgerSchemaError) as exc:
            obl.read_ledger(_write(tmp_path, payload))
        assert "upstream_hash" in str(exc.value)

    @pytest.mark.parametrize("bad_mtime", [True, "1788218339.8", None, [1]])
    def test_bad_mtime_is_rejected(self, tmp_path, bad_mtime):
        payload = {
            "a→b": {
                "date": "2026-09-01",
                "upstream_mtime": bad_mtime,
                "upstream_hash": SHA,
                "verdict": None,
            }
        }
        with pytest.raises(obl.LedgerSchemaError) as exc:
            obl.read_ledger(_write(tmp_path, payload))
        assert "upstream_mtime" in str(exc.value)

    def test_object_verdict_is_rejected(self, tmp_path):
        payload = {
            "a→b": {
                "date": "2026-09-01",
                "upstream_mtime": 1.0,
                "upstream_hash": SHA,
                "verdict": {"ok": True},
            }
        }
        with pytest.raises(obl.LedgerSchemaError) as exc:
            obl.read_ledger(_write(tmp_path, payload))
        assert "verdict" in str(exc.value)

    def test_corrupt_json_is_rejected_not_swallowed(self, tmp_path):
        with pytest.raises(obl.LedgerSchemaError) as exc:
            obl.read_ledger(_write(tmp_path, "{not json"))
        assert "not valid JSON" in str(exc.value)


class TestSummarise:
    def test_edges_are_sorted_and_latest_date_reported(self, tmp_path):
        summary = obl.summarise(obl.read_ledger(_write(tmp_path, LIVE_SHAPE)))
        assert summary["edge_count"] == 2
        assert [e["edge"] for e in summary["edges"]] == [
            "analysis→implementation",
            "issues→analysis",
        ]
        assert summary["latest_date"] == "2026-09-01"

    def test_empty_ledger_summary_is_json_safe(self):
        summary = obl.summarise({})
        assert summary["edge_count"] == 0 and summary["latest_date"] is None
        json.dumps(summary)


class TestCli:
    def test_valid_ledger_exits_zero_with_json_summary(self, tmp_path, capsys):
        _write(tmp_path, LIVE_SHAPE)
        assert obl.main(["--evolution-dir", str(tmp_path)]) == 0
        assert json.loads(capsys.readouterr().out)["edge_count"] == 2

    def test_schema_violation_exits_two_and_writes_to_stderr(self, tmp_path, capsys):
        _write(tmp_path, dict(LIVE_SHAPE, version=99))
        assert obl.main(["--evolution-dir", str(tmp_path)]) == 2
        captured = capsys.readouterr()
        assert "SCHEMA ERROR" in captured.err
        assert captured.out == ""

    def test_missing_ledger_exits_zero(self, tmp_path, capsys):
        assert obl.main(["--evolution-dir", str(tmp_path)]) == 0
        assert json.loads(capsys.readouterr().out)["edge_count"] == 0


def test_ledger_path_matches_the_writer():
    """The reader must point at the file the hydra gate writes."""
    assert obl.LEDGER_FILENAME == ".hydra-dispatch-ledger.json"
    assert obl.ledger_path("/evo") == Path("/evo/.hydra-dispatch-ledger.json")
