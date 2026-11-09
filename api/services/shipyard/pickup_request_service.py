"""Daily-demand and special-dispatch pickup requests with retained shortages."""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from psycopg2.extras import Json
from sqlalchemy import text

from constants import (
    ACTION_SHIPYARD_PICKUP_REQUEST_CONFIRMED,
    ACTION_SHIPYARD_PICKUP_REQUEST_IMPORTED,
    ACTION_SHIPYARD_PICKUP_REQUEST_LINE_MATCHED,
    ACTION_SHIPYARD_PICKUP_REQUEST_OUTBOUND_CREATED,
    ACTION_SHIPYARD_PICKUP_REQUEST_VOIDED,
    SHIPYARD_OUTBOUND_LINE_ALLOCATED,
    SHIPYARD_OUTBOUND_MANIFEST_CONFIRMED,
    SHIPYARD_OUTBOUND_MANIFEST_PARSED,
    SHIPYARD_OUTBOUND_ORDER_PENDING,
    SHIPYARD_OUTBOUND_TASK_PENDING,
    SHIPYARD_PICKUP_AMBIGUOUS,
    SHIPYARD_PICKUP_AVAILABLE,
    SHIPYARD_PICKUP_CONFIRMED,
    SHIPYARD_PICKUP_DUPLICATE_DEMAND,
    SHIPYARD_PICKUP_OUTBOUND,
    SHIPYARD_PICKUP_OUTBOUND_PENDING,
    SHIPYARD_PICKUP_PARSED,
    SHIPYARD_PICKUP_QUANTITY_MISMATCH,
    SHIPYARD_PICKUP_SOURCE_DECLARED_UNAVAILABLE,
    SHIPYARD_PICKUP_SOURCE_UNAVAILABLE,
    SHIPYARD_PICKUP_VOID,
    SHIPYARD_PICKUP_WAREHOUSE_MISSING,
    SHIPYARD_TICKET_ALLOCATED,
    SHIPYARD_TICKET_IN_STOCK,
    SHIPYARD_TICKET_PARTIAL,
)
from services.audit_service import write_audit_log
from services.shipyard.errors import (
    DuplicatePickupRequestFileError,
    PickupLineNotFoundError,
    PickupLineStateError,
    PickupRequestNotFoundError,
    PickupRequestStateError,
    WarehouseNotFoundError,
)
from services.shipyard.outbound_manifest_service import (
    get_outbound_order_payload,
)
from services.shipyard.pickup_parser import (
    ParsedPickupLine,
    parse_pickup_workbook,
)


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _decimal_text(value: Any) -> str | None:
    if value is None:
        return None
    return format(Decimal(value), "f")


def _normalized(value: Any) -> str:
    return str(value or "").strip().casefold()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _check_warehouse(db, warehouse_id: int) -> None:
    exists = db.execute(
        text(
            """
            SELECT 1
              FROM warehouses
             WHERE warehouse_id = :warehouse_id
               AND is_active = TRUE
            """
        ),
        {"warehouse_id": warehouse_id},
    ).scalar()
    if not exists:
        raise WarehouseNotFoundError("仓库不存在或已停用")


def _request_header(db, request_id: str, *, for_update: bool = False):
    lock_clause = "FOR UPDATE" if for_update else ""
    return db.execute(
        text(
            f"""
            SELECT request_id, warehouse_id, request_no, source_type,
                   request_date, dispatch_no, source_file_name,
                   source_file_sha256, source_template_version, status,
                   metadata, created_by, created_at, updated_at
              FROM shipyard_pickup_requests
             WHERE request_id = :request_id
             {lock_clause}
            """  # nosec B608
        ),
        {"request_id": request_id},
    ).fetchone()


def _request_lines(db, request_id: str, *, for_update: bool = False):
    lock_clause = "FOR UPDATE OF l" if for_update else ""
    return db.execute(
        text(
            f"""
            SELECT l.line_id, l.request_id, l.source_sheet, l.source_row_no,
                   l.source_row_hash, l.source_kind, l.request_date,
                   l.source_availability_status,
                   l.engineering_no, l.section_no, l.pallet_no,
                   l.pallet_name, l.delivery_status_raw,
                   l.prewarehouse_status_raw, l.pickup_status_raw,
                   l.supplier_name, l.work_order_status_raw,
                   l.applicant_raw, l.department, l.delivery_address,
                   l.contact_name, l.contact_phone, l.demand_window_raw,
                   l.demand_start_at, l.demand_end_at, l.material_code,
                   l.material_name, l.item_spec, l.demand_quantity,
                   l.demand_unit, l.expected_arrival_date, l.keeper_name,
                   l.keeper_phone, l.dispatch_no, l.remarks, l.resource_1,
                   l.driver_1, l.resource_2, l.driver_2, l.resource_3,
                   l.driver_3, l.confirmer, l.completed_at_raw,
                   l.completed_at, l.confirmation_status_raw, l.match_status,
                   l.available_quantity, l.ticket_id, l.outbound_order_id,
                   l.outbound_task_id, l.match_details, l.raw_data,
                   l.created_at, l.updated_at,
                   ot.status AS outbound_task_status
              FROM shipyard_pickup_request_lines l
              LEFT JOIN shipyard_outbound_tasks ot
                ON ot.task_id = l.outbound_task_id
             WHERE l.request_id = :request_id
             ORDER BY l.request_date NULLS LAST, l.source_sheet,
                      COALESCE((l.raw_data->>'excel_row')::int, 0),
                      l.source_row_no
             {lock_clause}
            """  # nosec B608
        ),
        {"request_id": request_id},
    ).fetchall()


def _effective_line_status(line) -> str:
    if line.outbound_task_status == SHIPYARD_PICKUP_OUTBOUND:
        return SHIPYARD_PICKUP_OUTBOUND
    return line.match_status


def _line_payload(row) -> dict[str, Any]:
    return {
        "line_id": str(row.line_id),
        "source_sheet": row.source_sheet,
        "source_row_no": row.source_row_no,
        "source_kind": row.source_kind,
        "source_availability_status": row.source_availability_status,
        "request_date": _iso(row.request_date),
        "engineering_no": row.engineering_no,
        "section_no": row.section_no,
        "pallet_no": row.pallet_no,
        "pallet_name": row.pallet_name,
        "delivery_status_raw": row.delivery_status_raw,
        "prewarehouse_status_raw": row.prewarehouse_status_raw,
        "pickup_status_raw": row.pickup_status_raw,
        "supplier_name": row.supplier_name,
        "work_order_status_raw": row.work_order_status_raw,
        "applicant_raw": row.applicant_raw,
        "department": row.department,
        "delivery_address": row.delivery_address,
        "contact_name": row.contact_name,
        "contact_phone": row.contact_phone,
        "demand_window_raw": row.demand_window_raw,
        "demand_start_at": _iso(row.demand_start_at),
        "demand_end_at": _iso(row.demand_end_at),
        "material_code": row.material_code,
        "material_name": row.material_name,
        "item_spec": row.item_spec,
        "demand_quantity": _decimal_text(row.demand_quantity),
        "demand_unit": row.demand_unit,
        "expected_arrival_date": _iso(row.expected_arrival_date),
        "keeper_name": row.keeper_name,
        "keeper_phone": row.keeper_phone,
        "dispatch_no": row.dispatch_no,
        "remarks": row.remarks,
        "resource_1": row.resource_1,
        "driver_1": row.driver_1,
        "resource_2": row.resource_2,
        "driver_2": row.driver_2,
        "resource_3": row.resource_3,
        "driver_3": row.driver_3,
        "confirmer": row.confirmer,
        "completed_at": _iso(row.completed_at),
        "confirmation_status_raw": row.confirmation_status_raw,
        "match_status": _effective_line_status(row),
        "available_quantity": _decimal_text(row.available_quantity),
        "ticket_id": str(row.ticket_id) if row.ticket_id else None,
        "outbound_order_id": (
            str(row.outbound_order_id) if row.outbound_order_id else None
        ),
        "outbound_task_id": (
            str(row.outbound_task_id) if row.outbound_task_id else None
        ),
        "outbound_task_status": row.outbound_task_status,
        "match_details": row.match_details,
        "raw_data": row.raw_data,
    }


