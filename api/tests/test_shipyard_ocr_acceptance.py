import json
from pathlib import Path

import pytest

from scripts.shipyard_ocr_acceptance import (
    AcceptanceConfigError,
    evaluate_dataset,
    load_dataset,
    load_env_file,
    main,
    normalize_field_value,
    render_markdown,
)
from services.shipyard.label_ocr import LabelOcrResult


def _result(**fields):
    return LabelOcrResult(
        provider="baidu",
        raw_text="\n".join(f"{key}: {value}" for key, value in fields.items()),
        raw_lines=[],
        fields=fields,
        confidence=0.97,
    )


def _dataset(tmp_path: Path):
    (tmp_path / "label-01.jpg").write_bytes(b"test-image")
    (tmp_path / "expectations.json").write_text(
        json.dumps(
            {
                "dataset": "脱敏吊牌回归集",
                "samples": [
                    {
                        "file": "label-01.jpg",
                        "expected": {
                            "material_code": "MAT 001",
                            "item_name": "船用支架",
                            "item_spec": "S-100",
                            "tag_suffix": "1-1",
                            "label_quantity": "4.000",
                        },
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return load_dataset(tmp_path, tmp_path / "expectations.json")


def test_acceptance_report_counts_normalized_field_hits(tmp_path):
    dataset = _dataset(tmp_path)

    report = evaluate_dataset(
        dataset,
        recognizer=lambda _content: _result(
            material_code="mat001",
            item_name="船用支架",
            item_spec="S-100",
            tag_suffix="1－1",
            label_quantity="4",
        ),
    )

    assert report["gate"] == "PASS"
    assert report["summary"]["image_pass_rate"] == 1.0
    assert report["summary"]["field_accuracy"] == 1.0
    assert report["samples"][0]["passed"] is True


def test_acceptance_report_exposes_spec_mismatch(tmp_path):
    dataset = _dataset(tmp_path)

    report = evaluate_dataset(
        dataset,
        recognizer=lambda _content: _result(
            material_code="MAT001",
            item_name="船用支架",
            item_spec="S-999",
            tag_suffix="1-1",
            label_quantity="4",
        ),
    )

    assert report["gate"] == "FAIL"
    assert report["summary"]["passed_sample_count"] == 0
    mismatch = next(
        item for item in report["samples"][0]["fields"] if item["field"] == "item_spec"
    )
    assert mismatch == {
        "field": "item_spec",
        "expected": "S-100",
        "actual": "S-999",
        "passed": False,
    }
    markdown = render_markdown(report)
    assert "S-100" in markdown
    assert "S-999" in markdown


def test_acceptance_rejects_paths_outside_input_directory(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside-label.jpg"
    outside.write_bytes(b"private")
    input_dir = tmp_path / "samples"
    input_dir.mkdir()
    expectations = input_dir / "expectations.json"
    expectations.write_text(
        json.dumps(
            {
                "samples": [
                    {
                        "file": "../outside-label.jpg",
                        "expected": {"material_code": "MAT-001"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(AcceptanceConfigError, match="越出输入目录"):
        load_dataset(input_dir, expectations)


def test_env_file_loads_only_ocr_keys_without_overriding_process_values(
    tmp_path,
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "BAIDU_OCR_API_KEY=file-key\n"
        "BAIDU_OCR_SECRET_KEY=file-secret\n"
        "SHIPYARD_OCR_PROVIDER=baidu\n"
        "JWT_SECRET=must-not-load",
        encoding="utf-8",
    )
    environ = {"BAIDU_OCR_API_KEY": "process-key"}

    load_env_file(env_file, environ=environ)

    assert environ["BAIDU_OCR_API_KEY"] == "process-key"
    assert environ["BAIDU_OCR_SECRET_KEY"] == "file-secret"
    assert "JWT_SECRET" not in environ


def test_quantity_normalization_ignores_decimal_display_zeros():
    assert normalize_field_value("label_quantity", "4") == normalize_field_value(
        "label_quantity",
        "4.000",
    )


def test_cli_dry_run_validates_manifest_without_credentials(
    tmp_path,
    monkeypatch,
    capsys,
):
    _dataset(tmp_path)
    for key in ("BAIDU_OCR_API_KEY", "BAIDU_OCR_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)

    exit_code = main(["--input-dir", str(tmp_path), "--dry-run"])

    assert exit_code == 0
    assert "未调用 OCR" in capsys.readouterr().out


def test_cli_refuses_real_run_without_credentials(tmp_path, monkeypatch, capsys):
    _dataset(tmp_path)
    monkeypatch.delenv("BAIDU_OCR_API_KEY", raising=False)
    monkeypatch.delenv("BAIDU_OCR_SECRET_KEY", raising=False)

    exit_code = main(["--input-dir", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "缺少真实 OCR 凭据" in captured.err
