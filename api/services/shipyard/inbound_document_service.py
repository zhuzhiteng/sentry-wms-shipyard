"""Inbound arrival-slip evidence and copy-return tracking."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from constants import ACTION_SHIPYARD_ARRIVAL_SLIP_RECORDED
from sqlalchemy import text
from utils.uploads import MAX_UPLOAD_BYTES

from services.audit_service import write_audit_log
from services.shipyard.errors import (
    ArrivalSlipValidationError,
    ManifestNotFoundError,
    OcrProviderError,
    OcrUnavailableError,
    PhotoNotFoundError,
)
from services.shipyard.label_ocr import recognize_label_image

ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}

_ARRIVAL_FIELD_LABELS = {
    "slip_no": "验收单号",
    "delivery_note_no": "送货单号",
    "supplier_name": "供应商",
    "engineering_no": "工程号",
    "purchase_order_no": "采购单号",
}
_ARRIVAL_FIELD_ALIASES = {
    "slip_no": ("到货验收单号", "验收单号", "验收编号"),
    "delivery_note_no": ("送货单编号", "送货单号", "送货编号"),
    "supplier_name": ("供应商名称", "供货单位", "供应商"),
    "engineering_no": ("工程编号", "工程号", "船号"),
    "purchase_order_no": (
        "采购订单号",
        "采购单号",
        "PO NO",
        "PO号",
    ),
}
_REQUIRED_ARRIVAL_FIELDS = ("slip_no", "delivery_note_no", "supplier_name")
_INSTRUCTION_MARKERS = ("如送", "注：", "告知", "保管员", "必须", "填写", "□")
_SLIP_NO_PATTERN = re.compile(
    r"\b(?:No\.?\s*S?|NOS)\s*[:：]?\s*([A-Z0-9-]{5,})\b",
    re.IGNORECASE,
)
_PURCHASE_ORDER_PATTERN = re.compile(r"\bSWS\d{10,}\b", re.IGNORECASE)
_ENGINEERING_NO_PATTERN = re.compile(r"\bH(?:B)?\d{4}\b", re.IGNORECASE)
_ARRIVAL_CODE_PATTERN = re.compile(r"[A-Z0-9][A-Z0-9-]{2,63}", re.IGNORECASE)
_DELIVERY_NOTE_PATTERN = re.compile(r"^\d{5,12}$")
_COMPANY_NAME_PATTERN = re.compile(
    r"[\u4e00-\u9fffA-Za-z0-9（）()·]{2,80}(?:有限责任公司|有限公司|公司|厂)"
)
_DELIVERY_NOTE_LABEL_PATTERN = re.compile(
    r"送货单(?:编)?号\s*[:：=]?\s*(\d{5,12})",
    re.IGNORECASE,
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _manifest(db, manifest_id: str):
    row = db.execute(
        text(
            """
            SELECT manifest_id, warehouse_id, manifest_no
              FROM shipyard_inbound_manifests
             WHERE manifest_id = :manifest_id
            """
        ),
        {"manifest_id": manifest_id},
    ).fetchone()
    if row is None:
        raise ManifestNotFoundError("入库清单不存在")
    return row


def _clean_ocr_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip(" ：:=|;,，；")


def _has_arrival_label(value: str) -> bool:
    compact = re.sub(r"\s+", "", value or "").casefold()
    return any(
        re.sub(r"\s+", "", alias).casefold() in compact
        for aliases in _ARRIVAL_FIELD_ALIASES.values()
        for alias in aliases
    )


def _truncate_at_next_label(value: str) -> str:
    positions = []
    for aliases in _ARRIVAL_FIELD_ALIASES.values():
        for alias in aliases:
            match = re.search(re.escape(alias), value, re.IGNORECASE)
            if match is not None:
                positions.append(match.start())
    return value[: min(positions)] if positions else value


def _is_instruction_line(value: str) -> bool:
    compact = re.sub(r"\s+", "", value or "")
    return any(marker in compact for marker in _INSTRUCTION_MARKERS)


def _find_labeled_value(lines: list[str], aliases: tuple[str, ...]) -> str:
    for index, line in enumerate(lines):
        if _is_instruction_line(line):
            continue
        for alias in aliases:
            match = re.search(
                rf"^\s*{re.escape(alias)}\s*[:：=]?\s*(.*)$",
                line,
                re.IGNORECASE,
            )
            if match is None:
                continue
            value = _clean_ocr_text(
                _truncate_at_next_label(match.group(1)),
            )
            if value:
                return value
            if index + 1 < len(lines):
                candidate = _clean_ocr_text(lines[index + 1])
                if candidate and not _has_arrival_label(candidate):
                    return candidate
    return ""


def _value_after_label(
    lines: list[str],
    aliases: tuple[str, ...],
    pattern: re.Pattern[str],
) -> str:
    for index, line in enumerate(lines):
        if not any(
            re.search(re.escape(alias), line, re.IGNORECASE) for alias in aliases
        ):
            continue
        for candidate in lines[index + 1 : index + 5]:
            value = _clean_ocr_text(candidate)
            if not value or _has_arrival_label(value):
                continue
            if pattern.fullmatch(value):
                return value
    return ""


def _supplier_name_fallback(lines: list[str]) -> str:
    for line in lines:
        if _is_instruction_line(line) or "外高桥" in line:
            continue
        match = re.search(
            r"([\u4e00-\u9fffA-Za-z0-9（）()·]{2,80}"
            r"(?:有限责任公司|有限公司|公司|厂))",
            line,
        )
        if match:
            return _clean_ocr_text(match.group(1))
    return ""


def recognize_arrival_slip_fields(content: bytes) -> dict[str, Any]:
    """Return editable arrival-slip suggestions without persisting the photo."""
    try:
        result = recognize_label_image(content)
    except (OcrUnavailableError, OcrProviderError):
        result = None

    lines = list(result.raw_lines if result is not None else [])
    raw_text = result.raw_text if result is not None else ""
    search_text = raw_text or "\n".join(lines)
    fields = {
        field: _find_labeled_value(lines, aliases)
        for field, aliases in _ARRIVAL_FIELD_ALIASES.items()
    }
    if not _ARRIVAL_CODE_PATTERN.fullmatch(fields["slip_no"]):
        match = _SLIP_NO_PATTERN.search(search_text)
        fields["slip_no"] = match.group(1) if match else ""
    if not _DELIVERY_NOTE_PATTERN.fullmatch(fields["delivery_note_no"]):
        match = _DELIVERY_NOTE_LABEL_PATTERN.search(search_text)
        if match:
            fields["delivery_note_no"] = match.group(1)
        else:
            fields["delivery_note_no"] = _value_after_label(
                lines,
                _ARRIVAL_FIELD_ALIASES["delivery_note_no"],
                _DELIVERY_NOTE_PATTERN,
            )
    if not _COMPANY_NAME_PATTERN.fullmatch(fields["supplier_name"]):
        fields["supplier_name"] = _supplier_name_fallback(lines)
    if not _ENGINEERING_NO_PATTERN.fullmatch(fields["engineering_no"]):
        match = _ENGINEERING_NO_PATTERN.search(search_text)
        fields["engineering_no"] = match.group(0) if match else ""
    if not _PURCHASE_ORDER_PATTERN.fullmatch(fields["purchase_order_no"]):
        match = _PURCHASE_ORDER_PATTERN.search(search_text)
        fields["purchase_order_no"] = match.group(0) if match else ""
    missing_fields = [field for field in _REQUIRED_ARRIVAL_FIELDS if not fields[field]]
    return {
        "fields": fields,
        "missing_fields": missing_fields,
        "missing_labels": [_ARRIVAL_FIELD_LABELS[field] for field in missing_fields],
        "recognized_count": sum(bool(value) for value in fields.values()),
        "message": (
            "已自动识别，请核对验收单信息。"
            if result is not None and any(fields.values())
            else "未能完整识别，请补充验收单信息。"
        ),
    }


def list_arrival_slips(db, manifest_id: str) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            SELECT arrival_slip_id, slip_no, delivery_note_no,
                   supplier_name, engineering_no, purchase_order_no,
                   is_urgent, factory_copy_count, shipyard_copy_count,
                   warehouse_copy_retained, file_name, content_type,
                   file_size, file_sha256, received_by, received_at,
                   created_at
              FROM shipyard_arrival_slips
             WHERE manifest_id = :manifest_id
             ORDER BY received_at DESC, created_at DESC
            """
        ),
        {"manifest_id": manifest_id},
    ).fetchall()
    return [
        {
            "arrival_slip_id": str(row.arrival_slip_id),
            "slip_no": row.slip_no,
            "delivery_note_no": row.delivery_note_no,
            "supplier_name": row.supplier_name,
            "engineering_no": row.engineering_no,
            "purchase_order_no": row.purchase_order_no,
            "is_urgent": row.is_urgent,
            "factory_copy_count": row.factory_copy_count,
            "shipyard_copy_count": row.shipyard_copy_count,
            "warehouse_copy_retained": row.warehouse_copy_retained,
            "file_name": row.file_name,
            "content_type": row.content_type,
            "file_size": row.file_size,
            "file_sha256": row.file_sha256,
            "received_by": row.received_by,
            "received_at": _iso(row.received_at),
            "file_url": (
                f"/api/shipyard/inbound/arrival-slips/{row.arrival_slip_id}/file"
            ),
        }
        for row in rows
    ]


