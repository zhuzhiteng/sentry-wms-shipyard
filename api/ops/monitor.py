"""Poll critical production dependencies and alert on state changes."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal

# Celery inspect uses a fixed argv list and shell=False.
import subprocess  # nosec B404
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import urlopen

from ops.common import (
    atomic_write_json,
    atomic_write_text,
    bool_env,
    int_env,
    parse_utc,
    read_json,
    redact_text,
    send_alert,
    touch_heartbeat,
    utc_iso,
    utc_now,
    validate_webhook_url,
)

LOGGER = logging.getLogger("sentry.ops.monitor")
DEFAULT_STATE_DIR = Path("/state")


@dataclass(frozen=True)
class CheckResult:
    healthy: bool
    detail: str = ""
    observed: dict[str, Any] | None = None


@dataclass(frozen=True)
class MonitorConfig:
    api_health_url: str
    database_url: str
    redis_url: str
    celery_broker_url: str
    state_dir: Path
    environment: str
    interval_seconds: int
    timeout_seconds: int
    failure_threshold: int
    repeat_seconds: int
    backup_max_age_hours: int
    alert_webhook_url: str
    alert_bearer_token: str
    allow_http_alerts: bool


def load_config() -> MonitorConfig:
    state_dir = Path(os.getenv("SENTRY_OPS_STATE_DIR", str(DEFAULT_STATE_DIR)))
    database_url = os.getenv("DATABASE_URL", "").strip()
    redis_url = os.getenv("REDIS_URL", "").strip()
    celery_broker_url = os.getenv("CELERY_BROKER_URL", "").strip()
    if not database_url:
        raise ValueError("DATABASE_URL is required")
    if not redis_url:
        raise ValueError("REDIS_URL is required")
    if not celery_broker_url:
        raise ValueError("CELERY_BROKER_URL is required")
    allow_http_alerts = bool_env("SENTRY_OPS_ALLOW_HTTP_ALERTS", False)
    webhook_url = validate_webhook_url(
        os.getenv("SENTRY_OPS_ALERT_WEBHOOK_URL", "").strip(),
        allow_http=allow_http_alerts,
    )
    api_health_url = os.getenv(
        "SENTRY_OPS_API_HEALTH_URL",
        "http://api:5000/api/health",
    ).strip()
    parsed_health = urlsplit(api_health_url)
    if parsed_health.scheme not in {"http", "https"} or not parsed_health.netloc:
        raise ValueError("SENTRY_OPS_API_HEALTH_URL must be an HTTP(S) URL")
    return MonitorConfig(
        api_health_url=api_health_url,
        database_url=database_url,
        redis_url=redis_url,
        celery_broker_url=celery_broker_url,
        state_dir=state_dir,
        environment=os.getenv("SENTRY_OPS_ENVIRONMENT", "production").strip(),
        interval_seconds=int_env("SENTRY_OPS_INTERVAL_S", 60, 10, 3600),
        timeout_seconds=int_env("SENTRY_OPS_TIMEOUT_S", 5, 1, 30),
        failure_threshold=int_env("SENTRY_OPS_FAILURE_THRESHOLD", 2, 1, 10),
        repeat_seconds=int_env("SENTRY_OPS_REPEAT_S", 3600, 60, 86400),
        backup_max_age_hours=int_env(
            "SENTRY_OPS_BACKUP_MAX_AGE_HOURS",
            30,
            1,
            168,
        ),
        alert_webhook_url=webhook_url,
        alert_bearer_token=os.getenv(
            "SENTRY_OPS_ALERT_BEARER_TOKEN",
            "",
        ).strip(),
        allow_http_alerts=allow_http_alerts,
    )


def _safe_error(exc: BaseException) -> str:
    message = " ".join(str(exc).split())
    detail = redact_text(message[:300])
    return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__


def probe_api(url: str, timeout_seconds: int, *, opener=urlopen) -> CheckResult:
    try:
        response = opener(url, timeout=timeout_seconds)
        try:
            status = int(getattr(response, "status", 200))
            raw = response.read(4096)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        payload = json.loads(raw.decode("utf-8"))
        if status != 200:
            return CheckResult(False, f"HTTP {status}")
        if payload.get("status") != "ok":
            return CheckResult(False, "unexpected health payload")
        return CheckResult(True, observed={"service": payload.get("service")})
    except Exception as exc:  # noqa: BLE001 - probes must never crash the monitor
        return CheckResult(False, _safe_error(exc))


def probe_database(database_url: str, timeout_seconds: int) -> CheckResult:
    try:
        import psycopg2

        with (
            psycopg2.connect(
                database_url,
                connect_timeout=timeout_seconds,
            ) as connection,
            connection.cursor() as cursor,
        ):
            cursor.execute("SELECT 1")
            value = cursor.fetchone()[0]
        if value != 1:
            return CheckResult(False, "unexpected SELECT result")
        return CheckResult(True)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(False, _safe_error(exc))


def probe_redis(redis_url: str, timeout_seconds: int) -> CheckResult:
    try:
        import redis

        client = redis.Redis.from_url(
            redis_url,
            socket_connect_timeout=timeout_seconds,
            socket_timeout=timeout_seconds,
        )
        if client.ping() is not True:
            return CheckResult(False, "PING did not return PONG")
        return CheckResult(True)
    except Exception as exc:  # noqa: BLE001
        return CheckResult(False, _safe_error(exc))


def probe_celery(timeout_seconds: int, *, runner=subprocess.run) -> CheckResult:
    try:
        completed = runner(
            [
                "celery",
                "-A",
                "jobs",
                "inspect",
                "ping",
                "--timeout",
                str(timeout_seconds),
            ],
            capture_output=True,
            text=True,
            timeout=timeout_seconds + 2,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001
        return CheckResult(False, _safe_error(exc))
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    if completed.returncode != 0 or "pong" not in output.lower():
        return CheckResult(False, output[:300] or "no Celery worker replied")
    return CheckResult(True)


def probe_backup(
    status_path: Path,
    max_age_hours: int,
    *,
    now=None,
) -> CheckResult:
    status = read_json(status_path, {})
    if not isinstance(status, dict):
        return CheckResult(False, "backup status is unreadable")
    last_success = parse_utc(status.get("last_success_at"))
    if last_success is None:
        return CheckResult(False, "no successful backup recorded")
    current = now or utc_now()
    age_hours = (current - last_success).total_seconds() / 3600
    last_failure = parse_utc(status.get("last_failure_at"))
    if last_failure is not None and last_failure >= last_success:
        return CheckResult(False, "latest backup attempt failed")
    if age_hours > max_age_hours:
        return CheckResult(
            False,
            f"latest successful backup is {age_hours:.1f} hours old",
        )
    return CheckResult(
        True,
        observed={
            "age_hours": round(age_hours, 2),
            "bytes": status.get("bytes"),
            "sha256": status.get("sha256"),
        },
    )


def apply_component_result(
    previous: dict[str, Any] | None,
    component: str,
    result: CheckResult,
    *,
    now_seconds: float,
    failure_threshold: int,
    repeat_seconds: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    state = dict(previous or {})
    failures = int(state.get("consecutive_failures", 0))
    alert_active = bool(state.get("alert_active", False))
    last_alert = float(state.get("last_alert_epoch", 0) or 0)
    event = None

    if result.healthy:
        if alert_active:
            event = {
                "event": "recovered",
                "component": component,
                "detail": result.detail or "health checks are passing",
            }
        failures = 0
        alert_active = False
    else:
        failures += 1
        should_alert = failures >= failure_threshold and (
            not alert_active or now_seconds - last_alert >= repeat_seconds
        )
        if should_alert:
            event = {
                "event": "repeated" if alert_active else "down",
                "component": component,
                "detail": result.detail or "health check failed",
            }
            alert_active = True
            last_alert = now_seconds

    state.update(
        {
            "healthy": result.healthy,
            "consecutive_failures": failures,
            "alert_active": alert_active,
            "last_alert_epoch": last_alert,
            "last_error": None if result.healthy else result.detail,
            "last_checked_at": utc_iso(),
            "observed": result.observed or {},
        }
    )
    return state, event


def _metrics_text(status: dict[str, Any]) -> str:
    lines = [
        "# HELP sentry_wms_component_up Whether a critical dependency passed its latest probe.",
        "# TYPE sentry_wms_component_up gauge",
    ]
    for name, component in sorted(status.get("components", {}).items()):
        value = 1 if component.get("healthy") else 0
        lines.append(f'sentry_wms_component_up{{component="{name}"}} {value}')
    checked = parse_utc(status.get("checked_at"))
    if checked:
        lines.append(
            f"sentry_wms_monitor_last_check_timestamp_seconds {checked.timestamp():.0f}"
        )
    lines.append(f"sentry_wms_monitor_overall_up {1 if status.get('healthy') else 0}")
    return "\n".join(lines) + "\n"


def _alert_text(
    event: dict[str, Any],
    *,
    environment: str,
) -> str:
    prefix = f"[Sentry WMS][{environment}]"
    if event["event"] == "recovered":
        return f"{prefix} {event['component']} recovered. {event['detail']}"
    repeat = " still unhealthy" if event["event"] == "repeated" else " is unhealthy"
    return f"{prefix} {event['component']}{repeat}: {event['detail']}"


def run_once(
    config: MonitorConfig,
    *,
    probes: dict[str, Callable[[], CheckResult]] | None = None,
    alert_sender=send_alert,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    state_path = config.state_dir / "monitor-state.json"
    status_path = config.state_dir / "monitor-status.json"
    metrics_path = config.state_dir / "monitor.prom"
    heartbeat_path = config.state_dir / "monitor-heartbeat"
    previous = read_json(state_path, {})
    if not isinstance(previous, dict):
        previous = {}
    previous_components = previous.get("components", {})
    if not isinstance(previous_components, dict):
        previous_components = {}

    selected_probes = probes or {
        "api": lambda: probe_api(
            config.api_health_url,
            config.timeout_seconds,
        ),
        "database": lambda: probe_database(
            config.database_url,
            config.timeout_seconds,
        ),
        "redis": lambda: probe_redis(
            config.redis_url,
            config.timeout_seconds,
        ),
        "celery": lambda: probe_celery(config.timeout_seconds),
        "backup": lambda: probe_backup(
            config.state_dir / "backup-status.json",
            config.backup_max_age_hours,
        ),
    }

    now = utc_now()
    now_seconds = now.timestamp()
    components: dict[str, dict[str, Any]] = {}
    alerts: list[dict[str, Any]] = []
    for name, probe in selected_probes.items():
        try:
            result = probe()
        except Exception as exc:  # noqa: BLE001
            result = CheckResult(False, _safe_error(exc))
        state, event = apply_component_result(
            previous_components.get(name),
            name,
            result,
            now_seconds=now_seconds,
            failure_threshold=config.failure_threshold,
            repeat_seconds=config.repeat_seconds,
        )
        components[name] = state
        if event:
            alerts.append(event)

    status = {
        "checked_at": utc_iso(now),
        "environment": config.environment,
        "healthy": all(item.get("healthy") for item in components.values()),
        "components": components,
    }
    atomic_write_json(
        state_path,
        {"checked_at": status["checked_at"], "components": components},
    )
    atomic_write_json(status_path, status)
    atomic_write_text(metrics_path, _metrics_text(status))
    touch_heartbeat(heartbeat_path)

    for event in alerts:
        text = _alert_text(event, environment=config.environment)
        try:
            delivered = alert_sender(
                config.alert_webhook_url,
                text,
                timeout_seconds=config.timeout_seconds,
                allow_http=config.allow_http_alerts,
                bearer_token=config.alert_bearer_token,
            )
            LOGGER.warning("ops alert %s delivered=%s", event, delivered)
        except Exception:
            LOGGER.exception("ops alert delivery failed")
    return status, alerts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    config = load_config()
    if not config.alert_webhook_url:
        LOGGER.warning(
            "SENTRY_OPS_ALERT_WEBHOOK_URL is empty; status files are updated "
            "but no external alerts will be sent",
        )
    if args.once:
        status, _alerts = run_once(config)
        LOGGER.info("ops monitor status healthy=%s", status["healthy"])
        return 0 if status["healthy"] else 1

    stopping = threading.Event()

    def stop(_signum, _frame):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while not stopping.is_set():
        try:
            status, _alerts = run_once(config)
            LOGGER.info("ops monitor status healthy=%s", status["healthy"])
        except Exception:
            LOGGER.exception("ops monitor cycle failed")
            touch_heartbeat(config.state_dir / "monitor-heartbeat")
        stopping.wait(config.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
