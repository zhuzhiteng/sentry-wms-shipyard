"""Local-first OCR with optional cloud fallback for shipyard labels."""

from __future__ import annotations

import base64
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import requests

from services.shipyard.errors import OcrProviderError, OcrUnavailableError

BAIDU_OAUTH_URL = "https://aip.baidubce.com/oauth/2.0/token"
BAIDU_OCR_URL = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"

DEFAULT_PROVIDER = "rapidocr"
SUPPORTED_PROVIDERS = frozenset({"rapidocr", "baidu"})

_TOKEN_LOCK = threading.Lock()
_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_RAPIDOCR_LOCK = threading.Lock()
_RAPIDOCR_ENGINE = None
_LOGGER = logging.getLogger(__name__)

_FIELD_ALIASES = {
    "船号": "ship_no",
    "工程号": "engineering_no",
    "制作图号": "production_drawing_no",
    "制作图编号": "production_drawing_no",
    "图号": "production_drawing_no",
    "物料编码": "material_code",
    "物资编码": "material_code",
    "品名": "item_name",
    "物料名称": "item_name",
    "规格": "item_spec",
    "规格型号": "item_spec",
    "型号": "item_spec",
    "批次号": "batch_no",
    "批次": "batch_no",
    "托盘后缀": "tag_suffix",
    "吊牌后缀": "tag_suffix",
    "安装托盘号": "installation_pallet_no",
    "安装托盘名称": "installation_pallet_name",
    "二维码托盘号": "factory_pallet_no",
    "托盘号": "factory_pallet_no",
    "厂家": "manufacturer_name",
    "厂家名称": "manufacturer_name",
    "目的地": "destination",
    "数量": "label_quantity",
    "件数": "label_quantity",
    "ship_no": "ship_no",
    "engineering_no": "engineering_no",
    "production_drawing_no": "production_drawing_no",
    "drawing_no": "production_drawing_no",
    "material_code": "material_code",
    "item_name": "item_name",
    "material_name": "item_name",
    "item_spec": "item_spec",
    "batch_no": "batch_no",
    "tag_suffix": "tag_suffix",
    "suffix": "tag_suffix",
    "manufacturer_name": "manufacturer_name",
    "destination": "destination",
    "pallet_no": "factory_pallet_no",
    "pallet": "factory_pallet_no",
    "quantity": "label_quantity",
    "qty": "label_quantity",
}

_ALIASES_BY_LENGTH = sorted(_FIELD_ALIASES, key=len, reverse=True)
_SUFFIX_PATTERN = re.compile(r"(?<!\d)([1-9]\d*-[1-9]\d*)(?!\d)")
_SUFFIX_DASHES = str.maketrans(
    {
        "‐": "-",
        "‑": "-",
        "‒": "-",
        "–": "-",
        "—": "-",
        "−": "-",
        "－": "-",
    }
)
_COLON_VALUES = re.compile(
    rf"^({'|'.join(re.escape(item) for item in _ALIASES_BY_LENGTH)})"
    r"\s*[:=：]\s*(.+)$",
    re.IGNORECASE,
)
_SPACE_VALUES = re.compile(
    rf"^({'|'.join(re.escape(item) for item in _ALIASES_BY_LENGTH)})"
    r"\s+(.+)$",
    re.IGNORECASE,
)


def _timeout(name: str, default: float) -> float:
    try:
        return max(float(os.getenv(name, default)), 1.0)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class LabelOcrResult:
    provider: str
    raw_text: str
    raw_lines: list[str]
    fields: dict[str, Any]
    confidence: float | None


def _normalized_key(value: str) -> str:
    compact = re.sub(r"\s+", "", value or "").casefold()
    return _FIELD_ALIASES.get(compact, "")


def _clean_value(field: str, value: str) -> Any:
    text = value.strip().strip("：:=|;,，；")
    if field == "material_code":
        return re.sub(r"\s+", "", text)
    if field == "tag_suffix":
        match = _SUFFIX_PATTERN.search(text.translate(_SUFFIX_DASHES))
        return match.group(1) if match else ""
    if field == "label_quantity":
        number = text.replace(",", "").replace("件", "").strip()
        return number or ""
    return text


