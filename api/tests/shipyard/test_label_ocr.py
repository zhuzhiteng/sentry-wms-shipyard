"""Offline tests for local-first OCR and optional provider fallback."""

import sys
import types

import pytest
from services.shipyard import label_ocr
from services.shipyard.errors import OcrProviderError, OcrUnavailableError


def _configure_provider(monkeypatch, provider, fallback=""):
    monkeypatch.setenv("SHIPYARD_OCR_PROVIDER", provider)
    if fallback:
        monkeypatch.setenv("SHIPYARD_OCR_FALLBACK_PROVIDER", fallback)
    else:
        monkeypatch.delenv("SHIPYARD_OCR_FALLBACK_PROVIDER", raising=False)


def _rapid_result(*lines, scores=None):
    return types.SimpleNamespace(
        txts=lines,
        scores=list(scores if scores is not None else [0.98] * len(lines)),
    )


def test_parse_label_text_supports_colons_spaces_next_line_and_suffix():
    fields = label_ocr.parse_label_text(
        """
船号：H1613
工程号 H1613
制作图号
D-001
物料编码： MAT 001
品名：船用支架
规格型号：S-100
厂家名称：测试船舶配件有限公司
安装托盘号：P-01
数量：4件
1－2
"""
    )

    assert fields == {
        "ship_no": "H1613",
        "engineering_no": "H1613",
        "production_drawing_no": "D-001",
        "material_code": "MAT001",
        "item_name": "船用支架",
        "item_spec": "S-100",
        "manufacturer_name": "测试船舶配件有限公司",
        "installation_pallet_no": "P-01",
        "label_quantity": "4",
        "tag_suffix": "1-2",
    }


def test_rapidocr_is_lazy_and_reused_without_baidu_credentials(
    monkeypatch,
):
    init_calls = []
    inference_calls = []

    class Engine:
        def __init__(self):
            init_calls.append("init")

        def __call__(self, content):
            inference_calls.append(content)
            return _rapid_result(
                "物料编码：MAT-001",
                "品名：船用支架",
                "托盘后缀：1-2",
                scores=[0.95, 0.99, 0.97],
            )

    fake_module = types.ModuleType("rapidocr")
    fake_module.RapidOCR = Engine
    monkeypatch.setitem(sys.modules, "rapidocr", fake_module)
    monkeypatch.setattr(label_ocr, "_RAPIDOCR_ENGINE", None)
    monkeypatch.delenv("BAIDU_OCR_API_KEY", raising=False)
    monkeypatch.delenv("BAIDU_OCR_SECRET_KEY", raising=False)
    monkeypatch.delenv("SHIPYARD_OCR_PROVIDER", raising=False)
    monkeypatch.delenv("SHIPYARD_OCR_FALLBACK_PROVIDER", raising=False)

    first = label_ocr.recognize_label_image(b"first")
    second = label_ocr.recognize_label_image(b"second")

    assert first.provider == "rapidocr"
    assert first.fields == {
        "material_code": "MAT-001",
        "item_name": "船用支架",
        "tag_suffix": "1-2",
    }
    assert first.confidence == 0.97
    assert second.provider == "rapidocr"
    assert init_calls == ["init"]
    assert inference_calls == [b"first", b"second"]