def get_pickup_request_payload(db, *, request_id: str) -> dict[str, Any]:
    header = _request_header(db, request_id)
    if header is None:
        raise PickupRequestNotFoundError("配送/提货需求不存在")
    lines = _request_lines(db, request_id)
    line_payloads = [_line_payload(line) for line in lines]
    request_dates = sorted({line.request_date for line in lines if line.request_date})
    counts: dict[str, int] = {}
    for line in line_payloads:
        status = line["match_status"]
        counts[status] = counts.get(status, 0) + 1
    return {
        "request": {
            "request_id": str(header.request_id),
            "warehouse_id": header.warehouse_id,
            "request_no": header.request_no,
            "source_type": header.source_type,
            "request_date": _iso(header.request_date),
            "dispatch_no": header.dispatch_no,
            "source_file_name": header.source_file_name,
            "source_file_sha256": header.source_file_sha256,
            "source_template_version": header.source_template_version,
            "status": header.status,
            "metadata": header.metadata,
            "created_by": header.created_by,
            "created_at": _iso(header.created_at),
            "updated_at": _iso(header.updated_at),
            "file_url": (
                f"/api/shipyard/pickup-requests/{header.request_id}/file"
                if header.source_file_name
                else None
            ),
            "print_url": (f"/api/shipyard/pickup-requests/{header.request_id}/print"),
            "print_all_url": (
                f"/api/shipyard/pickup-requests/{header.request_id}/print"
                "?include_unavailable=1"
            ),
        },
        "lines": line_payloads,
        "summary": {
            "line_count": len(line_payloads),
            "available_count": counts.get(SHIPYARD_PICKUP_AVAILABLE, 0),
            "ambiguous_count": counts.get(SHIPYARD_PICKUP_AMBIGUOUS, 0),
            "warehouse_missing_count": counts.get(SHIPYARD_PICKUP_WAREHOUSE_MISSING, 0),
            "source_unavailable_count": counts.get(
                SHIPYARD_PICKUP_SOURCE_UNAVAILABLE, 0
            ),
            "source_available_count": sum(
                line.source_availability_status == "AVAILABLE" for line in lines
            ),
            "quantity_mismatch_count": counts.get(SHIPYARD_PICKUP_QUANTITY_MISMATCH, 0),
            "outbound_pending_count": counts.get(SHIPYARD_PICKUP_OUTBOUND_PENDING, 0),
            "outbound_count": counts.get(SHIPYARD_PICKUP_OUTBOUND, 0),
            "duplicate_demand_count": counts.get(SHIPYARD_PICKUP_DUPLICATE_DEMAND, 0),
            "status_counts": counts,
            "date_from": _iso(request_dates[0]) if request_dates else None,
            "date_to": _iso(request_dates[-1]) if request_dates else None,
            "demand_date_count": len(request_dates),
        },
    }


def list_pickup_requests(
    db,
    *,
    warehouse_id: int,
    limit: int = 50,
) -> dict[str, Any]:
    _check_warehouse(db, warehouse_id)
    rows = db.execute(
        text(
            """
            SELECT r.request_id, r.request_no, r.source_type, r.request_date,
                   r.dispatch_no, r.source_file_name, r.status,
                   r.created_by, r.created_at,
                   COUNT(l.line_id) AS line_count,
                   COUNT(l.line_id) FILTER (
                       WHERE l.match_status = 'AVAILABLE'
                   ) AS available_count,
                   COUNT(l.line_id) FILTER (
                       WHERE l.match_status = 'WAREHOUSE_MISSING'
                   ) AS warehouse_missing_count,
                   COUNT(l.line_id) FILTER (
                       WHERE l.match_status = 'AMBIGUOUS'
                   ) AS ambiguous_count,
                   COUNT(l.line_id) FILTER (
                       WHERE l.match_status = 'SOURCE_UNAVAILABLE'
                   ) AS source_unavailable_count,
                   COUNT(l.line_id) FILTER (
                       WHERE l.source_availability_status = 'AVAILABLE'
                   ) AS source_available_count
              FROM shipyard_pickup_requests r
              LEFT JOIN shipyard_pickup_request_lines l
                ON l.request_id = r.request_id
             WHERE r.warehouse_id = :warehouse_id
             GROUP BY r.request_id
             ORDER BY r.created_at DESC
             LIMIT :limit
            """
        ),
        {"warehouse_id": warehouse_id, "limit": max(1, min(limit, 200))},
    ).fetchall()
    return {
        "requests": [
            {
                "request_id": str(row.request_id),
                "request_no": row.request_no,
                "source_type": row.source_type,
                "request_date": _iso(row.request_date),
                "dispatch_no": row.dispatch_no,
                "source_file_name": row.source_file_name,
                "status": row.status,
                "line_count": row.line_count,
                "available_count": row.available_count,
                "warehouse_missing_count": row.warehouse_missing_count,
                "ambiguous_count": row.ambiguous_count,
                "source_unavailable_count": row.source_unavailable_count,
                "source_available_count": row.source_available_count,
                "created_by": row.created_by,
                "created_at": _iso(row.created_at),
            }
            for row in rows
        ]
    }


def _candidate_rows(db, *, warehouse_id: int, line):
    return db.execute(
        text(
            """
            SELECT t.ticket_id, t.ticket_no, t.declared_quantity,
                   t.received_quantity, t.on_hand_quantity,
                   t.expected_pallet_count, t.assigned_bin_id,
                   t.location_path_snapshot, t.stock_lot_number,
                   t.status, t.created_at,
                   l.ship_no,
                   COALESCE(
                       NULLIF(BTRIM(l.engineering_no), ''),
                       NULLIF(BTRIM(pb.engineering_no), ''),
                       BTRIM(l.ship_no)
                   ) AS engineering_no,
                   COALESCE(
                       NULLIF(BTRIM(l.production_drawing_no), ''),
                       NULLIF(BTRIM(pb.production_drawing_no), ''),
                       ''
                   ) AS production_drawing_no,
                   COALESCE(
                       NULLIF(BTRIM(l.manufacturer_name), ''),
                       NULLIF(BTRIM(pb.manufacturer_name), ''),
                       ''
                   ) AS manufacturer_name,
                   l.material_code, l.item_name, l.item_spec, l.unit,
                   b.bin_code, z.zone_code, z.zone_name
              FROM shipyard_cargo_tickets t
              JOIN shipyard_inbound_lines l
                ON l.line_id = t.inbound_line_id
              JOIN shipyard_inbound_manifests im
                ON im.manifest_id = l.manifest_id
              JOIN bins b ON b.bin_id = t.assigned_bin_id
              LEFT JOIN zones z ON z.zone_id = b.zone_id
              LEFT JOIN shipyard_project_bindings pb
                ON pb.binding_id = l.project_binding_id
             WHERE im.warehouse_id = :warehouse_id
               AND t.status IN (:in_stock, :partial)
               AND t.on_hand_quantity > 0
               AND (
                   LOWER(BTRIM(l.ship_no)) = :engineering_no
                   OR LOWER(BTRIM(COALESCE(
                       NULLIF(l.engineering_no, ''),
                       pb.engineering_no,
                       ''
                   ))) = :engineering_no
               )
               AND (
                   LOWER(BTRIM(COALESCE(
                       NULLIF(l.production_drawing_no, ''),
                       pb.production_drawing_no,
                       ''
                   ))) = :pallet_no
                   OR EXISTS (
                       SELECT 1
                         FROM shipyard_pallet_units p
                        WHERE p.ticket_id = t.ticket_id
                          AND (
                              LOWER(BTRIM(COALESCE(p.factory_pallet_no, '')))
                                  = :pallet_no
                              OR LOWER(BTRIM(COALESCE(
                                  p.installation_pallet_no, ''
                              ))) = :pallet_no
                          )
                   )
               )
               AND NOT EXISTS (
                   SELECT 1
                     FROM shipyard_outbound_tasks ot
                    WHERE ot.ticket_id = t.ticket_id
               )
             ORDER BY t.created_at, t.ticket_no
            """
        ),
        {
            "warehouse_id": warehouse_id,
            "in_stock": SHIPYARD_TICKET_IN_STOCK,
            "partial": SHIPYARD_TICKET_PARTIAL,
            "engineering_no": _normalized(line.engineering_no),
            "pallet_no": _normalized(line.pallet_no),
        },
    ).fetchall()


