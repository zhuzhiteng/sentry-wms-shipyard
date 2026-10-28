"""Unit coverage for scheduled PostgreSQL backup safety and retention."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ops.backup as backup_module
from ops.backup import (
    BackupConfig,
    next_scheduled_run,
    parse_database_url,
    parse_schedule,
    prune_backups,
    prune_partials,
    run_backup,
    run_backup_with_heartbeat,
)


def _config(tmp_path: Path, **overrides) -> BackupConfig:
    values = {
        "database_url": "postgresql://sentry:p%40ss@db:5432/sentry",
        "backup_dir": tmp_path / "backups",
        "state_dir": tmp_path / "state",
        "schedule_utc": parse_schedule("02:30"),
        "retention_count": 3,
        "timeout_seconds": 120,
        "retry_seconds": 900,
        "run_on_start": True,
    }
    values.update(overrides)
    return BackupConfig(**values)


def test_database_url_is_parsed_without_putting_password_in_arguments():
    target = parse_database_url(
        "postgresql://sentry:p%40ss@db:5433/sentry?sslmode=require",
    )
    assert target.host == "db"
    assert target.port == 5433
    assert target.username == "sentry"
    assert target.password == "p@ss"
    assert target.database == "sentry"
    assert target.sslmode == "require"


def test_schedule_calculation_is_utc_and_rolls_to_next_day():
    schedule = parse_schedule("02:30")
    before = datetime(2026, 10, 29, 1, 0, tzinfo=UTC)
    after = datetime(2026, 10, 29, 3, 0, tzinfo=UTC)
    assert next_scheduled_run(before, schedule) == datetime(
        2026,
        10,
        29,
        2,
        30,
        tzinfo=UTC,
    )
    assert next_scheduled_run(after, schedule) == datetime(
        2026,
        10,
        30,
        2,
        30,
        tzinfo=UTC,
    )
    with pytest.raises(ValueError, match="valid UTC time"):
        parse_schedule("25:00")


def test_retention_only_deletes_managed_backup_files(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    managed = [
        backup_dir / f"sentry_sentry_2026102{day}T000000Z.dump" for day in range(1, 5)
    ]
    for path in managed:
        path.write_bytes(path.name.encode())
        path.with_suffix(path.suffix + ".sha256").write_text("hash\n")
        path.with_suffix(path.suffix + ".json").write_text("{}\n")
    unrelated = backup_dir / "keep-me.txt"
    unrelated.write_text("keep")

    removed = prune_backups(backup_dir, 2)

    assert unrelated.exists()
    assert [path.name for path in managed if path.exists()] == [
        managed[2].name,
        managed[3].name,
    ]
    assert len(removed) == 6


def test_prune_partials_removes_only_stale_unfinished_dumps(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    stale = backup_dir / "sentry_old.dump.partial"
    fresh = backup_dir / "sentry_new.dump.partial"
    stale.write_bytes(b"old")
    fresh.write_bytes(b"new")
    old_time = time.time() - 48 * 3600
    os.utime(stale, (old_time, old_time))

    removed = prune_partials(backup_dir)

    assert removed == [str(stale)]
    assert fresh.exists()


def test_run_backup_creates_verified_dump_checksum_and_metadata(tmp_path):
    seen = {}

    def runner(command, **kwargs):
        seen.setdefault("commands", []).append(command)
        seen["env"] = kwargs["env"]
        if command[0] == "pg_dump":
            output = Path(command[command.index("--file") + 1])
            output.write_bytes(b"custom-format-dump")
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "; archive TOC\n", "")

    config = _config(tmp_path)
    status = run_backup(
        config,
        now=datetime(2026, 10, 29, 2, 30, tzinfo=UTC),
        runner=runner,
    )

    dump_path = config.backup_dir / status["filename"]
    assert dump_path.read_bytes() == b"custom-format-dump"
    assert stat.S_IMODE(dump_path.stat().st_mode) == 0o600
    assert status["verified_with"] == "pg_restore --list"
    assert len(status["sha256"]) == 64
    assert seen["env"]["PGPASSWORD"] == "p@ss"
    assert "p@ss" not in " ".join(seen["commands"][0])
    assert (dump_path.parent / f"{dump_path.name}.sha256").exists()
    assert (
        json.loads(
            (dump_path.parent / f"{dump_path.name}.json").read_text(),
        )["sha256"]
        == status["sha256"]
    )
    assert (config.state_dir / "backup-status.json").exists()


def test_failed_dump_removes_partial_and_records_failure(tmp_path):
    def runner(command, **kwargs):
        if command[0] == "pg_dump":
            output = Path(command[command.index("--file") + 1])
            output.write_bytes(b"partial")
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "connection lost",
            )
        raise AssertionError("pg_restore must not run after a failed dump")

    config = _config(tmp_path)
    with pytest.raises(RuntimeError, match="connection lost"):
        run_backup(
            config,
            now=datetime(2026, 10, 29, 2, 30, tzinfo=UTC),
            runner=runner,
        )

    assert list(config.backup_dir.glob("*.partial")) == []
    status = json.loads(
        (config.state_dir / "backup-status.json").read_text(),
    )
    assert status["last_failure_at"]
    assert "connection lost" in status["last_error"]
    assert "p@ss" not in status["last_error"]


def test_backup_heartbeat_wraps_long_running_work(monkeypatch, tmp_path):
    touches = []
    monkeypatch.setattr(
        backup_module,
        "run_backup",
        lambda config, now=None: {"filename": "ok.dump"},
    )
    monkeypatch.setattr(
        backup_module,
        "touch_heartbeat",
        lambda path: touches.append(path),
    )
    result = run_backup_with_heartbeat(
        _config(tmp_path),
        tmp_path / "state" / "backup-heartbeat",
    )
    assert result == {"filename": "ok.dump"}
    assert touches
    assert touches[0] == tmp_path / "state" / "backup-heartbeat"