def test_rapidocr_empty_result_uses_configured_baidu_fallback(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(
        label_ocr,
        "_RAPIDOCR_ENGINE",
        lambda _content: _rapid_result(),
    )
    monkeypatch.setattr(
        label_ocr,
        "_baidu_recognize",
        lambda _content: (
            "物料编码：MAT-001\n品名：船用支架\n托盘后缀：1-1",
            ["物料编码：MAT-001", "品名：船用支架", "托盘后缀：1-1"],
            0.98,
        ),
    )
    _configure_provider(monkeypatch, "rapidocr", "baidu")

    result = label_ocr.recognize_label_image(b"image")

    assert result.provider == "baidu"
    assert result.fields["tag_suffix"] == "1-1"
    assert "shipyard_ocr_fallback_used" in caplog.text
    assert "reason=empty_result" in caplog.text


def test_rapidocr_failure_uses_configured_baidu_fallback(monkeypatch):
    def fail(_content):
        raise RuntimeError("onnx failed")

    monkeypatch.setattr(label_ocr, "_RAPIDOCR_ENGINE", fail)
    monkeypatch.setattr(
        label_ocr,
        "_baidu_recognize",
        lambda _content: (
            "物料编码：MAT-002\n品名：法兰\n托盘后缀：2-1",
            ["物料编码：MAT-002", "品名：法兰", "托盘后缀：2-1"],
            0.96,
        ),
    )
    _configure_provider(monkeypatch, "rapidocr", "baidu")

    result = label_ocr.recognize_label_image(b"image")

    assert result.provider == "baidu"
    assert result.fields["material_code"] == "MAT-002"


def test_failed_optional_fallback_does_not_break_empty_local_result(
    monkeypatch,
):
    monkeypatch.setattr(
        label_ocr,
        "_RAPIDOCR_ENGINE",
        lambda _content: _rapid_result(),
    )

    def unavailable(_content):
        raise OcrUnavailableError(
            "百度 OCR 尚未配置",
            {"required_env": ["BAIDU_OCR_API_KEY", "BAIDU_OCR_SECRET_KEY"]},
        )

    monkeypatch.setattr(label_ocr, "_baidu_recognize", unavailable)
    _configure_provider(monkeypatch, "rapidocr", "baidu")

    result = label_ocr.recognize_label_image(b"image")

    assert result.provider == "rapidocr"
    assert result.raw_text == ""


def test_baidu_without_credentials_is_reported_as_unavailable(monkeypatch):
    monkeypatch.delenv("BAIDU_OCR_API_KEY", raising=False)
    monkeypatch.delenv("BAIDU_OCR_SECRET_KEY", raising=False)
    _configure_provider(monkeypatch, "baidu")

    with pytest.raises(OcrUnavailableError) as raised:
        label_ocr.recognize_label_image(b"image")

    assert raised.value.code == "ocr_unavailable"
    assert raised.value.details["required_env"] == [
        "BAIDU_OCR_API_KEY",
        "BAIDU_OCR_SECRET_KEY",
    ]


def test_unsupported_provider_is_rejected(monkeypatch):
    _configure_provider(monkeypatch, "stub")

    with pytest.raises(OcrUnavailableError) as raised:
        label_ocr.recognize_label_image(b"image")

    assert raised.value.details["provider"] == "stub"


def test_same_fallback_provider_is_ignored(monkeypatch):
    monkeypatch.setattr(
        label_ocr,
        "_RAPIDOCR_ENGINE",
        lambda _content: _rapid_result("物料编码：MAT-001"),
    )
    _configure_provider(monkeypatch, "rapidocr", "rapidocr")

    result = label_ocr.recognize_label_image(b"image")

    assert result.provider == "rapidocr"


def test_baidu_provider_contract_is_normalized_without_network(monkeypatch):
    calls = []

    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url == label_ocr.BAIDU_OAUTH_URL:
            return Response(
                200,
                {"access_token": "test-token", "expires_in": 3600},
            )
        return Response(
            200,
            {
                "words_result": [
                    {
                        "words": "物料编码：MAT-001",
                        "probability": {"average": 0.97},
                    },
                    {
                        "words": "品名：船用支架",
                        "probability": {"average": 0.99},
                    },
                    {"words": "托盘后缀：1-2"},
                ]
            },
        )

    monkeypatch.setenv("BAIDU_OCR_API_KEY", "test-key")
    monkeypatch.setenv("BAIDU_OCR_SECRET_KEY", "test-secret")
    monkeypatch.setattr(label_ocr.requests, "post", fake_post)
    _configure_provider(monkeypatch, "baidu")
    label_ocr._TOKEN_CACHE.clear()

    result = label_ocr.recognize_label_image(b"image")

    assert result.provider == "baidu"
    assert result.fields == {
        "material_code": "MAT-001",
        "item_name": "船用支架",
        "tag_suffix": "1-2",
    }
    assert result.confidence == 0.98
    assert len(calls) == 2
    assert calls[1][1]["params"]["access_token"] == "test-token"
    assert calls[1][1]["data"]["image"] == "aW1hZ2U="


def test_baidu_retries_once_after_an_expired_access_token(monkeypatch):
    calls = []

    class Response:
        def __init__(self, payload):
            self.status_code = 200
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        if url == label_ocr.BAIDU_OAUTH_URL:
            token = "expired-token" if len(calls) == 1 else "fresh-token"
            return Response({"access_token": token, "expires_in": 3600})
        token = kwargs["params"]["access_token"]
        if token == "expired-token":
            return Response({"error_code": 110, "error_msg": "expired"})
        return Response(
            {
                "words_result": [
                    {"words": "物料编码：MAT-001"},
                    {"words": "品名：船用支架"},
                    {"words": "规格：S-100"},
                    {"words": "托盘后缀：1-1"},
                ]
            }
        )

    monkeypatch.setenv("BAIDU_OCR_API_KEY", "test-key")
    monkeypatch.setenv("BAIDU_OCR_SECRET_KEY", "test-secret")
    monkeypatch.setattr(label_ocr.requests, "post", fake_post)
    _configure_provider(monkeypatch, "baidu")
    label_ocr._TOKEN_CACHE.clear()

    result = label_ocr.recognize_label_image(b"image")

    assert result.fields["item_spec"] == "S-100"
    assert [call[0] for call in calls] == [
        label_ocr.BAIDU_OAUTH_URL,
        label_ocr.BAIDU_OCR_URL,
        label_ocr.BAIDU_OAUTH_URL,
        label_ocr.BAIDU_OCR_URL,
    ]
    assert calls[3][1]["params"]["access_token"] == "fresh-token"


def test_baidu_timeout_is_reported_as_provider_error(monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"access_token": "test-token", "expires_in": 3600}

    def fake_post(url, **kwargs):
        if url == label_ocr.BAIDU_OAUTH_URL:
            return Response()
        raise label_ocr.requests.Timeout("timed out")

    monkeypatch.setenv("BAIDU_OCR_API_KEY", "test-key")
    monkeypatch.setenv("BAIDU_OCR_SECRET_KEY", "test-secret")
    monkeypatch.setattr(label_ocr.requests, "post", fake_post)
    _configure_provider(monkeypatch, "baidu")
    label_ocr._TOKEN_CACHE.clear()

    with pytest.raises(OcrProviderError) as raised:
        label_ocr.recognize_label_image(b"image")

    assert raised.value.code == "ocr_provider_error"
    assert raised.value.message == "吊牌 OCR 服务暂时不可用"