def _pallets(db, ticket_id: str):
    return db.execute(
        text(
            """
            SELECT pallet_id, tag_suffix, factory_pallet_no,
                   installation_pallet_no, batch_no, status
              FROM shipyard_pallet_units
             WHERE ticket_id = :ticket_id
             ORDER BY tag_suffix NULLS FIRST, pallet_id
            """
        ),
        {"ticket_id": ticket_id},
    ).fetchall()


def _candidate_payload(row, pallets) -> dict[str, Any]:
    suffixes = [pallet.tag_suffix for pallet in pallets]
    suffix_set = set(suffixes)
    blocking_reason = None
    if not pallets:
        blocking_reason = "库存票货没有已登记的托盘吊牌"
    elif any(not suffix for suffix in suffixes):
        blocking_reason = "库存票货存在无托盘后缀的吊牌"
    elif len(suffix_set) != len(suffixes):
        blocking_reason = "库存票货存在重复托盘后缀"
    elif any(pallet.status != "STORED" for pallet in pallets):
        blocking_reason = "库存票货存在非在库托盘"
    elif (
        row.expected_pallet_count is not None
        and len(pallets) != row.expected_pallet_count
    ):
        blocking_reason = "库存托盘数与入库票货预计托盘数不一致"
    return {
        "ticket_id": str(row.ticket_id),
        "ticket_no": row.ticket_no,
        "ship_no": row.ship_no,
        "engineering_no": row.engineering_no,
        "production_drawing_no": row.production_drawing_no,
        "manufacturer_name": row.manufacturer_name,
        "material_code": row.material_code,
        "item_name": row.item_name,
        "item_spec": row.item_spec,
        "unit": row.unit,
        "available_quantity": _decimal_text(row.on_hand_quantity),
        "declared_quantity": _decimal_text(row.declared_quantity),
        "received_quantity": _decimal_text(row.received_quantity),
        "location_path_snapshot": row.location_path_snapshot,
        "stock_lot_number": row.stock_lot_number,
        "expected_pallet_count": len(pallets),
        "tag_suffixes": suffixes,
        "eligible": blocking_reason is None,
        "blocking_reason": blocking_reason,
    }


def _narrow_candidates(line, candidates):
    if line.demand_quantity is None:
        return candidates
    exact = [
        candidate
        for candidate in candidates
        if Decimal(candidate["available_quantity"]) == Decimal(line.demand_quantity)
    ]
    if exact:
        return exact
    return candidates


def _material_filter(line, candidates):
    accepted = []
    conflicts = []
    missing_fields = set()
    expected_fields = (
        ("material_code", line.material_code),
        ("material_name", line.material_name),
        ("item_spec", line.item_spec),
    )
    for candidate in candidates:
        candidate_conflicts = []
        for field, expected in expected_fields:
            expected_text = _normalized(expected)
            if not expected_text:
                continue
            actual_text = _normalized(candidate.get(field))
            if not actual_text:
                missing_fields.add(field)
                continue
            if expected_text != actual_text:
                candidate_conflicts.append(
                    {
                        "field": field,
                        "expected": str(expected),
                        "actual": candidate.get(field),
                    }
                )
        if candidate_conflicts:
            conflicts.append(
                {
                    "ticket_id": candidate["ticket_id"],
                    "ticket_no": candidate["ticket_no"],
                    "conflicts": candidate_conflicts,
                }
            )
        else:
            accepted.append(candidate)
    return accepted, conflicts, sorted(missing_fields)


def _match_line(db, *, warehouse_id: int, line) -> dict[str, Any]:
    if line.source_availability_status == SHIPYARD_PICKUP_SOURCE_DECLARED_UNAVAILABLE:
        return {
            "match_status": SHIPYARD_PICKUP_SOURCE_UNAVAILABLE,
            "ticket_id": None,
            "available_quantity": Decimal(0),
            "details": {
                "candidate_count": 0,
                "eligible_count": 0,
                "candidates": [],
                "matching_policy": "SOURCE_DECLARED_UNAVAILABLE",
                "message": "配送需求已标注前置仓无货或未到，本批不安排出库",
                "delivery_status_raw": line.delivery_status_raw,
                "prewarehouse_status_raw": line.prewarehouse_status_raw,
            },
        }
    rows = _candidate_rows(db, warehouse_id=warehouse_id, line=line)
    candidates = []
    for row in rows:
        pallets = _pallets(db, str(row.ticket_id))
        candidates.append(_candidate_payload(row, pallets))
    eligible = [candidate for candidate in candidates if candidate["eligible"]]
    material_eligible, material_conflicts, missing_material_fields = _material_filter(
        line, eligible
    )
    narrowed = _narrow_candidates(line, material_eligible)

    details = {
        "candidate_count": len(candidates),
        "eligible_count": len(eligible),
        "candidates": candidates,
        "material_conflicts": material_conflicts,
        "material_fields_not_on_stock_record": missing_material_fields,
        "matching_policy": (
            "WHOLE_TICKET_EXACT_QUANTITY"
            if line.demand_quantity is not None
            else "WHOLE_TICKET_BY_PALLET"
        ),
    }
    if not material_eligible:
        return {
            "match_status": SHIPYARD_PICKUP_WAREHOUSE_MISSING,
            "ticket_id": None,
            "available_quantity": Decimal(0),
            "details": {
                **details,
                "message": (
                    "同托盘库存的物料编码、品名或规格与提货单不一致"
                    if material_conflicts
                    else None
                ),
            },
        }
    if len(narrowed) > 1:
        return {
            "match_status": SHIPYARD_PICKUP_AMBIGUOUS,
            "ticket_id": None,
            "available_quantity": Decimal(0),
            "details": {
                **details,
                "message": "匹配到多张库存票货，请人工选择",
            },
        }
    if len(narrowed) == 1:
        selected = narrowed[0]
        if line.demand_quantity is not None and Decimal(
            selected["available_quantity"]
        ) != Decimal(line.demand_quantity):
            return {
                "match_status": SHIPYARD_PICKUP_QUANTITY_MISMATCH,
                "ticket_id": selected["ticket_id"],
                "available_quantity": Decimal(selected["available_quantity"]),
                "details": {
                    **details,
                    "selected_ticket": selected,
                    "message": "整票数量与提货数量不一致，系统不拆票",
                },
            }
        return {
            "match_status": SHIPYARD_PICKUP_AVAILABLE,
            "ticket_id": selected["ticket_id"],
            "available_quantity": Decimal(selected["available_quantity"]),
            "details": {**details, "selected_ticket": selected},
        }

    if line.demand_quantity is not None and candidates:
        return {
            "match_status": SHIPYARD_PICKUP_QUANTITY_MISMATCH,
            "ticket_id": None,
            "available_quantity": Decimal(0),
            "details": {
                **details,
                "message": "没有数量一致的整票库存，系统不拆票",
            },
        }
    return {
        "match_status": SHIPYARD_PICKUP_WAREHOUSE_MISSING,
        "ticket_id": None,
        "available_quantity": Decimal(0),
        "details": details,
    }


