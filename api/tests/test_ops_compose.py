"""Guardrails for production operations Compose wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load_compose(name: str) -> dict:
    path = ROOT / name
    if not path.exists():
        pytest.skip("repository-root Compose files are unavailable in the API image")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_production_api_builds_from_repo_root_for_mapping_copy():
    prod = _load_compose("docker-compose.prod.yml")
    assert prod["services"]["api"]["build"] == {
        "context": ".",
        "dockerfile": "./api/Dockerfile",
    }
    assert prod["services"]["celery-worker"]["build"] == {
        "context": ".",
        "dockerfile": "./api/Dockerfile",
    }


def test_production_forwards_required_secrets_and_loopback_bindings():
    prod = _load_compose("docker-compose.prod.yml")
    api_env = prod["services"]["api"]["environment"]
    for key in (
        "SENTRY_TOKEN_PEPPER",
        "SENTRY_PUBSUB_HMAC_KEY",
        "REDIS_URL",
        "SHIPYARD_OCR_PROVIDER",
    ):
        assert key in api_env
    assert prod["services"]["api"]["ports"] == [
        "${API_BIND_HOST:-127.0.0.1}:5000:5000",
    ]
    assert prod["services"]["admin"]["ports"] == [
        "${ADMIN_BIND_HOST:-127.0.0.1}:8080:8080",
    ]


def test_ops_overlay_has_monitor_backup_healthchecks_and_shared_state():
    ops = _load_compose("docker-compose.ops.yml")
    assert ops["services"]["ops-backup"]["build"]["dockerfile"] == (
        "./ops/backup.Dockerfile"
    )
    assert "healthcheck" in ops["services"]["ops-monitor"]
    assert "healthcheck" in ops["services"]["ops-backup"]
    assert set(ops["services"]["ops-monitor"]["depends_on"]) == {"ops-init"}
    assert "opsstate:/state" in ops["services"]["ops-monitor"]["volumes"]
    assert "backupdata:/backups" in ops["services"]["ops-backup"]["volumes"]
    assert (
        ":?"
        in (
            ops["services"]["ops-monitor"]["environment"][
                "SENTRY_OPS_ALERT_WEBHOOK_URL"
            ]
        )
    )
    assert "backupdata" in ops["volumes"]


def test_backup_image_is_pinned_to_postgresql_16():
    dockerfile = ROOT / "ops" / "backup.Dockerfile"
    if not dockerfile.exists():
        pytest.skip("repository-root ops image is unavailable in the API image")
    content = dockerfile.read_text(encoding="utf-8")
    assert "FROM postgres:16" in content
    assert "COPY api/ops /app/ops" in content
