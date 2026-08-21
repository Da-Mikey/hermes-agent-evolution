# -*- coding: utf-8 -*-
"""Tests for :mod:`evolution.lib.mutation_guard` (issue #3014)."""

from __future__ import annotations

import json

import pytest

from evolution.lib.mutation_guard import (
    MutatingFailureCounter,
    Snapshot,
    extract_destructive_paths,
    is_destructive_command,
    snapshot_destructive_command,
)


class TestIsDestructiveCommand:
    def test_empty_is_not_destructive(self):
        assert is_destructive_command("") is False

    def test_rm_is_destructive(self):
        assert is_destructive_command("rm data.csv") is True
        assert is_destructive_command("rm -rf build/") is True

    def test_mv_is_destructive(self):
        assert is_destructive_command("mv notes.txt archive/") is True

    def test_truncate_is_destructive(self):
        assert is_destructive_command("truncate -s 0 app.log") is True

    def test_dd_of_is_destructive(self):
        assert (
            is_destructive_command("dd if=/dev/zero of=blob.img bs=1M count=1") is True
        )

    def test_redirect_is_destructive(self):
        assert is_destructive_command("echo hi > out.txt") is True
        assert is_destructive_command("cat a >> log.txt") is True

    def test_readonly_is_not_destructive(self):
        assert is_destructive_command("ls -la") is False
        assert is_destructive_command("grep foo file.txt") is False
        assert is_destructive_command("cat notes.txt") is False


class TestExtractDestructivePaths:
    def test_rm_paths(self):
        paths = extract_destructive_paths("rm -rf build/ dist/", cwd="/proj")
        # Path normalization strips trailing slashes.
        assert "/proj/build" in paths
        assert "/proj/dist" in paths

    def test_mv_uses_source(self):
        paths = extract_destructive_paths("mv /a/source.txt /dest/", cwd="/proj")
        assert "/a/source.txt" in paths
        # Destination of mv is a target, not a lost source — not snapshotted.
        assert "/dest/" not in paths

    def test_dd_of_target(self):
        paths = extract_destructive_paths("dd if=/dev/zero of=blob.img", cwd="/work")
        assert "/work/blob.img" in paths

    def test_redirect_target(self):
        paths = extract_destructive_paths("python x.py > results/out.log", cwd="/run")
        assert "/run/results/out.log" in paths

    def test_ignores_dev_and_absolute_system_paths(self):
        paths = extract_destructive_paths("rm -rf /dev/shm/app", cwd="/proj")
        # /dev/ is excluded as a pseudo-filesystem we must not snapshot.
        assert all("/dev/" not in p for p in paths)

    def test_deduplicates(self):
        paths = extract_destructive_paths("rm data.txt; mv data.txt /backup", cwd="/d")
        assert paths.count("/d/data.txt") == 1

    def test_empty(self):
        assert extract_destructive_paths("") == []


class TestSnapshotCommand:
    def test_non_destructive_returns_none(self, tmp_path):
        assert snapshot_destructive_command("ls -la", cwd=str(tmp_path)) is None

    def test_snapshot_copies_touched_files(self, tmp_path):
        target = tmp_path / "data.csv"
        target.write_text("a,b,c\n", encoding="utf-8")
        snap = snapshot_destructive_command("rm data.csv", cwd=str(tmp_path))
        assert snap is not None
        # Backup files are prefixed with an index (e.g. 000_data.csv).
        backup_names = [p.name for p in map(_path, snap.files.values())]
        assert any(name.endswith(target.name) for name in backup_names)
        # The original file must be left intact (snapshot copies, doesn't move).
        assert target.read_text(encoding="utf-8") == "a,b,c\n"
        # Backup copy holds the same bytes.
        backup = list(snap.files.values())[0]
        assert _path(backup).read_text(encoding="utf-8") == "a,b,c\n"

    def test_snapshot_manifest_serializable(self, tmp_path):
        (tmp_path / "f.txt").write_text("x", encoding="utf-8")
        snap = snapshot_destructive_command("rm f.txt", cwd=str(tmp_path))
        assert snap is not None
        json.dumps(snap.to_dict())  # must not raise
        assert snap.rollback_paths == list(snap.files.values())

    def test_snapshot_custom_dir(self, tmp_path):
        (tmp_path / "g.log").write_text("y", encoding="utf-8")
        snap_dir = tmp_path / "snaps"
        snap = snapshot_destructive_command(
            "truncate -s 0 g.log", cwd=str(tmp_path), snapshot_dir=str(snap_dir)
        )
        assert snap is not None
        assert snap.snapshot_dir == str(snap_dir)
        assert snap_dir.is_dir()

    def test_missing_file_skipped(self, tmp_path):
        # Command touches a path that doesn't exist -> no files to snapshot.
        snap = snapshot_destructive_command("rm does-not-exist.txt", cwd=str(tmp_path))
        assert snap is None or snap.files == {}


def _path(s: str):
    from pathlib import Path

    return Path(s)


class TestSnapshotDataclass:
    def test_defaults(self):
        snap = Snapshot(command="rm x", snapshot_dir="/tmp/s")
        assert snap.files == {}
        assert snap.created == ""
        assert snap.rollback_paths == []


class TestMutatingFailureCounter:
    def test_record_and_summary(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        counter = MutatingFailureCounter(ledger)
        counter.record("run-1", "mutating")
        counter.record("run-2", "other")
        counter.record("run-3", "mutating")
        s = counter.summary()
        assert s["failed_runs"] == 3
        assert s["mutating_cause"] == 2
        assert s["other_cause"] == 1
        assert s["mutating_share"] == round(2 / 3, 3)

    def test_no_records_empty_summary(self, tmp_path):
        counter = MutatingFailureCounter(tmp_path / "nope.jsonl")
        s = counter.summary()
        assert s["failed_runs"] == 0
        assert s["mutating_share"] is None

    def test_invalid_cause_rejected(self, tmp_path):
        counter = MutatingFailureCounter(tmp_path / "ledger.jsonl")
        with pytest.raises(ValueError):
            counter.record("run-x", "maybe")

    def test_ledger_lines_are_json(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        counter = MutatingFailureCounter(ledger)
        counter.record("run-1", "mutating", recorded_at="2026-08-21")
        lines = ledger.read_text(encoding="utf-8").strip().splitlines()
        rec = json.loads(lines[0])
        assert rec["run_id"] == "run-1"
        assert rec["cause_category"] == "mutating"
        assert rec["outcome"] == "failed"
        assert rec["recorded_at"] == "2026-08-21"

    def test_malformed_lines_skipped(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text("not-json\n", encoding="utf-8")
        counter = MutatingFailureCounter(ledger)
        counter.record("run-1", "other")
        assert counter.summary()["failed_runs"] == 1