def parse_label_text(raw_text: str) -> dict[str, Any]:
    """Parse key/value label lines without guessing unrelated free text."""

    lines = [
        re.sub(r"\s+", " ", line).strip()
        for line in (raw_text or "").splitlines()
        if line.strip()
    ]
    fields: dict[str, Any] = {}
    for index, line in enumerate(lines):
        for segment in re.split(r"[|；;]", line):
            match = _COLON_VALUES.match(segment.strip()) or _SPACE_VALUES.match(
                segment.strip()
            )
            if match:
                field = _normalized_key(match.group(1))
                value = _clean_value(field, match.group(2))
                if field and value not in ("", None):
                    fields[field] = value
                continue
            exact_field = _normalized_key(segment.strip())
            if exact_field and index + 1 < len(lines):
                value = _clean_value(exact_field, lines[index + 1])
                if value not in ("", None):
                    fields[exact_field] = value

    suffix = _SUFFIX_PATTERN.search((raw_text or "").translate(_SUFFIX_DASHES))
    if suffix and not fields.get("tag_suffix"):
        fields["tag_suffix"] = suffix.group(1)
    return fields


def _provider(value: str | None = None) -> str:
    configured = value if value is not None else os.getenv("SHIPYARD_OCR_PROVIDER")
    return (configured or DEFAULT_PROVIDER).strip().casefold()


def _fallback_provider() -> str:
    return (os.getenv("SHIPYARD_OCR_FALLBACK_PROVIDER") or "").strip().casefold()


def _baidu_access_token(*, force_refresh: bool = False) -> str:
    api_key = (os.getenv("BAIDU_OCR_API_KEY") or "").strip()
    secret_key = (os.getenv("BAIDU_OCR_SECRET_KEY") or "").strip()
    if not api_key or not secret_key:
        raise OcrUnavailableError(
            "吊牌 OCR 尚未配置",
            {"required_env": ["BAIDU_OCR_API_KEY", "BAIDU_OCR_SECRET_KEY"]},
        )

    cache_key = f"{api_key}:{secret_key}"
    now = time.time()
    with _TOKEN_LOCK:
        cached = _TOKEN_CACHE.get(cache_key)
        if not force_refresh and cached and cached[1] > now:
            return cached[0]
        try:
            # _timeout() always returns a finite positive value.
            response = requests.post(  # nosec B113
                BAIDU_OAUTH_URL,
                data={
                    "grant_type": "client_credentials",
                    "client_id": api_key,
                    "client_secret": secret_key,
                },
                timeout=_timeout("SHIPYARD_OCR_TOKEN_TIMEOUT_S", 10),
            )
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise OcrProviderError("OCR 鉴权服务暂时不可用") from exc
        if not isinstance(payload, dict):
            raise OcrProviderError("OCR 鉴权服务返回格式异常")
        token = payload.get("access_token")
        if response.status_code != 200 or not token:
            raise OcrProviderError(
                "OCR 鉴权失败",
                {"provider_error": payload.get("error") or "authentication_failed"},
            )
        expires_at = now + max(int(payload.get("expires_in", 2592000)) - 60, 60)
        _TOKEN_CACHE[cache_key] = (token, expires_at)
        return token


def _baidu_ocr_request(content: bytes, token: str) -> tuple[int, dict[str, Any]]:
    try:
        # _timeout() always returns a finite positive value.
        response = requests.post(  # nosec B113
            BAIDU_OCR_URL,
            params={"access_token": token},
            data={
                "image": base64.b64encode(content).decode("ascii"),
                "language_type": "CHN_ENG",
                "detect_direction": "true",
                "probability": "true",
            },
            timeout=_timeout("SHIPYARD_OCR_TIMEOUT_S", 20),
        )
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise OcrProviderError("吊牌 OCR 服务暂时不可用") from exc
    if not isinstance(payload, dict):
        raise OcrProviderError("吊牌 OCR 服务返回格式异常")
    return response.status_code, payload


def _baidu_recognize(content: bytes) -> tuple[str, list[str], float | None]:
    token = _baidu_access_token()
    status_code, payload = _baidu_ocr_request(content, token)
    if str(payload.get("error_code") or "") in {"110", "111"}:
        token = _baidu_access_token(force_refresh=True)
        status_code, payload = _baidu_ocr_request(content, token)

    if status_code != 200 or payload.get("error_code"):
        raise OcrProviderError(
            "吊牌 OCR 识别失败",
            {"provider_error": payload.get("error_code") or status_code},
        )

    words = payload.get("words_result") or []
    lines = [
        str(item.get("words") or "").strip()
        for item in words
        if str(item.get("words") or "").strip()
    ]
    probabilities = [
        float(item["probability"]["average"])
        for item in words
        if isinstance(item.get("probability"), dict)
        and item["probability"].get("average") is not None
    ]
    confidence = (
        round(sum(probabilities) / len(probabilities), 4) if probabilities else None
    )
    return "\n".join(lines), lines, confidence


