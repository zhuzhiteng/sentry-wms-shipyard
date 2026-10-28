"""Shared helpers for the production operations sidecars."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("sentry.ops")


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_iso(now: datetime | None = None) -> str:
    value = now or utc_now()
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def redact_text(value: str) -> str:
    """Remove common password forms before persisting or alerting."""
    redacted = re.sub(
        r"(?i)(password=)[^\s&]+",
        r"\1***",
        value,
    )
    return re.sub(
        r"(?<=://)([^/@\s]*):[^@\s]+@",
        r"\1:***@",
        redacted,
    )


def bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return default


def touch_heartbeat(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def heartbeat_is_fresh(
    path: Path,
    max_age_seconds: int,
    *,
    now: float | None = None,
) -> bool:
    try:
        modified = path.stat().st_mtime
    except OSError:
        return False
    current = time.time() if now is None else now
    return current - modified <= max_age_seconds


def validate_webhook_url(url: str, *, allow_http: bool) -> str:
    if not url:
        return ""
    parsed = urlsplit(url)
    if parsed.scheme == "https" and parsed.netloc:
        return url
    if allow_http and parsed.scheme == "http" and parsed.netloc:
        return url
    raise ValueError("SENTRY_OPS_ALERT_WEBHOOK_URL must be an HTTPS URL")


def send_alert(
    webhook_url: str,
    text: str,
    *,
    timeout_seconds: int,
    allow_http: bool,
    bearer_token: str = "",
    opener=urlopen,
) -> bool:
    """Send a small generic/Teams-compatible alert payload."""
    url = validate_webhook_url(webhook_url, allow_http=allow_http)
    if not url:
        return False
    request = Request(
        url,
        data=json.dumps({"text": text}, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    if bearer_token:
        request.add_header("Authorization", f"Bearer {bearer_token}")
    response = opener(request, timeout=timeout_seconds)
    try:
        status = int(getattr(response, "status", 200))
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()
    if not 200 <= status < 300:
        raise RuntimeError(f"alert webhook returned HTTP {status}")
    return True