def _set_line_match(db, *, line_id: str, match: dict[str, Any]) -> None:
    db.execute(
        text(
            """
            UPDATE shipyard_pickup_request_lines
               SET match_status = :match_status,
                   ticket_id = :ticket_id,
                   available_quantity = :available_quantity,
                   match_details = :match_details,
                   updated_at = NOW()
             WHERE line_id = :line_id
            """
        ),
        {
            "line_id": line_id,
            "match_status": match["match_status"],
            "ticket_id": match["ticket_id"],
            "available_quantity": match["available_quantity"],
            "match_details": Json(match["details"]),
        },
    )


def _refresh_request_matches(db, request_id: str) -> None:
    header = _request_header(db, request_id)
    if header is None:
        raise PickupRequestNotFoundError("配送/提货需求不存在")
    lines = _request_lines(db, request_id)
    for line in lines:
        if line.outbound_task_id or line.match_status in (
            SHIPYARD_PICKUP_OUTBOUND_PENDING,
            SHIPYARD_PICKUP_OUTBOUND,
        ):
            continue
        match = _match_line(db, warehouse_id=header.warehouse_id, line=line)
        _set_line_match(db, line_id=str(line.line_id), match=match)
    dates = [line.request_date for line in lines if line.request_date]
    if dates:
        db.execute(
            text(
                """
                UPDATE shipyard_pickup_requests
                   SET request_date = :request_date, updated_at = NOW()
                 WHERE request_id = :request_id
                """
            ),
            {"request_date": max(dates), "request_id": request_id},
        )


def _insert_parsed_line(
    db,
    *,
    request_id: str,
    line: ParsedPickupLine,
) -> str:
    row = db.execute(
        text(
            """
            INSERT INTO shipyard_pickup_request_lines
                (request_id, source_sheet, source_row_no, source_row_hash,
                 source_kind, source_availability_status, request_date,
                 engineering_no, section_no,
                 pallet_no, pallet_name, delivery_status_raw,
                 prewarehouse_status_raw, pickup_status_raw, supplier_name,
                 work_order_status_raw, applicant_raw, department,
                 delivery_address, contact_name, contact_phone,
                 demand_window_raw, demand_start_at, demand_end_at,
                 material_code, material_name, item_spec, demand_quantity,
                 demand_unit, expected_arrival_date, keeper_name,
                 keeper_phone, dispatch_no, remarks, resource_1, driver_1,
                 resource_2, driver_2, resource_3, driver_3, confirmer,
                 completed_at_raw, completed_at, confirmation_status_raw,
                 raw_data)
            VALUES
                (:request_id, :source_sheet, :source_row_no,
                 :source_row_hash, :source_kind,
                 :source_availability_status, :request_date,
                 :engineering_no, :section_no, :pallet_no, :pallet_name,
                 :delivery_status_raw, :prewarehouse_status_raw,
                 :pickup_status_raw, :supplier_name, :work_order_status_raw,
                 :applicant_raw, :department, :delivery_address,
                 :contact_name, :contact_phone, :demand_window_raw,
                 :demand_start_at, :demand_end_at, :material_code,
                 :material_name, :item_spec, :demand_quantity,
                 :demand_unit, :expected_arrival_date, :keeper_name,
                 :keeper_phone, :dispatch_no, :remarks, :resource_1,
                 :driver_1, :resource_2, :driver_2, :resource_3, :driver_3,
                 :confirmer, :completed_at_raw, :completed_at,
                 :confirmation_status_raw, :raw_data)
            RETURNING line_id
            """
        ),
        {
            "request_id": request_id,
            "source_sheet": line.source_sheet[:128],
            "source_row_no": line.source_row_no,
            "source_row_hash": line.source_row_hash,
            "source_kind": line.source_kind,
            "source_availability_status": line.source_availability_status,
            "request_date": line.request_date,
            "engineering_no": line.engineering_no[:64],
            "section_no": line.section_no,
            "pallet_no": line.pallet_no[:128],
            "pallet_name": line.pallet_name[:255],
            "delivery_status_raw": (
                line.delivery_status[:128] if line.delivery_status else None
            ),
            "prewarehouse_status_raw": (
                line.prewarehouse_status[:128] if line.prewarehouse_status else None
            ),
            "pickup_status_raw": (
                line.pickup_status[:128] if line.pickup_status else None
            ),
            "supplier_name": (line.supplier_name[:200] if line.supplier_name else None),
            "work_order_status_raw": (
                line.work_order_status_raw[:128] if line.work_order_status_raw else None
            ),
            "applicant_raw": (line.applicant_raw[:255] if line.applicant_raw else None),
            "department": line.department[:255] if line.department else None,
            "delivery_address": (
                line.delivery_address[:255] if line.delivery_address else None
            ),
            "contact_name": (line.contact_name[:100] if line.contact_name else None),
            "contact_phone": (line.contact_phone[:50] if line.contact_phone else None),
            "demand_window_raw": (
                line.demand_window_raw[:255] if line.demand_window_raw else None
            ),
            "demand_start_at": line.demand_start_at,
            "demand_end_at": line.demand_end_at,
            "material_code": (line.material_code[:128] if line.material_code else None),
            "material_name": (line.material_name[:255] if line.material_name else None),
            "item_spec": line.item_spec,
            "demand_quantity": line.demand_quantity,
            "demand_unit": line.demand_unit[:32],
            "expected_arrival_date": line.expected_arrival_date,
            "keeper_name": (line.keeper_name[:100] if line.keeper_name else None),
            "keeper_phone": (line.keeper_phone[:50] if line.keeper_phone else None),
            "dispatch_no": (line.dispatch_no[:128] if line.dispatch_no else None),
            "remarks": line.remarks[:500] if line.remarks else None,
            "resource_1": line.resource_1,
            "driver_1": line.driver_1,
            "resource_2": line.resource_2,
            "driver_2": line.driver_2,
            "resource_3": line.resource_3,
            "driver_3": line.driver_3,
            "confirmer": line.confirmer,
            "completed_at_raw": line.completed_at_raw,
            "completed_at": line.completed_at,
            "confirmation_status_raw": line.confirmation_status,
            "raw_data": Json(line.raw_data),
        },
    ).fetchone()
    return str(row.line_id)


def create_daily_pickup_request(
    db,
    *,
    warehouse_id: int,
    source_file_name: str,
    content_type: str,
    content: bytes,
    request_no: str | None,
    created_by: str,
) -> dict[str, Any]:
    _check_warehouse(db, warehouse_id)
    file_sha = _sha256(content)
    duplicate = db.execute(
        text(
            """
            SELECT request_id
              FROM shipyard_pickup_requests
             WHERE warehouse_id = :warehouse_id
               AND source_file_sha256 = :file_sha
               AND status <> :void
             LIMIT 1
            """
        ),
        {
            "warehouse_id": warehouse_id,
            "file_sha": file_sha,
            "void": SHIPYARD_PICKUP_VOID,
        },
    ).fetchone()
    if duplicate is not None:
        raise DuplicatePickupRequestFileError(
            "该日配送/按需需求文件已经导入",
            {"request_id": str(duplicate.request_id)},
        )
    parsed = parse_pickup_workbook(
        content,
        source_file_name=source_file_name,
    )
    resolved_no = (
        (request_no or "").strip()
        or f"PICK-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(2).upper()}"
    )
    request_date = max(
        (line.request_date for line in parsed.lines if line.request_date),
        default=None,
    )
    metadata = {
        **parsed.metadata,
        "warnings": list(parsed.warnings),
    }
    header = db.execute(
        text(
            """
            INSERT INTO shipyard_pickup_requests
                (warehouse_id, request_no, source_type, request_date,
                 source_file_name, source_file_sha256,
                 source_template_version, status, metadata, created_by)
            VALUES
                (:warehouse_id, :request_no, 'DAILY_DEMAND', :request_date,
                 :source_file_name, :file_sha, :template_version, :status,
                 :metadata, :created_by)
            RETURNING request_id
            """
        ),
        {
            "warehouse_id": warehouse_id,
            "request_no": resolved_no[:128],
            "request_date": request_date,
            "source_file_name": source_file_name[:255],
            "file_sha": file_sha,
            "template_version": parsed.metadata["parser_version"][:64],
            "status": SHIPYARD_PICKUP_CONFIRMED,
            "metadata": Json(metadata),
            "created_by": created_by,
        },
    ).fetchone()
    request_id = str(header.request_id)
    db.execute(
        text(
            """
            INSERT INTO shipyard_pickup_request_files
                (request_id, file_kind, file_name, content_type, file_size,
                 file_sha256, file_bytes)
            VALUES
                (:request_id, 'SOURCE', :file_name, :content_type,
                 :file_size, :file_sha, :file_bytes)
            """
        ),
        {
            "request_id": request_id,
            "file_name": source_file_name[:255],
            "content_type": content_type[:128],
            "file_size": len(content),
            "file_sha": file_sha,
            "file_bytes": content,
        },
    )
    for line in parsed.lines:
        _insert_parsed_line(db, request_id=request_id, line=line)
    _refresh_request_matches(db, request_id)
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_PICKUP_REQUEST_IMPORTED,
        entity_type="shipyard_pickup_request",
        entity_id=0,
        user_id=created_by,
        warehouse_id=warehouse_id,
        details={
            "request_id": request_id,
            "request_no": resolved_no,
            "line_count": len(parsed.lines),
            "daily_demand_count": parsed.metadata["daily_demand_count"],
            "on_demand_count": parsed.metadata["on_demand_count"],
        },
    )
    db.commit()
    return get_pickup_request_payload(db, request_id=request_id)


