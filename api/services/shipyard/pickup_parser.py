"""Parser for shipyard daily-delivery and on-demand pickup workbooks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Any
from zipfile import BadZipFile
from zoneinfo import ZoneInfo

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.datetime import from_excel
from openpyxl.utils.exceptions import InvalidFileException

from services.shipyard.xlsx_security import XlsxSecurityError, validate_xlsx_archive

SOURCE_TIMEZONE = ZoneInfo("Asia/Shanghai")
MAX_SCAN_ROWS = 5000
MAX_TEMPLATE_COLUMNS = 64
HEADER_SEARCH_ROWS = 20
PARSER_VERSION = "shipyard-pickup-v2"

HEADER_ALIASES = {
    "request_date": ("提货时间", "日期", "提货日期", "需求日期"),
    "engineering_no": ("工程号", "船号", "项目号"),
    "section_no": ("分段号", "分段"),
    "pallet_no": ("托盘号", "托盘编号", "制作图号", "图号"),
    "pallet_name": ("托盘名称", "托盘表名称", "图名"),
    "delivery_status": ("到货情况", "到货状态"),
    "prewarehouse_status": ("前置仓状态",),
    "pickup_status": ("提货情况", "提货状态"),
    "supplier_name": ("供应商", "厂家"),
    "work_order_status_raw": ("工单状态",),
    "applicant_raw": ("申请人",),
    "department": ("部门", "申请部门"),
    "delivery_address": ("配送地址", "送货地址", "需求位置"),
    "contact_name": ("联系人",),
    "contact_phone": ("手机号", "联系电话", "电话"),
    "demand_window_raw": ("需求时间", "需求时段"),
    "resource_1": ("配送资源1",),
    "driver_1": ("配送员1",),
    "resource_2": ("配送资源2",),
    "driver_2": ("配送员2",),
    "resource_3": ("配送资源3",),
    "driver_3": ("配送员3",),
    "confirmer": ("确认人",),
    "completed_at_raw": ("完成时间",),
    "confirmation_status": ("确认状态",),
    "material_code": ("物资编码", "物料编码", "编码"),
    "material_name": ("物资名称", "品名", "物料名称"),
    "item_spec": ("物资规格", "型号规格", "规格", "品名规格"),
    "quantity": ("数量", "需求数量", "提货数量", "供货量"),
    "unit": ("单位",),
    "expected_arrival_date": ("预计到货日期", "预计到货时间", "到货日期"),
    "keeper_name": ("保管员",),
    "keeper_phone": ("保管员电话", "保管员手机号"),
    "dispatch_no": ("派工单号", "提货单号", "派工号"),
    "remarks": ("备注", "说明", "平台"),
}


@dataclass(frozen=True)
class ParsedPickupLine:
    source_sheet: str
    source_row_no: str
    source_row_hash: str
    source_kind: str
    source_availability_status: str
    request_date: date | None
    engineering_no: str
    section_no: str | None
    pallet_no: str
    pallet_name: str
    delivery_status: str | None
    prewarehouse_status: str | None
    pickup_status: str | None
    supplier_name: str | None
    work_order_status_raw: str | None
    applicant_raw: str | None
    department: str | None
    delivery_address: str | None
    contact_name: str | None
    contact_phone: str | None
    demand_window_raw: str | None
    demand_start_at: datetime | None
    demand_end_at: datetime | None
    material_code: str | None
    material_name: str | None
    item_spec: str | None
    demand_quantity: Decimal | None
    demand_unit: str
    expected_arrival_date: date | None
    keeper_name: str | None
    keeper_phone: str | None
    dispatch_no: str | None
    remarks: str | None
    resource_1: str | None
    driver_1: str | None
    resource_2: str | None
    driver_2: str | None
    resource_3: str | None
    driver_3: str | None
    confirmer: str | None
    completed_at_raw: str | None
    completed_at: datetime | None
    confirmation_status: str | None
    raw_data: dict[str, Any]


@dataclass(frozen=True)
class ParsedPickupWorkbook:
    metadata: dict[str, Any]
    warnings: tuple[dict[str, Any], ...]
    lines: tuple[ParsedPickupLine, ...]


class PickupParseError(ValueError):
    def __init__(
        self,
        message: str,
        diagnostics: list[dict[str, Any]] | None = None,
    ):
        super().__init__(message)
        self.diagnostics = diagnostics or []


def _normalize_header(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip().upper()
    return re.sub(r"[\s\u3000:：()（）/／]+", "", text)


_NORMALIZED_ALIASES = {
    canonical: frozenset(_normalize_header(alias) for alias in aliases)
    for canonical, aliases in HEADER_ALIASES.items()
}


def _to_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        text = value.isoformat()
    elif isinstance(value, float) and value.is_integer():
        text = str(int(value))
    else:
        text = str(value)
    text = text.strip()
    return text or None


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _parse_decimal(value: Any) -> Decimal | None:
    text = _to_text(value)
    if not text:
        return None
    normalized = (
        text.replace(",", "")
        .replace("，", "")
        .replace("件", "")
        .replace("托", "")
        .strip()
    )
    try:
        number = Decimal(normalized)
    except (InvalidOperation, ValueError):
        return None
    return number if number >= 0 else None


def _parse_date(value: Any, *, workbook_epoch: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            parsed = from_excel(value, workbook_epoch)
        except (TypeError, ValueError, OverflowError):
            return None
        return parsed.date() if isinstance(parsed, datetime) else parsed
    text = _to_text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=SOURCE_TIMEZONE) if value.tzinfo is None else value
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=SOURCE_TIMEZONE)
    text = _to_text(value)
    if not text:
        return None
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y/%m/%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.replace(tzinfo=SOURCE_TIMEZONE)
        except ValueError:
            continue
    return None


def _parse_window(value: Any) -> tuple[datetime | None, datetime | None]:
    if isinstance(value, datetime):
        return _parse_datetime(value), None
    text = _to_text(value)
    if not text:
        return None, None
    parts = re.split(r"\s*[~～至]\s*", text, maxsplit=1)
    if len(parts) != 2:
        return None, None
    return _parse_datetime(parts[0]), _parse_datetime(parts[1])


def _row_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_sheet_rows(worksheet) -> list[tuple[int, tuple[Any, ...]]]:
    max_row = min(worksheet.max_row or 0, MAX_SCAN_ROWS)
    if max_row <= 0:
        return []
    max_column = min(worksheet.max_column or 1, MAX_TEMPLATE_COLUMNS)
    return [
        (excel_row, tuple(values))
        for excel_row, values in enumerate(
            worksheet.iter_rows(
                min_row=1,
                max_row=max_row,
                max_col=max_column,
                values_only=True,
            ),
            start=1,
        )
    ]


def _find_header(
    rows: list[tuple[int, tuple[Any, ...]]],
) -> tuple[int, int, dict[str, int], dict[str, str]] | None:
    best = None
    best_score = -1
    for row_offset, (excel_row, values) in enumerate(rows[:HEADER_SEARCH_ROWS]):
        mapping: dict[str, int] = {}
        labels: dict[str, str] = {}
        for column_index, value in enumerate(values[:MAX_TEMPLATE_COLUMNS]):
            normalized = _normalize_header(value)
            if not normalized:
                continue
            for canonical, aliases in _NORMALIZED_ALIASES.items():
                if canonical not in mapping and normalized in aliases:
                    mapping[canonical] = column_index
                    labels[canonical] = str(value).strip()
                    break
        if not {"engineering_no", "pallet_no"}.issubset(mapping):
            continue
        score = len(mapping)
        if score > best_score:
            best = (row_offset, excel_row, mapping, labels)
            best_score = score
    return best


def _source_kind(sheet_name: str) -> str:
    return "ON_DEMAND" if "按需" in sheet_name else "DAILY_DEMAND"


def _source_availability(
    delivery_status: str | None,
    prewarehouse_status: str | None,
) -> str:
    delivery = re.sub(
        r"[\s,，。;；:：/／]+",
        "",
        (delivery_status or "").casefold(),
    )
    prewarehouse = re.sub(
        r"[\s,，。;；:：/／]+",
        "",
        (prewarehouse_status or "").casefold(),
    )
    if any(
        token in delivery
        for token in ("前置仓无货", "前置仓未到", "前置仓没有", "前置仓缺货")
    ):
        return "UNAVAILABLE"
    if any(token in delivery for token in ("前置仓有货", "前置仓", "可提", "有货")):
        return "AVAILABLE"
    if any(
        token in delivery or token in prewarehouse
        for token in ("未到", "无货", "缺货", "未查到", "库存无", "暂无")
    ):
        return "UNAVAILABLE"
    return "UNKNOWN"


def _business_rows(
    *,
    rows: list[tuple[int, tuple[Any, ...]]],
    header_offset: int,
    mapping: dict[str, int],
    labels: dict[str, str],
    sheet_name: str,
    workbook_epoch: Any,
) -> tuple[list[ParsedPickupLine], list[dict[str, Any]]]:
    lines: list[ParsedPickupLine] = []
    warnings: list[dict[str, Any]] = []
    for excel_row, values in rows[header_offset + 1 :]:
        if not any(_to_text(value) for value in values):
            continue
        fields = {
            field: _to_text(values[index]) if index < len(values) else None
            for field, index in mapping.items()
        }
        engineering_no = fields.get("engineering_no")
        pallet_no = fields.get("pallet_no")
        if not engineering_no or not pallet_no:
            if "合计" in " ".join(str(value) for value in values if value is not None):
                continue
            warnings.append(
                {
                    "code": "ROW_SKIPPED_MISSING_KEY",
                    "sheet": sheet_name,
                    "excel_row": excel_row,
                    "message": "工程号或托盘号为空，未导入该行",
                    "engineering_no": engineering_no,
                    "pallet_no": pallet_no,
                }
            )
            continue

        demand_start_at, demand_end_at = _parse_window(fields.get("demand_window_raw"))
        if fields.get("demand_window_raw") and not (demand_start_at and demand_end_at):
            warnings.append(
                {
                    "code": "UNPARSEABLE_DEMAND_WINDOW",
                    "sheet": sheet_name,
                    "excel_row": excel_row,
                    "value": fields.get("demand_window_raw"),
                    "message": "需求时间未按起止时间解析，原文已保留",
                }
            )
        request_date = _parse_date(
            values[mapping["request_date"]]
            if "request_date" in mapping and mapping["request_date"] < len(values)
            else None,
            workbook_epoch=workbook_epoch,
        )
        if demand_start_at is not None:
            request_date = demand_start_at.date()

        raw_data: dict[str, Any] = {}
        for column_index, value in enumerate(values):
            header = _to_text(rows[header_offset][1][column_index])
            key_name = header or f"未命名列{get_column_letter(column_index + 1)}"
            raw_data[key_name] = _json_value(value)
        raw_data["excel_row"] = excel_row
        raw_data["sheet_name"] = sheet_name
        raw_data["form_template"] = "shipyard_pickup_demand"
        raw_data["source_kind"] = _source_kind(sheet_name)
        source_availability_status = _source_availability(
            fields.get("delivery_status"),
            fields.get("prewarehouse_status"),
        )
        raw_data["source_availability_status"] = source_availability_status

        quantity = _parse_decimal(fields.get("quantity"))
        payload = {
            "source_sheet": sheet_name,
            "source_row_no": str(excel_row),
            "engineering_no": engineering_no,
            "section_no": fields.get("section_no"),
            "pallet_no": pallet_no,
            "pallet_name": fields.get("pallet_name"),
            "quantity": str(quantity) if quantity is not None else None,
            "raw_data": raw_data,
        }
        lines.append(
            ParsedPickupLine(
                source_sheet=sheet_name,
                source_row_no=str(excel_row),
                source_row_hash=_row_hash(payload),
                source_kind=_source_kind(sheet_name),
                source_availability_status=source_availability_status,
                request_date=request_date,
                engineering_no=engineering_no,
                section_no=fields.get("section_no"),
                pallet_no=pallet_no,
                pallet_name=fields.get("pallet_name") or pallet_no,
                delivery_status=fields.get("delivery_status"),
                prewarehouse_status=fields.get("prewarehouse_status"),
                pickup_status=fields.get("pickup_status"),
                supplier_name=fields.get("supplier_name"),
                work_order_status_raw=fields.get("work_order_status_raw"),
                applicant_raw=fields.get("applicant_raw"),
                department=fields.get("department"),
                delivery_address=fields.get("delivery_address"),
                contact_name=fields.get("contact_name"),
                contact_phone=fields.get("contact_phone"),
                demand_window_raw=fields.get("demand_window_raw"),
                demand_start_at=demand_start_at,
                demand_end_at=demand_end_at,
                material_code=fields.get("material_code"),
                material_name=fields.get("material_name"),
                item_spec=fields.get("item_spec"),
                demand_quantity=quantity,
                demand_unit=fields.get("unit") or "件",
                expected_arrival_date=_parse_date(
                    values[mapping["expected_arrival_date"]]
                    if "expected_arrival_date" in mapping
                    and mapping["expected_arrival_date"] < len(values)
                    else None,
                    workbook_epoch=workbook_epoch,
                ),
                keeper_name=fields.get("keeper_name"),
                keeper_phone=fields.get("keeper_phone"),
                dispatch_no=fields.get("dispatch_no"),
                remarks=fields.get("remarks"),
                resource_1=fields.get("resource_1"),
                driver_1=fields.get("driver_1"),
                resource_2=fields.get("resource_2"),
                driver_2=fields.get("driver_2"),
                resource_3=fields.get("resource_3"),
                driver_3=fields.get("driver_3"),
                confirmer=fields.get("confirmer"),
                completed_at_raw=fields.get("completed_at_raw"),
                completed_at=_parse_datetime(fields.get("completed_at_raw")),
                confirmation_status=fields.get("confirmation_status"),
                raw_data=raw_data,
            )
        )
    return lines, warnings


def parse_pickup_workbook(
    content: bytes,
    *,
    source_file_name: str | None = None,
) -> ParsedPickupWorkbook:
    if not content:
        raise PickupParseError("上传文件为空")
    try:
        validate_xlsx_archive(content)
    except XlsxSecurityError as exc:
        raise PickupParseError(str(exc), [{"code": exc.code}]) from exc
    try:
        workbook = load_workbook(
            BytesIO(content),
            read_only=True,
            data_only=True,
            keep_links=False,
        )
    except (BadZipFile, InvalidFileException, OSError, ValueError) as exc:
        raise PickupParseError(
            "无法读取 Excel 文件，请确认文件未损坏且为 .xlsx 格式"
        ) from exc

    lines: list[ParsedPickupLine] = []
    warnings: list[dict[str, Any]] = []
    sheet_metadata: list[dict[str, Any]] = []
    ignored_sheets: list[str] = []
    field_mappings: dict[str, dict[str, str]] = {}
    try:
        for worksheet in workbook.worksheets:
            rows = _read_sheet_rows(worksheet)
            candidate = _find_header(rows)
            if candidate is None:
                ignored_sheets.append(worksheet.title)
                continue
            header_offset, header_row, mapping, labels = candidate
            parsed_lines, parsed_warnings = _business_rows(
                rows=rows,
                header_offset=header_offset,
                mapping=mapping,
                labels=labels,
                sheet_name=worksheet.title,
                workbook_epoch=workbook.epoch,
            )
            if not parsed_lines:
                ignored_sheets.append(worksheet.title)
                continue
            lines.extend(parsed_lines)
            warnings.extend(parsed_warnings)
            field_mappings[worksheet.title] = labels
            sheet_metadata.append(
                {
                    "sheet_name": worksheet.title,
                    "header_row": header_row,
                    "source_kind": _source_kind(worksheet.title),
                    "line_count": len(parsed_lines),
                }
            )
    finally:
        workbook.close()

    if not lines:
        raise PickupParseError(
            "未找到日配送需求或按需提货表头",
            [
                {
                    "code": "HEADER_NOT_FOUND",
                    "message": "至少需要工程号/船号和托盘号/制作图号两列",
                }
            ],
        )

    counts = {
        source_kind: sum(line.source_kind == source_kind for line in lines)
        for source_kind in ("DAILY_DEMAND", "ON_DEMAND")
    }
    metadata = {
        "parser_version": PARSER_VERSION,
        "source_file_name": source_file_name,
        "sheets": sheet_metadata,
        "ignored_sheets": ignored_sheets,
        "field_mapping": field_mappings,
        "line_count": len(lines),
        "daily_demand_count": counts["DAILY_DEMAND"],
        "on_demand_count": counts["ON_DEMAND"],
        "source_available_count": sum(
            line.source_availability_status == "AVAILABLE" for line in lines
        ),
        "source_unavailable_count": sum(
            line.source_availability_status == "UNAVAILABLE" for line in lines
        ),
        "source_availability_unknown_count": sum(
            line.source_availability_status == "UNKNOWN" for line in lines
        ),
        "engineering_count": len({line.engineering_no for line in lines}),
        "pallet_count": len({line.pallet_no for line in lines}),
        "warning_count": len(warnings),
        "source_timezone": "Asia/Shanghai",
    }
    return ParsedPickupWorkbook(
        metadata=metadata,
        warnings=tuple(warnings),
        lines=tuple(lines),
    )
