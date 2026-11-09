#!/usr/bin/env python3
"""Read-only smoke checks for a deployed shipyard WMS environment."""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class SmokeConfigurationError(ValueError):
    """The smoke request is unsafe or incomplete."""


class SmokeCheckError(RuntimeError):
    """One deployment check failed."""


@dataclass(frozen=True)
class SmokeConfig:
    api_base: str
    admin_url: str
    username: str
    password: str
    warehouse_id: int
    timeout_seconds: float
    expected_version: str
    require_proxy: bool


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def normalize_api_base(value: str, *, allow_http: bool = False) -> str:
    text = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ({"https"} | ({"http"} if allow_http else set())):
        raise SmokeConfigurationError(
            "API 地址必须是正式 HTTPS（本地测试可显式允许 HTTP）"
        )
    if not parsed.hostname:
        raise SmokeConfigurationError("API 地址缺少主机名")
    if parsed.username or parsed.password:
        raise SmokeConfigurationError("API 地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise SmokeConfigurationError("API 地址不能包含查询参数或片段")
    if parsed.path.rstrip("/") != "/api":
        raise SmokeConfigurationError("API 根地址路径必须是 /api")
    if not allow_http and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        raise SmokeConfigurationError("正式校验不能使用本机地址")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/api", "", ""))


def normalize_admin_url(value: str, *, allow_http: bool = False) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ({"https"} | ({"http"} if allow_http else set())):
        raise SmokeConfigurationError("管理端地址必须是正式 HTTPS")
    if not parsed.hostname:
        raise SmokeConfigurationError("管理端地址缺少主机名")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SmokeConfigurationError("管理端地址不能包含凭据、查询参数或片段")
    if not allow_http and parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
        raise SmokeConfigurationError("正式校验不能使用本机管理端地址")
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path.rstrip("/") or "/", "", "")
    )