def create_special_pickup_request(
    db,
    *,
    warehouse_id: int,
    request_no: str,
    dispatch_no: str,
    request_date,
    lines,
    evidence: tuple[str, str, bytes] | None,
    created_by: str,
) -> dict[str, Any]:
    _check_warehouse(db, warehouse_id)
    duplicate = db.execute(
        text(
            """
            SELECT request_id
              FROM shipyard_pickup_requests
             WHERE warehouse_id = :warehouse_id
               AND request_no = :request_no
               AND status <> :void
             LIMIT 1
            """
        ),
        {
            "warehouse_id": warehouse_id,
            "request_no": request_no,
            "void": SHIPYARD_PICKUP_VOID,
        },
    ).fetchone()
    if duplicate is not None:
        raise DuplicatePickupRequestFileError(
            "该急单/改单派工号已经登记",
            {"request_id": str(duplicate.request_id)},
        )
    header = db.execute(
        text(
            """
            INSERT INTO shipyard_pickup_requests
                (warehouse_id, request_no, source_type, request_date,
                 dispatch_no, source_file_name, source_template_version,
                 status, metadata, created_by)
            VALUES
                (:warehouse_id, :request_no, 'SPECIAL_DISPATCH',
                 :request_date, :dispatch_no, :source_file_name,
                 'shipyard-special-dispatch-v1', :status, :metadata,
                 :created_by)
            RETURNING request_id
            """
        ),
        {
            "warehouse_id": warehouse_id,
            "request_no": request_no[:128],
            "request_date": request_date,
            "dispatch_no": dispatch_no[:128],
            "source_file_name": evidence[0][:255] if evidence is not None else None,
            "status": SHIPYARD_PICKUP_CONFIRMED,
            "metadata": Json({"entry_mode": "MANUAL_OR_IMAGE_BACKED"}),
            "created_by": created_by,
        },
    ).fetchone()
    request_id = str(header.request_id)
    if evidence is not None:
        file_name, content_type, content = evidence
        db.execute(
            text(
                """
                INSERT INTO shipyard_pickup_request_files
                    (request_id, file_kind, file_name, content_type,
                     file_size, file_sha256, file_bytes)
                VALUES
                    (:request_id, 'EVIDENCE', :file_name, :content_type,
                     :file_size, :file_sha, :file_bytes)
                """
            ),
            {
                "request_id": request_id,
                "file_name": file_name[:255],
                "content_type": content_type[:128],
                "file_size": len(content),
                "file_sha": _sha256(content),
                "file_bytes": content,
            },
        )
    for index, line in enumerate(lines, start=1):
        raw_data = {
            "form_template": "shipyard_special_dispatch",
            "line_no": line.line_no,
            "manual_entry": True,
        }
        source_row_hash = _sha256(
            (
                f"{request_id}:{line.line_no}:{line.engineering_no}:"
                f"{line.pallet_no}:{line.quantity}"
            ).encode()
        )
        db.execute(
            text(
                """
                INSERT INTO shipyard_pickup_request_lines
                    (request_id, source_sheet, source_row_no,
                     source_row_hash, source_kind, request_date,
                     engineering_no, section_no, pallet_no, pallet_name,
                     material_code, material_name, item_spec,
                     demand_quantity, demand_unit, expected_arrival_date,
                     keeper_name, keeper_phone, delivery_address, remarks,
                     dispatch_no, raw_data)
                VALUES
                    (:request_id, '派工单', :source_row_no,
                     :source_row_hash, 'SPECIAL_DISPATCH', :request_date,
                     :engineering_no, :section_no, :pallet_no, :pallet_name,
                     :material_code, :material_name, :item_spec,
                     :demand_quantity, :demand_unit, :expected_arrival_date,
                     :keeper_name, :keeper_phone, :delivery_address,
                     :remarks, :dispatch_no, :raw_data)
                """
            ),
            {
                "request_id": request_id,
                "source_row_no": line.line_no[:32],
                "source_row_hash": source_row_hash,
                "request_date": request_date,
                "engineering_no": line.engineering_no,
                "section_no": line.section_no,
                "pallet_no": line.pallet_no,
                "pallet_name": line.pallet_name,
                "material_code": line.material_code,
                "material_name": line.material_name,
                "item_spec": line.item_spec,
                "demand_quantity": line.quantity,
                "demand_unit": line.unit,
                "expected_arrival_date": line.expected_arrival_date,
                "keeper_name": line.keeper_name,
                "keeper_phone": line.keeper_phone,
                "delivery_address": line.delivery_address,
                "remarks": line.remarks,
                "dispatch_no": dispatch_no,
                "raw_data": Json(raw_data),
            },
        )
    _refresh_request_matches(db, request_id)
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_PICKUP_REQUEST_IMPORTED,
        entity_type="shipyard_pickup_request",
        entity_id=0,
        user_id=created_by,
        warehouse_id=warehouse_id,
        details={
            "request_id": request_id,
            "request_no": request_no,
            "dispatch_no": dispatch_no,
            "source_type": "SPECIAL_DISPATCH",
            "line_count": len(lines),
        },
    )
    db.commit()
    return get_pickup_request_payload(db, request_id=request_id)


def confirm_pickup_request(
    db,
    *,
    request_id: str,
    confirmed_by: str,
) -> dict[str, Any]:
    header = _request_header(db, request_id, for_update=True)
    if header is None:
        raise PickupRequestNotFoundError("配送/提货需求不存在")
    if header.status == SHIPYARD_PICKUP_CONFIRMED:
        return get_pickup_request_payload(db, request_id=request_id)
    if header.status != SHIPYARD_PICKUP_PARSED:
        raise PickupRequestStateError(
            "当前配送/提货需求不能确认",
            {"status": header.status},
        )
    _refresh_request_matches(db, request_id)
    db.execute(
        text(
            """
            UPDATE shipyard_pickup_requests
               SET status = :status, updated_at = NOW()
             WHERE request_id = :request_id
            """
        ),
        {"status": SHIPYARD_PICKUP_CONFIRMED, "request_id": request_id},
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_PICKUP_REQUEST_CONFIRMED,
        entity_type="shipyard_pickup_request",
        entity_id=0,
        user_id=confirmed_by,
        warehouse_id=header.warehouse_id,
        details={
            "request_id": request_id,
            "request_no": header.request_no,
        },
    )
    db.commit()
    return get_pickup_request_payload(db, request_id=request_id)