def record_arrival_slip(
    db,
    *,
    manifest_id: str,
    slip_no: str,
    delivery_note_no: str,
    supplier_name: str,
    engineering_no: str | None,
    purchase_order_no: str | None,
    is_urgent: bool,
    factory_copy_count: int,
    shipyard_copy_count: int,
    warehouse_copy_retained: bool,
    file_name: str,
    content_type: str,
    content: bytes,
    received_by: str,
) -> dict[str, Any]:
    manifest = _manifest(db, manifest_id)
    if not content:
        raise ArrivalSlipValidationError("到货验收单照片为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ArrivalSlipValidationError("到货验收单照片超过 10 MiB")
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ArrivalSlipValidationError("到货验收单照片必须是 JPEG、PNG 或 WebP")
    if not factory_copy_count or not shipyard_copy_count:
        raise ArrivalSlipValidationError("四联单的工厂联和船厂联数量必须登记")
    file_sha = hashlib.sha256(content).hexdigest()
    row = db.execute(
        text(
            """
            INSERT INTO shipyard_arrival_slips
                (warehouse_id, manifest_id, slip_no, delivery_note_no,
                 supplier_name, engineering_no, purchase_order_no,
                 is_urgent, factory_copy_count, shipyard_copy_count,
                 warehouse_copy_retained, file_name, content_type,
                 file_size, file_sha256, file_bytes, received_by)
            VALUES
                (:warehouse_id, :manifest_id, :slip_no, :delivery_note_no,
                 :supplier_name, :engineering_no, :purchase_order_no,
                 :is_urgent, :factory_copy_count, :shipyard_copy_count,
                 :warehouse_copy_retained, :file_name, :content_type,
                 :file_size, :file_sha256, :file_bytes, :received_by)
            ON CONFLICT (manifest_id, file_sha256) DO NOTHING
            RETURNING arrival_slip_id
            """
        ),
        {
            "warehouse_id": manifest.warehouse_id,
            "manifest_id": manifest_id,
            "slip_no": slip_no[:128],
            "delivery_note_no": delivery_note_no[:128],
            "supplier_name": supplier_name[:200],
            "engineering_no": (engineering_no[:64] if engineering_no else None),
            "purchase_order_no": (
                purchase_order_no[:128] if purchase_order_no else None
            ),
            "is_urgent": is_urgent,
            "factory_copy_count": factory_copy_count,
            "shipyard_copy_count": shipyard_copy_count,
            "warehouse_copy_retained": warehouse_copy_retained,
            "file_name": file_name[:255],
            "content_type": content_type,
            "file_size": len(content),
            "file_sha256": file_sha,
            "file_bytes": content,
            "received_by": received_by,
        },
    ).fetchone()
    if row is None:
        existing = db.execute(
            text(
                """
                SELECT arrival_slip_id
                  FROM shipyard_arrival_slips
                 WHERE manifest_id = :manifest_id
                   AND file_sha256 = :file_sha256
                """
            ),
            {"manifest_id": manifest_id, "file_sha256": file_sha},
        ).fetchone()
        arrival_slip_id = str(existing.arrival_slip_id)
    else:
        arrival_slip_id = str(row.arrival_slip_id)
        write_audit_log(
            db,
            action_type=ACTION_SHIPYARD_ARRIVAL_SLIP_RECORDED,
            entity_type="shipyard_arrival_slip",
            entity_id=0,
            user_id=received_by,
            warehouse_id=manifest.warehouse_id,
            details={
                "arrival_slip_id": arrival_slip_id,
                "manifest_id": manifest_id,
                "manifest_no": manifest.manifest_no,
                "slip_no": slip_no,
                "delivery_note_no": delivery_note_no,
                "factory_copy_count": factory_copy_count,
                "shipyard_copy_count": shipyard_copy_count,
                "warehouse_copy_retained": warehouse_copy_retained,
                "file_sha256": file_sha,
            },
        )
    db.commit()
    return {
        "arrival_slip_id": arrival_slip_id,
        "arrival_slips": list_arrival_slips(db, manifest_id),
    }


def update_arrival_slip(
    db,
    *,
    arrival_slip_id: str,
    slip_no: str,
    delivery_note_no: str,
    supplier_name: str,
    engineering_no: str | None,
    purchase_order_no: str | None,
    updated_by: str,
) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT s.arrival_slip_id, s.manifest_id, s.warehouse_id,
                   m.manifest_no, s.slip_no, s.delivery_note_no,
                   s.supplier_name, s.engineering_no, s.purchase_order_no
              FROM shipyard_arrival_slips s
              JOIN shipyard_inbound_manifests m
                ON m.manifest_id = s.manifest_id
             WHERE s.arrival_slip_id = :arrival_slip_id
             FOR UPDATE OF s
            """
        ),
        {"arrival_slip_id": arrival_slip_id},
    ).fetchone()
    if row is None:
        raise PhotoNotFoundError("到货验收单不存在")
    if not slip_no or not delivery_note_no or not supplier_name:
        raise ArrivalSlipValidationError("验收单号、送货单号和供应商为必填项")
    db.execute(
        text(
            """
            UPDATE shipyard_arrival_slips
               SET slip_no = :slip_no,
                   delivery_note_no = :delivery_note_no,
                   supplier_name = :supplier_name,
                   engineering_no = :engineering_no,
                   purchase_order_no = :purchase_order_no
             WHERE arrival_slip_id = :arrival_slip_id
            """
        ),
        {
            "arrival_slip_id": arrival_slip_id,
            "slip_no": slip_no[:128],
            "delivery_note_no": delivery_note_no[:128],
            "supplier_name": supplier_name[:200],
            "engineering_no": engineering_no[:64] if engineering_no else None,
            "purchase_order_no": (
                purchase_order_no[:128] if purchase_order_no else None
            ),
        },
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_ARRIVAL_SLIP_RECORDED,
        entity_type="shipyard_arrival_slip",
        entity_id=0,
        user_id=updated_by,
        warehouse_id=row.warehouse_id,
        details={
            "action": "UPDATED",
            "arrival_slip_id": arrival_slip_id,
            "manifest_id": str(row.manifest_id),
            "manifest_no": row.manifest_no,
            "before": {
                "slip_no": row.slip_no,
                "delivery_note_no": row.delivery_note_no,
                "supplier_name": row.supplier_name,
                "engineering_no": row.engineering_no,
                "purchase_order_no": row.purchase_order_no,
            },
            "after": {
                "slip_no": slip_no,
                "delivery_note_no": delivery_note_no,
                "supplier_name": supplier_name,
                "engineering_no": engineering_no,
                "purchase_order_no": purchase_order_no,
            },
        },
    )
    db.commit()
    manifest_id = str(row.manifest_id)
    return {
        "arrival_slip_id": arrival_slip_id,
        "arrival_slips": list_arrival_slips(db, manifest_id),
    }


def get_arrival_slip_warehouse_id(db, arrival_slip_id: str) -> int:
    warehouse_id = db.execute(
        text(
            """
            SELECT warehouse_id
              FROM shipyard_arrival_slips
             WHERE arrival_slip_id = :arrival_slip_id
            """
        ),
        {"arrival_slip_id": arrival_slip_id},
    ).scalar()
    if warehouse_id is None:
        raise PhotoNotFoundError("到货验收单不存在")
    return int(warehouse_id)


def get_arrival_slip_file(db, arrival_slip_id: str) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT warehouse_id, file_name, content_type, file_size,
                   file_sha256, file_bytes
              FROM shipyard_arrival_slips
             WHERE arrival_slip_id = :arrival_slip_id
            """
        ),
        {"arrival_slip_id": arrival_slip_id},
    ).fetchone()
    if row is None:
        raise PhotoNotFoundError("到货验收单原图不存在")
    return {
        "warehouse_id": row.warehouse_id,
        "file_name": row.file_name,
        "content_type": row.content_type,
        "file_size": row.file_size,
        "file_sha256": row.file_sha256,
        "file_bytes": bytes(row.file_bytes),
    }
