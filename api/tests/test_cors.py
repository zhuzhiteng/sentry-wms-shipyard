"""Default CORS origins for the retained admin and mini-program clients."""

from app import DEFAULT_CORS_ORIGINS, resolve_cors_origins


def test_retired_expo_dev_origin_is_not_allowed_by_default():
    assert "http://localhost:8081" not in DEFAULT_CORS_ORIGINS


def test_admin_dev_origin_remains_allowed_by_default():
    assert "http://localhost:3000" in DEFAULT_CORS_ORIGINS


def test_explicit_cors_override_is_trimmed():
    assert resolve_cors_origins(" https://one.example, ,https://two.example ") == [
        "https://one.example",
        "https://two.example",
    ]
