"""Tests for the read-only deployment smoke checker."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from scripts.shipyard_deployment_smoke import (
    SmokeCheckError,
    SmokeConfig,
    normalize_api_base,
    run_smoke,
)


class _SmokeHandler(BaseHTTPRequestHandler):
    proxy_fix_active = True
    seen_authorization = []

    def log_message(self, _format, *_args):
        return

    def _json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/api/auth/login":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        assert payload == {"username": "smoke-admin", "password": "test-password"}
        self._json(200, {"token": "smoke-token"})

    def do_GET(self):
        if self.path == "/api/health":
            self._json(200, {"status": "ok", "service": "sentry-wms"})
            return
        if self.path == "/api/admin/system-info":
            self.seen_authorization.append(self.headers.get("Authorization"))
            self._json(
                200,
                {
                    "version": "1.30.0",
                    "proxy_fix_active": self.proxy_fix_active,
                },
            )
            return
        if self.path == "/api/shipyard/inbound/manifests?warehouse_id=1":
            self._json(200, {"manifests": []})
            return
        if self.path == "/api/shipyard/outbound/mobile/orders?warehouse_id=1":
            self._json(200, {"orders": []})
            return
        if self.path == "/":
            body = b"<html><body>admin</body></html>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json(404, {"error": "not found"})


@pytest.fixture()
def smoke_server():
    _SmokeHandler.proxy_fix_active = True
    _SmokeHandler.seen_authorization = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _SmokeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_normalize_api_base_requires_exact_api_path_and_https():
    assert (
        normalize_api_base("https://wms.example.com/api/")
        == "https://wms.example.com/api"
    )
    with pytest.raises(ValueError, match="HTTPS"):
        normalize_api_base("http://wms.example.com/api")
    with pytest.raises(ValueError, match="/api"):
        normalize_api_base("https://wms.example.com/api/v1")


def test_read_only_smoke_passes_against_expected_deployment(smoke_server):
    report = run_smoke(
        SmokeConfig(
            api_base=f"{smoke_server}/api",
            admin_url=f"{smoke_server}/",
            username="smoke-admin",
            password="test-password",
            warehouse_id=1,
            timeout_seconds=5,
            expected_version="1.30.0",
            require_proxy=True,
        )
    )

    assert report["status"] == "passed"
    assert {item["id"] for item in report["checks"]} == {
        "health",
        "admin-login",
        "system-info",
        "shipyard-inbound-read",
        "shipyard-outbound-read",
        "admin-page",
    }
    assert _SmokeHandler.seen_authorization == ["Bearer smoke-token"]


def test_smoke_blocks_missing_reverse_proxy_detection(smoke_server):
    _SmokeHandler.proxy_fix_active = False
    with pytest.raises(SmokeCheckError, match="反向代理"):
        run_smoke(
            SmokeConfig(
                api_base=f"{smoke_server}/api",
                admin_url="",
                username="smoke-admin",
                password="test-password",
                warehouse_id=1,
                timeout_seconds=5,
                expected_version="1.30.0",
                require_proxy=True,
            )
        )