def rematch_pickup_request(
    db,
    *,
    request_id: str,
    actor: str,
) -> dict[str, Any]:
    header = _request_header(db, request_id, for_update=True)
    if header is None:
        raise PickupRequestNotFoundError("配送/提货需求不存在")
    if header.status == SHIPYARD_PICKUP_VOID:
        raise PickupRequestStateError("已作废需求不能再重匹配")
    _refresh_request_matches(db, request_id)
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_PICKUP_REQUEST_LINE_MATCHED,
        entity_type="shipyard_pickup_request",
        entity_id=0,
        user_id=actor,
        warehouse_id=header.warehouse_id,
        details={"request_id": request_id, "action": "REMATCH_ALL"},
    )
    db.commit()
    return get_pickup_request_payload(db, request_id=request_id)


def select_pickup_line_ticket(
    db,
    *,
    request_id: str,
    line_id: str,
    ticket_id: str,
    actor: str,
) -> dict[str, Any]:
    header = _request_header(db, request_id, for_update=True)
    if header is None:
        raise PickupRequestNotFoundError("配送/提货需求不存在")
    line = db.execute(
        text(
            """
            SELECT *
              FROM shipyard_pickup_request_lines
             WHERE request_id = :request_id
               AND line_id = :line_id
             FOR UPDATE
            """
        ),
        {"request_id": request_id, "line_id": line_id},
    ).fetchone()
    if line is None:
        raise PickupLineNotFoundError("配送需求行不存在")
    if line.outbound_task_id:
        raise PickupLineStateError("该需求行已经生成出库任务，不能改匹配")
    match = _match_line(db, warehouse_id=header.warehouse_id, line=line)
    candidates = match["details"].get("candidates", [])
    conflict_ticket_ids = {
        item["ticket_id"] for item in match["details"].get("material_conflicts", [])
    }
    selected = next(
        (
            candidate
            for candidate in candidates
            if candidate["ticket_id"] == ticket_id
            and candidate["eligible"]
            and candidate["ticket_id"] not in conflict_ticket_ids
        ),
        None,
    )
    if selected is None:
        raise PickupLineStateError(
            "指定票货不是当前可匹配库存",
            {"ticket_id": ticket_id},
        )
    if line.demand_quantity is not None and Decimal(
        selected["available_quantity"]
    ) != Decimal(line.demand_quantity):
        raise PickupLineStateError(
            "整票数量与提货数量不一致，系统不拆票",
            {
                "ticket_quantity": selected["available_quantity"],
                "request_quantity": _decimal_text(line.demand_quantity),
            },
        )
    db.execute(
        text(
            """
            UPDATE shipyard_pickup_request_lines
               SET match_status = :status,
                   ticket_id = :ticket_id,
                   available_quantity = :available_quantity,
                   match_details = :details,
                   updated_at = NOW()
             WHERE line_id = :line_id
            """
        ),
        {
            "status": SHIPYARD_PICKUP_AVAILABLE,
            "ticket_id": ticket_id,
            "available_quantity": Decimal(selected["available_quantity"]),
            "details": Json(
                {
                    **match["details"],
                    "selected_ticket": selected,
                    "selected_by": actor,
                    "selection_mode": "MANUAL",
                }
            ),
            "line_id": line_id,
        },
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_PICKUP_REQUEST_LINE_MATCHED,
        entity_type="shipyard_pickup_request_line",
        entity_id=0,
        user_id=actor,
        warehouse_id=header.warehouse_id,
        details={
            "request_id": request_id,
            "line_id": line_id,
            "ticket_id": ticket_id,
            "selection_mode": "MANUAL",
        },
    )
    db.commit()
    return get_pickup_request_payload(db, request_id=request_id)


def _ticket_for_outbound(db, line, *, warehouse_id: int):
    return db.execute(
        text(
            """
            SELECT t.ticket_id, t.ticket_no, t.declared_quantity,
                   t.received_quantity, t.on_hand_quantity,
                   t.expected_pallet_count, t.assigned_bin_id,
                   t.location_path_snapshot, t.stock_lot_number,
                   t.status, l.ship_no,
                   COALESCE(
                       NULLIF(BTRIM(l.engineering_no), ''),
                       NULLIF(BTRIM(pb.engineering_no), ''),
                       BTRIM(l.ship_no)
                   ) AS engineering_no,
                   COALESCE(
                       NULLIF(BTRIM(l.production_drawing_no), ''),
                       NULLIF(BTRIM(pb.production_drawing_no), ''),
                       ''
                   ) AS production_drawing_no,
                   COALESCE(
                       NULLIF(BTRIM(l.manufacturer_name), ''),
                       NULLIF(BTRIM(pb.manufacturer_name), ''),
                       ''
                   ) AS manufacturer_name,
                   l.material_code, l.item_name, l.item_spec, l.unit,
                   b.bin_code, z.zone_code, z.zone_name
              FROM shipyard_cargo_tickets t
              JOIN shipyard_inbound_lines l
                ON l.line_id = t.inbound_line_id
              JOIN shipyard_inbound_manifests im
                ON im.manifest_id = l.manifest_id
              JOIN bins b ON b.bin_id = t.assigned_bin_id
              LEFT JOIN zones z ON z.zone_id = b.zone_id
              LEFT JOIN shipyard_project_bindings pb
                ON pb.binding_id = l.project_binding_id
             WHERE t.ticket_id = :ticket_id
               AND im.warehouse_id = :warehouse_id
               AND t.status IN (:in_stock, :partial)
               AND t.on_hand_quantity > 0
               AND NOT EXISTS (
                   SELECT 1
                     FROM shipyard_outbound_tasks ot
                    WHERE ot.ticket_id = t.ticket_id
               )
             FOR UPDATE OF t
            """
        ),
        {
            "ticket_id": str(line.ticket_id),
            "warehouse_id": warehouse_id,
            "in_stock": SHIPYARD_TICKET_IN_STOCK,
            "partial": SHIPYARD_TICKET_PARTIAL,
        },
    ).fetchone()


