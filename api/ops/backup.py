"""Scheduled PostgreSQL custom-format backups with verification and retention."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import signal

# pg_dump and pg_restore are invoked with fixed argv lists and shell=False.
import subprocess  # nosec B404
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from ops.common import (
    atomic_write_json,
    atomic_write_text,
    bool_env,
    int_env,
    read_json,
    redact_text,
    touch_heartbeat,
    utc_iso,
    utc_now,
)

LOGGER = logging.getLogger("sentry.ops.backup")
BACKUP_PATTERN = re.compile(
    r"^sentry_[A-Za-z0-9_.-]+_\d{8}T\d{6}Z\.dump$",
)


@dataclass(frozen=True)
class DatabaseTarget:
    host: str
    port: int
    username: str
    password: str
    database: str
    sslmode: str


@dataclass(frozen=True)
class BackupConfig:
    database_url: str
    backup_dir: Path
    state_dir: Path
    schedule_utc: time
    retention_count: int
    timeout_seconds: int
    retry_seconds: int
    run_on_start: bool


def parse_database_url(database_url: str) -> DatabaseTarget:
    parsed = urlsplit(
        database_url.replace("postgresql+psycopg2://", "postgresql://", 1)
    )
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise ValueError("DATABASE_URL must use postgresql://")
    if not parsed.hostname or not parsed.username:
        raise ValueError("DATABASE_URL must include host and username")
    database = unquote(parsed.path.lstrip("/"))
    if not database:
        raise ValueError("DATABASE_URL must include a database name")
    query = parse_qs(parsed.query)
    return DatabaseTarget(
        host=parsed.hostname,
        port=parsed.port or 5432,
        username=unquote(parsed.username),
        password=unquote(parsed.password or ""),
        database=database,
        sslmode=(query.get("sslmode") or [""])[0],
    )


def parse_schedule(value: str) -> time:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except (ValueError, AttributeError) as exc:
        raise ValueError("SENTRY_BACKUP_SCHEDULE_UTC must use HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError("SENTRY_BACKUP_SCHEDULE_UTC must use a valid UTC time")
    return time(hour=hour, minute=minute, tzinfo=UTC)


def next_scheduled_run(now: datetime, schedule: time) -> datetime:
    current = now.astimezone(UTC)
    candidate = datetime.combine(current.date(), schedule)
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate


def load_config() -> BackupConfig:
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    return BackupConfig(
        database_url=database_url,
        backup_dir=Path(os.getenv("SENTRY_BACKUP_DIR", "/backups")),
        state_dir=Path(os.getenv("SENTRY_OPS_STATE_DIR", "/state")),
        schedule_utc=parse_schedule(
            os.getenv("SENTRY_BACKUP_SCHEDULE_UTC", "02:30"),
        ),
        retention_count=int_env("SENTRY_BACKUP_RETENTION_COUNT", 14, 2, 365),
        timeout_seconds=int_env("SENTRY_BACKUP_TIMEOUT_S", 1800, 60, 21600),
        retry_seconds=int_env("SENTRY_BACKUP_RETRY_S", 900, 300, 86400),
        run_on_start=bool_env("SENTRY_BACKUP_RUN_ON_START", True),
    )


def _safe_database_name(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return sanitized or "database"


def _pg_environment(target: DatabaseTarget) -> dict[str, str]:
    env = os.environ.copy()
    env["PGPASSWORD"] = target.password
    if target.sslmode:
        env["PGSSLMODE"] = target.sslmode
    return env


def _dump_command(target: DatabaseTarget, output_path: Path) -> list[str]:
    command = [
        "pg_dump",
        "--host",
        target.host,
        "--port",
        str(target.port),
        "--username",
        target.username,
        "--dbname",
        target.database,
        "--format=custom",
        "--compress=9",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(output_path),
    ]
    return command


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prune_backups(backup_dir: Path, retention_count: int) -> list[str]:
    candidates = sorted(
        (
            path
            for path in backup_dir.iterdir()
            if path.is_file() and BACKUP_PATTERN.fullmatch(path.name)
        ),
        key=lambda path: path.name,
    )
    removed = []
    for path in candidates[:-retention_count]:
        for related in (
            path,
            path.with_suffix(path.suffix + ".sha256"),
            path.with_suffix(path.suffix + ".json"),
        ):
            if related.exists():
                related.unlink()
                removed.append(str(related))
    return removed


def prune_partials(
    backup_dir: Path,
    *,
    now: datetime | None = None,
    max_age_hours: int = 24,
) -> list[str]:
    current = (now or utc_now()).timestamp()
    removed = []
    for path in backup_dir.glob("*.dump.partial"):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if current - modified > max_age_hours * 3600:
            path.unlink()
            removed.append(str(path))
    return removed


def _load_status(path: Path) -> dict:
    status = read_json(path, {})
    return status if isinstance(status, dict) else {}


def run_backup(
    config: BackupConfig,
    *,
    now: datetime | None = None,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict:
    started = now or utc_now()
    started_at = utc_iso(started)
    target = parse_database_url(config.database_url)
    config.backup_dir.mkdir(parents=True, exist_ok=True)
    config.state_dir.mkdir(parents=True, exist_ok=True)
    prune_partials(config.backup_dir, now=started)
    status_path = config.state_dir / "backup-status.json"
    existing = _load_status(status_path)
    stamp = started.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = f"sentry_{_safe_database_name(target.database)}_{stamp}.dump"
    final_path = config.backup_dir / filename
    temporary_path = final_path.with_suffix(final_path.suffix + ".partial")
    env = _pg_environment(target)

    try:
        dump = runner(
            _dump_command(target, temporary_path),
            env=env,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
        )
        if dump.returncode != 0 or not temporary_path.exists():
            raise RuntimeError(
                (dump.stderr or dump.stdout or "pg_dump failed")[:500],
            )
        verify = runner(
            ["pg_restore", "--list", str(temporary_path)],
            env=env,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            check=False,
        )
        if verify.returncode != 0 or not (verify.stdout or "").strip():
            raise RuntimeError(
                (verify.stderr or "pg_restore archive verification failed")[:500],
            )
        os.replace(temporary_path, final_path)
        os.chmod(final_path, 0o600)
        checksum = sha256_file(final_path)
        atomic_write_text(
            final_path.with_suffix(final_path.suffix + ".sha256"),
            f"{checksum}  {final_path.name}\n",
        )
        finished = utc_now()
        metadata = {
            "filename": final_path.name,
            "bytes": final_path.stat().st_size,
            "sha256": checksum,
            "started_at": started_at,
            "finished_at": utc_iso(finished),
            "verified_with": "pg_restore --list",
        }
        atomic_write_json(
            final_path.with_suffix(final_path.suffix + ".json"),
            metadata,
        )
        status = {
            **existing,
            **metadata,
            "last_attempt_at": started_at,
            "last_success_at": metadata["finished_at"],
            "last_failure_at": None,
            "last_error": None,
            "retention_count": config.retention_count,
            "pruned": prune_backups(
                config.backup_dir,
                config.retention_count,
            ),
        }
        atomic_write_json(status_path, status)
        LOGGER.info(
            "backup completed file=%s bytes=%s sha256=%s",
            final_path.name,
            metadata["bytes"],
            checksum,
        )
        return status
    except Exception as exc:
        if temporary_path.exists():
            temporary_path.unlink()
        failed_at = utc_iso()
        message = (
            f"{type(exc).__name__}: {redact_text(' '.join(str(exc).split())[:500])}"
        )
        status = {
            **existing,
            "last_attempt_at": started_at,
            "last_failure_at": failed_at,
            "last_error": message,
            "retention_count": config.retention_count,
        }
        atomic_write_json(status_path, status)
        LOGGER.error("backup failed error=%s", message)
        raise


def run_backup_with_heartbeat(
    config: BackupConfig,
    heartbeat_path: Path,
    *,
    now: datetime | None = None,
) -> dict:
    """Keep the scheduler alive while a potentially long dump is running."""
    completed = threading.Event()
    outcome: dict[str, object] = {}

    def execute() -> None:
        try:
            outcome["status"] = run_backup(config, now=now)
        except Exception as exc:  # noqa: BLE001 - re-raised in the caller thread
            outcome["error"] = exc
        finally:
            completed.set()

    worker = threading.Thread(target=execute, name="backup-worker", daemon=True)
    touch_heartbeat(heartbeat_path)
    worker.start()
    while not completed.is_set():
        touch_heartbeat(heartbeat_path)
        completed.wait(15)
    worker.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["status"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_config()
    heartbeat_path = config.state_dir / "backup-heartbeat"
    if args.once:
        run_backup_with_heartbeat(config, heartbeat_path)
        return 0

    stopping = threading.Event()

    def stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    next_run = next_scheduled_run(utc_now(), config.schedule_utc)
    if config.run_on_start:
        try:
            run_backup_with_heartbeat(config, heartbeat_path)
            next_run = next_scheduled_run(utc_now(), config.schedule_utc)
        except Exception:
            LOGGER.exception("initial backup failed")
            next_run = utc_now() + timedelta(seconds=config.retry_seconds)

    while not stopping.is_set():
        touch_heartbeat(heartbeat_path)
        current = utc_now()
        if current >= next_run:
            try:
                run_backup_with_heartbeat(
                    config,
                    heartbeat_path,
                    now=current,
                )
                next_run = next_scheduled_run(current, config.schedule_utc)
            except Exception:
                LOGGER.exception("scheduled backup failed")
                next_run = current + timedelta(seconds=config.retry_seconds)
        remaining = min(30.0, max(0.2, (next_run - utc_now()).total_seconds()))
        stopping.wait(remaining)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
