#!/usr/bin/env python3
"""Evaluate real, de-identified shipyard label photos with the configured OCR."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.shipyard.errors import (
    OcrProviderError,
    OcrUnavailableError,
)
from services.shipyard.label_ocr import (
    LabelOcrResult,
    recognize_label_image,
)

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024
EXPECTED_FIELDS = {
    "ship_no",
    "engineering_no",
    "production_drawing_no",
    "material_code",
    "item_name",
    "item_spec",
    "batch_no",
    "tag_suffix",
    "factory_pallet_no",
    "installation_pallet_no",
    "installation_pallet_name",
    "manufacturer_name",
    "destination",
    "label_quantity",
}
OCR_ENV_KEYS = {
    "SHIPYARD_OCR_PROVIDER",
    "BAIDU_OCR_API_KEY",
    "BAIDU_OCR_SECRET_KEY",
    "SHIPYARD_OCR_TOKEN_TIMEOUT_S",
    "SHIPYARD_OCR_TIMEOUT_S",
}
_DASHES = str.maketrans(
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


class AcceptanceConfigError(ValueError):
    """The acceptance input is unsafe or incomplete."""


@dataclass(frozen=True)
class Sample:
    file_name: str
    path: Path
    expected: dict[str, str]
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class Dataset:
    name: str
    samples: tuple[Sample, ...]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _clean_text(value: Any) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", str(value or "")).strip(),
    )


def normalize_field_value(field: str, value: Any) -> str:
    """Normalize OCR and human-entered values for fair exact comparisons."""

    text = _clean_text(value).casefold()
    if field == "material_code":
        return re.sub(r"\s+", "", text)
    if field == "tag_suffix":
        return re.sub(r"\s+", "", text.translate(_DASHES))
    if field == "label_quantity":
        try:
            return format(
                Decimal(text.replace(",", "")).normalize(),
                "f",
            )
        except (InvalidOperation, ValueError):
            return text
    return text


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_env_file(path: Path, *, environ=None) -> None:
    """Load only OCR-related keys from a dotenv file without printing values."""

    environ = environ if environ is not None else os.environ
    if not path.is_file():
        raise AcceptanceConfigError(f"凭据文件不存在: {path}")
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise AcceptanceConfigError(
                f"凭据文件第 {line_number} 行不是 KEY=VALUE 格式"
            )
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in OCR_ENV_KEYS:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if value:
            environ.setdefault(key, value)


def require_ocr_configuration(environ=None) -> None:
    environ = environ if environ is not None else os.environ
    provider = (environ.get("SHIPYARD_OCR_PROVIDER") or "baidu").strip().casefold()
    if provider != "baidu":
        raise AcceptanceConfigError(f"不支持的 OCR provider: {provider}")
    missing = [
        key
        for key in ("BAIDU_OCR_API_KEY", "BAIDU_OCR_SECRET_KEY")
        if not (environ.get(key) or "").strip()
    ]
    if missing:
        raise AcceptanceConfigError("缺少真实 OCR 凭据: " + ", ".join(missing))


def _sample_path(input_dir: Path, file_name: Any) -> Path:
    if not isinstance(file_name, str) or not file_name.strip():
        raise AcceptanceConfigError("samples[].file 必须是非空相对路径")
    candidate = (input_dir / file_name).resolve()
    try:
        candidate.relative_to(input_dir)
    except ValueError as exc:
        raise AcceptanceConfigError(f"样图路径越出输入目录: {file_name}") from exc
    if candidate.suffix.casefold() not in SUPPORTED_EXTENSIONS:
        raise AcceptanceConfigError(
            f"不支持的图片格式: {file_name}；仅接受 JPEG、PNG、WebP"
        )
    if not candidate.is_file():
        raise AcceptanceConfigError(f"样图不存在: {file_name}")
    if candidate.stat().st_size > MAX_IMAGE_BYTES:
        raise AcceptanceConfigError(f"样图超过 10 MiB: {file_name}")
    return candidate


def load_dataset(input_dir: Path, expectations_path: Path) -> Dataset:
    input_dir = input_dir.resolve()
    if not input_dir.is_dir():
        raise AcceptanceConfigError(f"样图目录不存在: {input_dir}")
    if not expectations_path.is_file():
        raise AcceptanceConfigError(f"期望清单不存在: {expectations_path}")
    try:
        payload = json.loads(expectations_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AcceptanceConfigError(f"期望清单不是有效 JSON: {exc}") from exc

    if isinstance(payload, list):
        name = expectations_path.stem
        raw_samples = payload
    elif isinstance(payload, dict):
        name = str(payload.get("dataset") or expectations_path.stem).strip()
        raw_samples = payload.get("samples")
    else:
        raise AcceptanceConfigError("期望清单必须是 JSON 数组或含 samples 的对象")
    if not isinstance(raw_samples, list) or not raw_samples:
        raise AcceptanceConfigError("期望清单至少需要一个 sample")

    samples = []
    seen_paths = set()
    for index, item in enumerate(raw_samples):
        if not isinstance(item, dict):
            raise AcceptanceConfigError(f"samples[{index}] 必须是对象")
        file_name = item.get("file")
        expected = item.get("expected")
        if not isinstance(expected, dict) or not expected:
            raise AcceptanceConfigError(f"samples[{index}].expected 必须是非空对象")
        normalized_expected = {}
        for field, value in expected.items():
            if field not in EXPECTED_FIELDS:
                raise AcceptanceConfigError(
                    f"samples[{index}].expected 含未知字段: {field}"
                )
            text = _clean_text(value)
            if not text:
                raise AcceptanceConfigError(
                    f"samples[{index}].expected.{field} 不能为空"
                )
            normalized_expected[field] = text
        path = _sample_path(input_dir, file_name)
        if path in seen_paths:
            raise AcceptanceConfigError(f"样图重复登记: {file_name}")
        seen_paths.add(path)
        samples.append(
            Sample(
                file_name=str(file_name),
                path=path,
                expected=normalized_expected,
                sha256=_sha256(path),
                size_bytes=path.stat().st_size,
            )
        )
    return Dataset(name=name, samples=tuple(samples))


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(math.ceil(fraction * len(ordered)) - 1, 0)
    return round(ordered[index], 3)


def _rate(matches: int, total: int) -> float:
    return round(matches / total, 4) if total else 0.0


def evaluate_dataset(
    dataset: Dataset,
    *,
    recognizer: Callable[[bytes], LabelOcrResult] | None = None,
    interval_seconds: float = 0.0,
    minimum_image_pass_rate: float = 1.0,
    minimum_field_accuracy: float = 1.0,
) -> dict[str, Any]:
    recognizer = recognizer or recognize_label_image
    sample_reports = []
    durations = []
    matched_fields = 0
    total_fields = 0
    passed_samples = 0

    for index, sample in enumerate(dataset.samples):
        started = time.perf_counter()
        try:
            result = recognizer(sample.path.read_bytes())
            error = None
        except (OcrUnavailableError, OcrProviderError) as exc:
            result = None
            error = {"code": exc.code, "message": exc.message}
        except Exception as exc:  # noqa: BLE001 - the audit must report every sample
            result = None
            error = {
                "code": "unexpected_error",
                "message": type(exc).__name__,
            }
        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        durations.append(elapsed_ms)

        recognized = dict(result.fields) if result else {}
        field_results = []
        for field, expected in sample.expected.items():
            actual = recognized.get(field)
            passed = normalize_field_value(field, expected) == normalize_field_value(
                field,
                actual,
            )
            matched_fields += int(passed)
            total_fields += 1
            field_results.append(
                {
                    "field": field,
                    "expected": expected,
                    "actual": actual,
                    "passed": passed,
                }
            )
        sample_passed = error is None and all(item["passed"] for item in field_results)
        passed_samples += int(sample_passed)
        sample_reports.append(
            {
                "file": sample.file_name,
                "sha256": sample.sha256,
                "size_bytes": sample.size_bytes,
                "passed": sample_passed,
                "elapsed_ms": elapsed_ms,
                "error": error,
                "provider": result.provider if result else None,
                "confidence": result.confidence if result else None,
                "raw_text": result.raw_text if result else "",
                "recognized": recognized,
                "fields": field_results,
            }
        )
        if interval_seconds and index + 1 < len(dataset.samples):
            time.sleep(interval_seconds)

    image_pass_rate = _rate(passed_samples, len(sample_reports))
    field_accuracy = _rate(matched_fields, total_fields)
    passed = (
        image_pass_rate >= minimum_image_pass_rate
        and field_accuracy >= minimum_field_accuracy
    )
    return {
        "report_version": 1,
        "generated_at": _utc_now(),
        "dataset": dataset.name,
        "provider": (os.getenv("SHIPYARD_OCR_PROVIDER") or "baidu").casefold(),
        "gate": "PASS" if passed else "FAIL",
        "thresholds": {
            "minimum_image_pass_rate": minimum_image_pass_rate,
            "minimum_field_accuracy": minimum_field_accuracy,
        },
        "summary": {
            "sample_count": len(sample_reports),
            "passed_sample_count": passed_samples,
            "image_pass_rate": image_pass_rate,
            "matched_field_count": matched_fields,
            "field_count": total_fields,
            "field_accuracy": field_accuracy,
            "provider_error_count": sum(1 for item in sample_reports if item["error"]),
            "latency_ms_p50": round(statistics.median(durations), 3)
            if durations
            else 0.0,
            "latency_ms_p95": _percentile(durations, 0.95),
        },
        "samples": sample_reports,
        "note": (
            "本报告只验证 OCR 字段提取。不得替代小程序真机、现场业务 UAT、"
            "库存事务验收或生产部署审批。"
        ),
    }


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# 船厂吊牌 OCR 验收报告",
        "",
        f"- 生成时间：{report['generated_at']}",
        f"- 数据集：{report['dataset']}",
        f"- OCR 服务：{report['provider']}",
        f"- 结论：**{report['gate']}**",
        "",
        "## 汇总",
        "",
        "| 指标 | 结果 |",
        "| --- | ---: |",
        f"| 样图通过 | {summary['passed_sample_count']} / {summary['sample_count']} |",
        f"| 整图中标率 | {summary['image_pass_rate']:.2%} |",
        (
            f"| 字段准确率 | {summary['field_accuracy']:.2%} "
            f"({summary['matched_field_count']} / {summary['field_count']}) |"
        ),
        f"| 服务失败 | {summary['provider_error_count']} |",
        (
            f"| 耗时 P50 / P95 | {summary['latency_ms_p50']} / "
            f"{summary['latency_ms_p95']} ms |"
        ),
        "",
        "## 逐图结果",
        "",
        "| 样图 | 结果 | 耗时 | 失败原因 |",
        "| --- | --- | ---: | --- |",
    ]
    for sample in report["samples"]:
        reason = ""
        if sample["error"]:
            reason = f"{sample['error']['code']}: {sample['error']['message']}"
        elif not sample["passed"]:
            failed = [item["field"] for item in sample["fields"] if not item["passed"]]
            reason = "字段未命中: " + ", ".join(failed)
        lines.append(
            f"| {_markdown_cell(sample['file'])} "
            f"| {'通过' if sample['passed'] else '未通过'} "
            f"| {sample['elapsed_ms']} ms | "
            f"{_markdown_cell(reason or '—')} |"
        )

    mismatches = [
        (sample, item)
        for sample in report["samples"]
        for item in sample["fields"]
        if not item["passed"]
    ]
    if mismatches:
        lines.extend(
            [
                "",
                "## 字段差异",
                "",
                "| 样图 | 字段 | 期望 | 实际 |",
                "| --- | --- | --- | --- |",
            ]
        )
        for sample, item in mismatches:
            actual = item["actual"] if item["actual"] is not None else "（未识别）"
            lines.append(
                f"| {_markdown_cell(sample['file'])} "
                f"| {_markdown_cell(item['field'])} "
                f"| {_markdown_cell(item['expected'])} "
                f"| {_markdown_cell(actual)} |"
            )
    lines.extend(
        [
            "",
            "## 边界",
            "",
            report["note"],
            "",
        ]
    )
    return "\n".join(lines)


def _unique_report_path(directory: Path, stem: str, suffix: str) -> Path:
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


def write_reports(
    report: dict[str, Any],
    output_dir: Path,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    stem = f"shipyard-ocr-report-{stamp}"
    json_path = _unique_report_path(output_dir, stem, ".json")
    md_path = _unique_report_path(output_dir, stem, ".md")
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用真实脱敏吊牌照片验收船厂 OCR 字段识别",
    )
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument(
        "--expectations",
        type=Path,
        help="默认使用 <input-dir>/expectations.json",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="只读取 OCR 相关键；已有进程环境变量优先",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="报告目录；默认写回样图目录，建议使用仓库外目录",
    )
    parser.add_argument("--dry-run", action="store_true", help="只校验样本和清单")
    parser.add_argument("--interval-seconds", type=float, default=0.2)
    parser.add_argument(
        "--minimum-image-pass-rate",
        type=float,
        default=1.0,
    )
    parser.add_argument(
        "--minimum-field-accuracy",
        type=float,
        default=1.0,
    )
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    expectations = args.expectations or args.input_dir / "expectations.json"
    if not 0 <= args.minimum_image_pass_rate <= 1:
        print(
            "错误：--minimum-image-pass-rate 必须在 0 到 1 之间",
            file=sys.stderr,
        )
        return 2
    if not 0 <= args.minimum_field_accuracy <= 1:
        print("错误：--minimum-field-accuracy 必须在 0 到 1 之间", file=sys.stderr)
        return 2
    if args.interval_seconds < 0:
        print("错误：--interval-seconds 不能小于 0", file=sys.stderr)
        return 2
    try:
        dataset = load_dataset(args.input_dir, expectations)
        if args.dry_run:
            print(
                f"样图清单有效：{len(dataset.samples)} 张；"
                f"数据集={dataset.name}；未调用 OCR"
            )
            return 0
        if args.env_file:
            load_env_file(args.env_file)
        require_ocr_configuration()
    except AcceptanceConfigError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    report = evaluate_dataset(
        dataset,
        interval_seconds=args.interval_seconds,
        minimum_image_pass_rate=args.minimum_image_pass_rate,
        minimum_field_accuracy=args.minimum_field_accuracy,
    )
    output_dir = args.output_dir or args.input_dir
    json_path, md_path = write_reports(report, output_dir)
    summary = report["summary"]
    print(
        f"OCR 验收 {report['gate']}："
        f"{summary['passed_sample_count']}/{summary['sample_count']} 张通过，"
        f"字段准确率 {summary['field_accuracy']:.2%}"
    )
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")
    return 0 if report["gate"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