def generate_pickup_outbound(
    db,
    *,
    request_id: str,
    line_ids: list[str] | None,
    actor: str,
) -> dict[str, Any]:
    header = _request_header(db, request_id, for_update=True)
    if header is None:
        raise PickupRequestNotFoundError("配送/提货需求不存在")
    if header.status != SHIPYARD_PICKUP_CONFIRMED:
        raise PickupRequestStateError(
            "配送/提货需求确认后才能生成出库任务",
            {"status": header.status},
        )
    selected_ids = {str(item) for item in line_ids or []}
    lines = _request_lines(db, request_id, for_update=True)
    candidates = []
    for line in lines:
        if selected_ids and str(line.line_id) not in selected_ids:
            continue
        if line.match_status != SHIPYARD_PICKUP_AVAILABLE or not line.ticket_id:
            continue
        candidates.append(line)
    if not candidates:
        existing = next(
            (
                line
                for line in lines
                if (
                    selected_ids
                    and str(line.line_id) in selected_ids
                    and line.outbound_order_id
                )
            ),
            None,
        )
        if existing is not None:
            return {
                **get_pickup_request_payload(db, request_id=request_id),
                "outbound_order": get_outbound_order_payload(
                    db,
                    outbound_order_id=str(existing.outbound_order_id),
                ),
            }
        raise PickupLineStateError(
            "没有可生成出库任务的库存已匹配行",
            {"request_id": request_id},
        )

    ticket_contexts: dict[str, Any] = {}
    seen_tickets: set[str] = set()
    selected_lines = []
    duplicate_lines = []
    for line in candidates:
        ticket_id = str(line.ticket_id)
        if ticket_id in seen_tickets:
            duplicate_lines.append(str(line.line_id))
            continue
        context = _ticket_for_outbound(
            db,
            line,
            warehouse_id=header.warehouse_id,
        )
        if context is None:
            duplicate_lines.append(str(line.line_id))
            continue
        pallets = _pallets(db, ticket_id)
        suffixes = [pallet.tag_suffix for pallet in pallets]
        if (
            not pallets
            or any(not suffix for suffix in suffixes)
            or len(set(suffixes)) != len(suffixes)
            or any(pallet.status != "STORED" for pallet in pallets)
        ):
            duplicate_lines.append(str(line.line_id))
            continue
        seen_tickets.add(ticket_id)
        ticket_contexts[ticket_id] = context
        selected_lines.append((line, context, pallets))
    if duplicate_lines:
        db.execute(
            text(
                """
                UPDATE shipyard_pickup_request_lines
                   SET match_status = :status,
                       ticket_id = NULL,
                       available_quantity = 0,
                       updated_at = NOW()
                 WHERE line_id = ANY(:line_ids)
                """
            ),
            {
                "status": SHIPYARD_PICKUP_DUPLICATE_DEMAND,
                "line_ids": duplicate_lines,
            },
        )
    if not selected_lines:
        db.commit()
        return get_pickup_request_payload(db, request_id=request_id)

    generated_seed = (
        f"{request_id}:{','.join(sorted(str(line.line_id) for line, _, _ in selected_lines))}:"
        f"{datetime.now(UTC).isoformat()}"
    )
    generated_sha = hashlib.sha256(generated_seed.encode()).hexdigest()
    manifest_no = (
        f"PICKOUT-{datetime.now(UTC):%Y%m%d%H%M%S}-{secrets.token_hex(3).upper()}"
    )
    shipment_no = (header.dispatch_no or header.request_no)[:128]
    manifest = db.execute(
        text(
            """
            INSERT INTO shipyard_outbound_manifests
                (warehouse_id, manifest_no, factory_shipment_no,
                 source_file_name, source_file_sha256,
                 source_template_version, status, metadata, created_by)
            VALUES
                (:warehouse_id, :manifest_no, :factory_shipment_no,
                 :source_file_name, :file_sha, 'shipyard-pickup-v1',
                 :status, :metadata, :created_by)
            RETURNING manifest_id
            """
        ),
        {
            "warehouse_id": header.warehouse_id,
            "manifest_no": manifest_no,
            "factory_shipment_no": shipment_no,
            "source_file_name": (header.source_file_name or "急单/改单派工单")[:255],
            "file_sha": generated_sha,
            "status": SHIPYARD_OUTBOUND_MANIFEST_PARSED,
            "metadata": Json(
                {
                    "pickup_request_id": request_id,
                    "pickup_request_no": header.request_no,
                    "source_type": header.source_type,
                    "allocation_policy": "whole_ticket_by_pickup_request",
                }
            ),
            "created_by": actor,
        },
    ).fetchone()
    manifest_id = str(manifest.manifest_id)
    order_no = f"OUT-{datetime.now(UTC):%Y%m%d-%H%M%S}-{secrets.token_hex(3).upper()}"
    order = db.execute(
        text(
            """
            INSERT INTO shipyard_outbound_orders
                (warehouse_id, outbound_order_no, manifest_id,
                 factory_shipment_no, status, confirmed_by, metadata)
            VALUES
                (:warehouse_id, :order_no, :manifest_id,
                 :factory_shipment_no, :status, :confirmed_by,
                 :metadata)
            RETURNING outbound_order_id
            """
        ),
        {
            "warehouse_id": header.warehouse_id,
            "order_no": order_no,
            "manifest_id": manifest_id,
            "factory_shipment_no": shipment_no,
            "status": SHIPYARD_OUTBOUND_ORDER_PENDING,
            "confirmed_by": actor,
            "metadata": Json(
                {
                    "pickup_request_id": request_id,
                    "pickup_request_no": header.request_no,
                }
            ),
        },
    ).fetchone()
    order_id = str(order.outbound_order_id)
    outbound_links = []
    for line, context, pallets in selected_lines:
        material_code = (context.material_code or f"PALLET-{line.pallet_no}")[:128]
        item_name = line.material_name or context.item_name or line.pallet_name
        quantity = Decimal(context.on_hand_quantity)
        suffixes = [pallet.tag_suffix for pallet in pallets]
        manifest_line = db.execute(
            text(
                """
                INSERT INTO shipyard_outbound_manifest_lines
                    (manifest_id, source_row_no, ticket_no, ship_no,
                     engineering_no, production_drawing_no,
                     manufacturer_name, material_code, item_name, item_spec,
                     demand_quantity, unit, demand_date,
                     expected_pallet_count, expected_tag_suffixes,
                     status, raw_data)
                VALUES
                    (:manifest_id, :source_row_no, :ticket_no, :ship_no,
                     :engineering_no, :production_drawing_no,
                     :manufacturer_name, :material_code, :item_name,
                     :item_spec, :demand_quantity, :unit, :demand_date,
                     :expected_pallet_count, :expected_tag_suffixes,
                     :status, :raw_data)
                RETURNING line_id
                """
            ),
            {
                "manifest_id": manifest_id,
                "source_row_no": (f"{line.source_sheet[:32]}:{line.source_row_no}")[
                    :64
                ],
                "ticket_no": context.ticket_no,
                "ship_no": context.ship_no,
                "engineering_no": line.engineering_no,
                "production_drawing_no": (
                    context.production_drawing_no or line.pallet_no
                ),
                "manufacturer_name": context.manufacturer_name or "",
                "material_code": material_code,
                "item_name": item_name[:255],
                "item_spec": line.item_spec or context.item_spec,
                "demand_quantity": quantity,
                "unit": context.unit or line.demand_unit or "件",
                "demand_date": line.request_date,
                "expected_pallet_count": len(pallets),
                "expected_tag_suffixes": suffixes,
                "status": SHIPYARD_OUTBOUND_LINE_ALLOCATED,
                "raw_data": Json(
                    {
                        "pickup_request_id": request_id,
                        "pickup_line_id": str(line.line_id),
                        "pickup_pallet_no": line.pallet_no,
                    }
                ),
            },
        ).fetchone()
        order_line = db.execute(
            text(
                """
                INSERT INTO shipyard_outbound_order_lines
                    (outbound_order_id, manifest_line_id, source_row_no,
                     ticket_no, ship_no, engineering_no,
                     production_drawing_no, manufacturer_name,
                     material_code, item_name, item_spec,
                     demand_quantity, unit, allocated_quantity,
                     fulfilled_quantity, status, demand_date)
                VALUES
                    (:outbound_order_id, :manifest_line_id, :source_row_no,
                     :ticket_no, :ship_no, :engineering_no,
                     :production_drawing_no, :manufacturer_name,
                     :material_code, :item_name, :item_spec,
                     :demand_quantity, :unit, :allocated_quantity, 0,
                     :status, :demand_date)
                RETURNING order_line_id
                """
            ),
            {
                "outbound_order_id": order_id,
                "manifest_line_id": str(manifest_line.line_id),
                "source_row_no": (f"{line.source_sheet[:32]}:{line.source_row_no}")[
                    :64
                ],
                "ticket_no": context.ticket_no,
                "ship_no": context.ship_no,
                "engineering_no": line.engineering_no,
                "production_drawing_no": (
                    context.production_drawing_no or line.pallet_no
                ),
                "manufacturer_name": context.manufacturer_name or "",
                "material_code": material_code,
                "item_name": item_name[:255],
                "item_spec": line.item_spec or context.item_spec,
                "demand_quantity": quantity,
                "unit": context.unit or line.demand_unit or "件",
                "allocated_quantity": quantity,
                "status": SHIPYARD_OUTBOUND_LINE_ALLOCATED,
                "demand_date": line.request_date,
            },
        ).fetchone()
        task_no = (f"{order_no}-{line.source_row_no}-{context.ticket_no}")[:160]
        task = db.execute(
            text(
                """
                INSERT INTO shipyard_outbound_tasks
                    (task_no, outbound_order_id, order_line_id, ticket_id,
                     planned_quantity, expected_pallet_count,
                     expected_tag_suffixes, location_path_snapshot,
                     material_snapshot, status)
                VALUES
                    (:task_no, :outbound_order_id, :order_line_id,
                     :ticket_id, :planned_quantity, :expected_pallet_count,
                     :expected_tag_suffixes, :location_path_snapshot,
                     :material_snapshot, :status)
                RETURNING task_id
                """
            ),
            {
                "task_no": task_no,
                "outbound_order_id": order_id,
                "order_line_id": str(order_line.order_line_id),
                "ticket_id": str(context.ticket_id),
                "planned_quantity": quantity,
                "expected_pallet_count": len(pallets),
                "expected_tag_suffixes": suffixes,
                "location_path_snapshot": context.location_path_snapshot,
                "material_snapshot": Json(
                    {
                        "material_code": material_code,
                        "item_name": item_name,
                        "item_spec": line.item_spec or context.item_spec,
                        "ticket_no": context.ticket_no,
                        "pickup_pallet_no": line.pallet_no,
                        "identity_mode": "PALLET_ONLY",
                        "required_ocr_fields": [
                            "ship_no",
                            "engineering_no",
                            "production_drawing_no",
                            "tag_suffix",
                            "label_quantity",
                        ],
                    }
                ),
                "status": SHIPYARD_OUTBOUND_TASK_PENDING,
            },
        ).fetchone()
        db.execute(
            text(
                """
                UPDATE shipyard_cargo_tickets
                   SET status = :status, updated_at = NOW()
                 WHERE ticket_id = :ticket_id
                """
            ),
            {
                "status": SHIPYARD_TICKET_ALLOCATED,
                "ticket_id": str(context.ticket_id),
            },
        )
        db.execute(
            text(
                """
                UPDATE shipyard_pickup_request_lines
                   SET match_status = :status,
                       outbound_order_id = :outbound_order_id,
                       outbound_task_id = :outbound_task_id,
                       updated_at = NOW()
                 WHERE line_id = :line_id
                """
            ),
            {
                "status": SHIPYARD_PICKUP_OUTBOUND_PENDING,
                "outbound_order_id": order_id,
                "outbound_task_id": str(task.task_id),
                "line_id": str(line.line_id),
            },
        )
        outbound_links.append(
            {
                "pickup_line_id": str(line.line_id),
                "ticket_id": str(context.ticket_id),
                "task_id": str(task.task_id),
            }
        )
    db.execute(
        text(
            """
            UPDATE shipyard_outbound_manifests
               SET status = :status, updated_at = NOW()
             WHERE manifest_id = :manifest_id
            """
        ),
        {
            "status": SHIPYARD_OUTBOUND_MANIFEST_CONFIRMED,
            "manifest_id": manifest_id,
        },
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_PICKUP_REQUEST_OUTBOUND_CREATED,
        entity_type="shipyard_pickup_request",
        entity_id=0,
        user_id=actor,
        warehouse_id=header.warehouse_id,
        details={
            "request_id": request_id,
            "request_no": header.request_no,
            "outbound_order_id": order_id,
            "outbound_order_no": order_no,
            "task_count": len(outbound_links),
            "duplicate_line_count": len(duplicate_lines),
        },
    )
    db.commit()
    payload = get_pickup_request_payload(db, request_id=request_id)
    payload["outbound_order"] = get_outbound_order_payload(
        db,
        outbound_order_id=order_id,
    )
    payload["created_task_links"] = outbound_links
    return payload