def _safe_message(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    for marker in ("password", "token", "authorization", "secret"):
        if marker in text.casefold():
            return "响应内容包含敏感字段，已隐藏"
    return text[:300]


def request_json(
    url: str,
    *,
    method: str = "GET",
    token: str = "",
    payload: dict[str, Any] | None = None,
    timeout: float = 15,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=ssl.create_default_context(),
        ) as response:
            raw = response.read()
            return (
                response.status,
                json.loads(raw.decode("utf-8")) if raw else {},
                {key.casefold(): value for key, value in response.headers.items()},
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeError, json.JSONDecodeError):
            data = {}
        return (
            exc.code,
            data,
            {key.casefold(): value for key, value in exc.headers.items()},
        )
    except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
        raise SmokeCheckError(
            f"无法连接 {urllib.parse.urlsplit(url).hostname}: {_safe_message(exc)}"
        ) from exc


def request_admin_page(
    url: str, *, timeout: float = 15
) -> tuple[int, dict[str, str], str]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(
            request,
            timeout=timeout,
            context=ssl.create_default_context(),
        ) as response:
            return (
                response.status,
                {key.casefold(): value for key, value in response.headers.items()},
                response.read(2048).decode("utf-8", errors="replace"),
            )
    except urllib.error.HTTPError as exc:
        return (
            exc.code,
            {key.casefold(): value for key, value in exc.headers.items()},
            "",
        )
    except (urllib.error.URLError, TimeoutError, ssl.SSLError) as exc:
        raise SmokeCheckError(f"无法读取管理端: {_safe_message(exc)}") from exc


def _expect(status: int, expected: int, label: str, payload: dict[str, Any]) -> None:
    if status != expected:
        message = str(payload.get("message") or payload.get("error") or "").strip()
        suffix = f"：{message[:160]}" if message else ""
        raise SmokeCheckError(f"{label} 返回 {status}，预期 {expected}{suffix}")


def run_smoke(config: SmokeConfig) -> dict[str, Any]:
    started = time.monotonic()
    checks: list[dict[str, Any]] = []

    status, payload, _headers = request_json(
        f"{config.api_base}/health",
        timeout=config.timeout_seconds,
    )
    _expect(status, 200, "健康检查", payload)
    if payload.get("status") != "ok" or payload.get("service") != "sentry-wms":
        raise SmokeCheckError("健康检查响应不是预期的 sentry-wms 服务")
    checks.append({"id": "health", "status": "passed"})

    status, payload, _headers = request_json(
        f"{config.api_base}/auth/login",
        method="POST",
        payload={"username": config.username, "password": config.password},
        timeout=config.timeout_seconds,
    )
    _expect(status, 200, "管理员登录", payload)
    token = str(payload.get("token") or "").strip()
    if not token:
        raise SmokeCheckError("管理员登录响应没有 token")
    checks.append({"id": "admin-login", "status": "passed"})

    status, payload, _headers = request_json(
        f"{config.api_base}/admin/system-info",
        token=token,
        timeout=config.timeout_seconds,
    )
    _expect(status, 200, "系统信息", payload)
    version = str(payload.get("version") or "").strip()
    if not version:
        raise SmokeCheckError("系统信息未返回版本号")
    if config.expected_version and version != config.expected_version:
        raise SmokeCheckError(
            f"部署版本不一致：期望 {config.expected_version}，实际 {version}"
        )
    if config.require_proxy and payload.get("proxy_fix_active") is not True:
        raise SmokeCheckError("反向代理识别未生效，TRUST_PROXY 或代理转发头配置错误")
    checks.append(
        {
            "id": "system-info",
            "status": "passed",
            "version": version,
            "proxy_fix_active": payload.get("proxy_fix_active") is True,
        }
    )

    warehouse_query = urllib.parse.urlencode({"warehouse_id": config.warehouse_id})
    status, payload, _headers = request_json(
        f"{config.api_base}/shipyard/inbound/manifests?{warehouse_query}",
        token=token,
        timeout=config.timeout_seconds,
    )
    _expect(status, 200, "到货清单读取", payload)
    if not isinstance(payload.get("manifests"), list):
        raise SmokeCheckError("到货清单响应缺少 manifests 列表")
    checks.append({"id": "shipyard-inbound-read", "status": "passed"})

    status, payload, _headers = request_json(
        f"{config.api_base}/shipyard/outbound/mobile/orders?{warehouse_query}",
        token=token,
        timeout=config.timeout_seconds,
    )
    _expect(status, 200, "出库任务读取", payload)
    if not isinstance(payload.get("orders"), list):
        raise SmokeCheckError("出库任务响应缺少 orders 列表")
    checks.append({"id": "shipyard-outbound-read", "status": "passed"})

    if config.admin_url:
        status, headers, body = request_admin_page(
            config.admin_url,
            timeout=config.timeout_seconds,
        )
        _expect(status, 200, "管理端页面", {})
        if "text/html" not in headers.get("content-type", "").casefold():
            raise SmokeCheckError("管理端入口未返回 HTML")
        if "<html" not in body.casefold():
            raise SmokeCheckError("管理端入口内容不是 HTML 页面")
        for header in ("content-security-policy", "x-content-type-options"):
            if not headers.get(header):
                raise SmokeCheckError(f"管理端缺少安全响应头 {header}")
        checks.append({"id": "admin-page", "status": "passed"})

    return {
        "status": "passed",
        "generated_at": utc_now(),
        "api_origin": urllib.parse.urlsplit(config.api_base).netloc,
        "warehouse_id": config.warehouse_id,
        "elapsed_ms": round((time.monotonic() - started) * 1000, 1),
        "checks": checks,
    }


def load_config(args: argparse.Namespace, environ=None) -> SmokeConfig:
    environ = environ if environ is not None else os.environ
    username = str(environ.get(args.username_env) or "").strip()
    password = str(environ.get(args.password_env) or "")
    if not username:
        raise SmokeConfigurationError(f"缺少环境变量 {args.username_env}")
    if not password:
        raise SmokeConfigurationError(f"缺少环境变量 {args.password_env}")
    return SmokeConfig(
        api_base=normalize_api_base(args.api_base, allow_http=args.allow_http),
        admin_url=normalize_admin_url(
            args.admin_url,
            allow_http=args.allow_http,
        ),
        username=username,
        password=password,
        warehouse_id=args.warehouse_id,
        timeout_seconds=args.timeout,
        expected_version=args.expected_version,
        require_proxy=not args.allow_unproxied,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="对已部署的船厂 WMS 执行只读上线冒烟检查",
    )
    parser.add_argument("--api-base", required=True)
    parser.add_argument("--admin-url", default="")
    parser.add_argument("--username-env", default="SHIPYARD_SMOKE_USERNAME")
    parser.add_argument("--password-env", default="SHIPYARD_SMOKE_PASSWORD")
    parser.add_argument("--warehouse-id", type=int, default=1)
    parser.add_argument("--expected-version", default="")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--allow-unproxied", action="store_true")
    parser.add_argument("--report", default="")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args)
        report = run_smoke(config)
        if args.report:
            report_path = Path(args.report).expanduser().resolve()
            if report_path.exists() and not args.overwrite:
                raise SmokeConfigurationError(f"报告已存在: {report_path}")
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except SmokeConfigurationError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except SmokeCheckError as exc:
        print(f"冒烟检查失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
