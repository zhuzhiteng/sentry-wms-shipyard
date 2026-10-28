"""Unit coverage for the production operations monitor."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ops.common import heartbeat_is_fresh, redact_text, send_alert
from ops.monitor import (
    CheckResult,
    MonitorConfig,
    apply_component_result,
    load_config,
    probe_api,
    probe_backup,
    run_once,
)


def _config(tmp_path: Path, **overrides) -> MonitorConfig:
    values = {
        "api_health_url": "http://api:5000/api/health",
        "database_url": "postgresql://sentry:sentry@db:5432/sentry",
        "redis_url": "redis://:secret@redis:6379/0",
        "celery_broker_url": "redis://:secret@redis:6379/0",
        "state_dir": tmp_path,
        "environment": "test",
        "interval_seconds": 60,
        "timeout_seconds": 5,
        "failure_threshold": 2,
        "repeat_seconds": 300,
        "backup_max_age_hours": 30,
        "alert_webhook_url": "https://alerts.test/hook",
        "alert_bearer_token": "",
        "allow_http_alerts": False,
    }
    values.update(overrides)
    return MonitorConfig(**values)


def test_component_alerts_only_after_threshold_repeats_and_recovers():
    now = 1_000.0
    state = None
    events = []
    for result in (
        CheckResult(False, "timeout"),
        CheckResult(False, "timeout"),
        CheckResult(False, "timeout"),
        CheckResult(True),
    ):
        state, event = apply_component_result(
            state,
            "database",
            result,
            now_seconds=now,
            failure_threshold=2,
            repeat_seconds=300,
        )
        if event:
            events.append(event)
        now += 301

    assert [event["event"] for event in events] == ["down", "repeated", "recovered"]
    assert state["healthy"] is True
    assert state["alert_active"] is False
    assert state["consecutive_failures"] == 0


def test_run_once_persists_status_metrics_and_alerts(tmp_path):
    config = _config(tmp_path)
    alerts = []
    calls = {"api": 0}
    current = {"api": CheckResult(True)}

    def api_probe():
        calls["api"] += 1
        return current["api"]

    def healthy_probe():
        return CheckResult(True)

    probes = {
        "api": api_probe,
        "database": healthy_probe,
        "redis": healthy_probe,
        "celery": healthy_probe,
        "backup": healthy_probe,
    }

    def alert_sender(url, text, **kwargs):
        alerts.append((url, text, kwargs))
        return True

    first, first_events = run_once(
        config,
        probes=probes,
        alert_sender=alert_sender,
    )
    assert first["healthy"] is True
    assert first_events == []

    current["api"] = CheckResult(False, "HTTP 503")
    run_once(config, probes=probes, alert_sender=alert_sender)
    second, second_events = run_once(
        config,
        probes=probes,
        alert_sender=alert_sender,
    )
    assert second["healthy"] is False
    assert second_events[0]["event"] == "down"
    assert alerts[0][1].startswith("[Sentry WMS][test] api is unhealthy")

    current["api"] = CheckResult(True)
    recovered, recovery_events = run_once(
        config,
        probes=probes,
        alert_sender=alert_sender,
    )
    assert recovered["healthy"] is True
    assert recovery_events[0]["event"] == "recovered"
    assert "recovered" in alerts[1][1]

    status = json.loads((tmp_path / "monitor-status.json").read_text())
    assert status["components"]["api"]["healthy"] is True
    assert "sentry_wms_component_up" in (tmp_path / "monitor.prom").read_text()
    assert heartbeat_is_fresh(tmp_path / "monitor-heartbeat", 5)


class _Response:
    def __init__(self, payload: bytes, status: int = 200):
        self._payload = payload
        self.status = status
        self.closed = False

    def read(self, _size=-1):
        return self._payload

    def close(self):
        self.closed = True


def test_probe_api_requires_expected_payload():
    response = _Response(b'{"status":"ok","service":"sentry-wms"}')
    result = probe_api(
        "https://api.test/api/health", 2, opener=lambda *a, **k: response
    )
    assert result.healthy is True
    assert response.closed is True

    bad = probe_api(
        "https://api.test/api/health",
        2,
        opener=lambda *a, **k: _Response(b'{"status":"degraded"}'),
    )
    assert bad.healthy is False


def test_probe_backup_reports_staleness_and_latest_failure(tmp_path):
    status_path = tmp_path / "backup-status.json"
    now = time.time()
    status_path.write_text(
        json.dumps(
            {
                "last_success_at": "2026-10-29T00:00:00Z",
                "last_failure_at": None,
            },
        ),
        encoding="utf-8",
    )
    fresh = probe_backup(
        status_path,
        30,
        now=datetime.fromisoformat("2026-10-29T01:00:00+00:00"),
    )
    assert fresh.healthy is True
    stale = probe_backup(
        status_path,
        30,
        now=datetime.fromisoformat("2026-10-31T00:00:00+00:00"),
    )
    assert stale.healthy is False
    status = json.loads(status_path.read_text())
    status["last_failure_at"] = "2026-10-29T02:00:00Z"
    status_path.write_text(json.dumps(status), encoding="utf-8")
    failed = probe_backup(
        status_path,
        30,
        now=datetime.fromisoformat("2026-10-29T03:00:00+00:00"),
    )
    assert failed.healthy is False
    assert "failed" in failed.detail
    assert now > 0


def test_send_alert_uses_generic_teams_payload_and_enforces_https():
    captured = {}

    class OpenerResponse:
        status = 204

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return OpenerResponse()

    assert send_alert(
        "https://alerts.test/hook",
        "database down",
        timeout_seconds=3,
        allow_http=False,
        opener=opener,
    )
    assert captured == {
        "url": "https://alerts.test/hook",
        "body": {"text": "database down"},
        "timeout": 3,
    }
    with pytest.raises(ValueError, match="HTTPS"):
        send_alert(
            "http://alerts.test/hook",
            "database down",
            timeout_seconds=3,
            allow_http=False,
        )


def test_redact_text_removes_url_and_keyword_passwords():
    message = (
        "password=secret redis://:redis-secret@redis:6379/0 "
        "postgresql://sentry:db-secret@db:5432/sentry"
    )
    redacted = redact_text(message)
    assert "redis-secret" not in redacted
    assert "db-secret" not in redacted
    assert "password=***" in redacted
    assert "redis://:***@" in redacted
    assert "postgresql://sentry:***@" in redacted


def test_monitor_config_rejects_insecure_alert_and_missing_dependencies(
    monkeypatch,
):
    monkeypatch.setenv("DATABASE_URL", "postgresql://sentry:x@db:5432/sentry")
    monkeypatch.setenv("REDIS_URL", "redis://:x@redis:6379/0")
    monkeypatch.setenv("CELERY_BROKER_URL", "redis://:x@redis:6379/0")
    monkeypatch.setenv("SENTRY_OPS_ALERT_WEBHOOK_URL", "http://alerts.test/hook")
    monkeypatch.delenv("SENTRY_OPS_ALLOW_HTTP_ALERTS", raising=False)
    with pytest.raises(ValueError, match="HTTPS"):
        load_config()

    monkeypatch.delenv("DATABASE_URL")
    with pytest.raises(ValueError, match="DATABASE_URL"):
        load_config()


def test_probe_backup_accepts_utc_z_suffix(tmp_path):
    status_path = tmp_path / "backup-status.json"
    status_path.write_text(
        json.dumps(
            {
                "last_success_at": "2026-10-29T00:00:00Z",
                "last_failure_at": None,
            },
        ),
        encoding="utf-8",
    )
    result = probe_backup(
        status_path,
        30,
        now=datetime(2026, 10, 29, 1, tzinfo=UTC),
    )
    assert result.healthy is True