def void_pickup_request(
    db,
    *,
    request_id: str,
    reason: str,
    actor: str,
) -> dict[str, Any]:
    header = _request_header(db, request_id, for_update=True)
    if header is None:
        raise PickupRequestNotFoundError("配送/提货需求不存在")
    if header.status == SHIPYARD_PICKUP_VOID:
        raise PickupRequestStateError("需求已经作废")
    linked = db.execute(
        text(
            """
            SELECT COUNT(*)
              FROM shipyard_pickup_request_lines
             WHERE request_id = :request_id
               AND outbound_task_id IS NOT NULL
            """
        ),
        {"request_id": request_id},
    ).scalar()
    if linked:
        raise PickupRequestStateError(
            "已经生成出库任务的配送/提货需求不能作废",
            {"linked_task_count": linked},
        )
    db.execute(
        text(
            """
            UPDATE shipyard_pickup_requests
               SET status = :status,
                   metadata = metadata || :metadata,
                   updated_at = NOW()
             WHERE request_id = :request_id
            """
        ),
        {
            "status": SHIPYARD_PICKUP_VOID,
            "metadata": Json(
                {
                    "void_reason": reason,
                    "voided_by": actor,
                    "voided_at": datetime.now(UTC).isoformat(),
                }
            ),
            "request_id": request_id,
        },
    )
    db.execute(
        text(
            """
            UPDATE shipyard_pickup_request_lines
               SET match_status = 'VOID', updated_at = NOW()
             WHERE request_id = :request_id
            """
        ),
        {"request_id": request_id},
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_PICKUP_REQUEST_VOIDED,
        entity_type="shipyard_pickup_request",
        entity_id=0,
        user_id=actor,
        warehouse_id=header.warehouse_id,
        details={
            "request_id": request_id,
            "request_no": header.request_no,
            "reason": reason,
        },
    )
    db.commit()
    return get_pickup_request_payload(db, request_id=request_id)


def get_pickup_request_file(db, *, request_id: str) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            SELECT r.warehouse_id, f.file_name, f.content_type, f.file_size,
                   f.file_sha256, f.file_bytes
              FROM shipyard_pickup_request_files f
              JOIN shipyard_pickup_requests r
                ON r.request_id = f.request_id
             WHERE f.request_id = :request_id
             ORDER BY CASE f.file_kind WHEN 'SOURCE' THEN 0 ELSE 1 END,
                      f.created_at
             LIMIT 1
            """
        ),
        {"request_id": request_id},
    ).fetchone()
    if row is None:
        raise PickupRequestNotFoundError("配送/提货需求原文件不存在")
    return {
        "warehouse_id": row.warehouse_id,
        "file_name": row.file_name,
        "content_type": row.content_type,
        "file_size": row.file_size,
        "file_sha256": row.file_sha256,
        "file_bytes": bytes(row.file_bytes),
    }


def save_pickup_evidence(
    db,
    *,
    request_id: str,
    file_name: str,
    content_type: str,
    content: bytes,
    uploaded_by: str,
) -> dict[str, Any]:
    header = _request_header(db, request_id)
    if header is None:
        raise PickupRequestNotFoundError("配送/提货需求不存在")
    if header.status == SHIPYARD_PICKUP_VOID:
        raise PickupRequestStateError("已作废需求不能上传凭证")
    if not content:
        raise PickupRequestStateError("上传文件为空")
    file_sha = _sha256(content)
    db.execute(
        text(
            """
            INSERT INTO shipyard_pickup_request_files
                (request_id, file_kind, file_name, content_type, file_size,
                 file_sha256, file_bytes)
            VALUES
                (:request_id, 'EVIDENCE', :file_name, :content_type,
                 :file_size, :file_sha, :file_bytes)
            ON CONFLICT (request_id, file_kind, file_sha256) DO NOTHING
            """
        ),
        {
            "request_id": request_id,
            "file_name": file_name[:255],
            "content_type": content_type[:128],
            "file_size": len(content),
            "file_sha": file_sha,
            "file_bytes": content,
        },
    )
    db.execute(
        text(
            """
            UPDATE shipyard_pickup_requests
               SET source_file_name = COALESCE(source_file_name, :file_name),
                   updated_at = NOW()
             WHERE request_id = :request_id
            """
        ),
        {"request_id": request_id, "file_name": file_name[:255]},
    )
    write_audit_log(
        db,
        action_type=ACTION_SHIPYARD_PICKUP_REQUEST_IMPORTED,
        entity_type="shipyard_pickup_request",
        entity_id=0,
        user_id=uploaded_by,
        warehouse_id=header.warehouse_id,
        details={
            "request_id": request_id,
            "action": "EVIDENCE_UPLOADED",
            "file_name": file_name,
            "file_sha256": file_sha,
        },
    )
    db.commit()
    return get_pickup_request_payload(db, request_id=request_id)