def _rapidocr_recognize(content: bytes) -> tuple[str, list[str], float | None]:
    global _RAPIDOCR_ENGINE

    # RapidOCR's ONNX pipeline is fast after warm-up but is not documented as
    # thread-safe. One lock per API process also prevents duplicate model loads
    # under concurrent uploads.
    with _RAPIDOCR_LOCK:
        if _RAPIDOCR_ENGINE is None:
            try:
                from rapidocr import RapidOCR
            except (ImportError, OSError) as exc:
                raise OcrUnavailableError(
                    "本地 OCR 引擎不可用",
                    {
                        "provider": "rapidocr",
                        "required_packages": ["rapidocr", "onnxruntime"],
                    },
                ) from exc
            try:
                _RAPIDOCR_ENGINE = RapidOCR()
            except Exception as exc:
                raise OcrUnavailableError(
                    "本地 OCR 引擎初始化失败",
                    {
                        "provider": "rapidocr",
                        "reason": type(exc).__name__,
                    },
                ) from exc

        try:
            result = _RAPIDOCR_ENGINE(content)
        except Exception as exc:
            raise OcrProviderError(
                "本地 OCR 识别失败",
                {
                    "provider": "rapidocr",
                    "reason": type(exc).__name__,
                },
            ) from exc

    texts = list(getattr(result, "txts", None) or [])
    scores = list(getattr(result, "scores", None) or [])
    lines = []
    probabilities = []
    for index, text in enumerate(texts):
        value = str(text or "").strip()
        if not value:
            continue
        lines.append(value)
        if index >= len(scores) or scores[index] is None:
            continue
        try:
            score = float(scores[index])
        except (TypeError, ValueError):
            continue
        if math.isfinite(score):
            probabilities.append(score)
    confidence = (
        round(sum(probabilities) / len(probabilities), 4) if probabilities else None
    )
    return "\n".join(lines), lines, confidence


def _recognize_with_provider(
    provider: str,
    content: bytes,
) -> LabelOcrResult:
    if provider not in SUPPORTED_PROVIDERS:
        raise OcrUnavailableError(
            "不支持的吊牌 OCR 服务",
            {"provider": provider},
        )
    recognizer = {
        "rapidocr": _rapidocr_recognize,
        "baidu": _baidu_recognize,
    }[provider]
    raw_text, raw_lines, confidence = recognizer(content)
    return LabelOcrResult(
        provider=provider,
        raw_text=raw_text,
        raw_lines=raw_lines,
        fields=parse_label_text(raw_text),
        confidence=confidence,
    )


def recognize_label_image(
    content: bytes,
    *,
    provider: str | None = None,
    allow_fallback: bool = True,
) -> LabelOcrResult:
    """Recognize one label, using cloud OCR only when explicitly configured.

    Fallback is limited to provider failures and empty primary results. Parsed
    field completeness remains a business validation concern because the same
    entry point also serves non-pallet four-part acceptance documents.
    """

    primary = _provider(provider)
    if primary not in SUPPORTED_PROVIDERS:
        raise OcrUnavailableError(
            "不支持的吊牌 OCR 服务",
            {"provider": primary},
        )

    fallback = _fallback_provider() if allow_fallback else ""
    if fallback and fallback not in SUPPORTED_PROVIDERS:
        _LOGGER.warning(
            "shipyard_ocr_fallback_ignored primary=%s reason=unsupported_provider",
            primary,
        )
        fallback = ""
    if fallback == primary:
        _LOGGER.warning(
            "shipyard_ocr_fallback_ignored primary=%s reason=same_provider",
            primary,
        )
        fallback = ""

    try:
        primary_result = _recognize_with_provider(primary, content)
        primary_error = None
    except (OcrUnavailableError, OcrProviderError) as exc:
        primary_result = None
        primary_error = exc

    if primary_result is not None and (primary_result.raw_text.strip() or not fallback):
        return primary_result

    if not fallback:
        if primary_error is not None:
            raise primary_error
        return primary_result

    fallback_reason = "provider_error" if primary_error is not None else "empty_result"
    try:
        fallback_result = _recognize_with_provider(fallback, content)
    except (OcrUnavailableError, OcrProviderError) as exc:
        _LOGGER.warning(
            "shipyard_ocr_fallback_failed primary=%s fallback=%s reason=%s "
            "fallback_error=%s",
            primary,
            fallback,
            fallback_reason,
            type(exc).__name__,
        )
        if primary_error is not None:
            raise primary_error
        # The primary engine completed correctly but found no text. A failed
        # optional fallback must not turn that usable result into an outage.
        return primary_result

    _LOGGER.warning(
        "shipyard_ocr_fallback_used primary=%s fallback=%s reason=%s",
        primary,
        fallback,
        fallback_reason,
    )
    return fallback_result
