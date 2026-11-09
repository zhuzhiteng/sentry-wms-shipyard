from decimal import Decimal
from io import BytesIO

from db_test_context import get_raw_connection
from openpyxl import Workbook
from shipyard.helpers import (
    PNG_BYTES,
    create_binding,
    create_zone_mapping,
    single_line_manifest_bytes,
    upload_manifest,
)

from services.shipyard import outbound_turnover_service
from services.shipyard.label_ocr import LabelOcrResult


def _pickup_bytes(ship_no, drawing):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "日需求1"
    worksheet.append(
        [
            "提货时间",
            "工程号",
            "分段号",
            "托盘号",
            "托盘名称",
            "到货情况",
            "前置仓状态",
            "供应商",
            "需求时间",
        ]
    )
    worksheet.append(
        [
            "2026-10-28",
            ship_no,
            "101",
            drawing,
            "101分段舾装件安装托盘表",
            "前置仓",
            "已到",
            "测试供应商",
            "2026-10-28 09:00:00~2026-10-28 09:30:00",
        ]
    )
    worksheet.append(
        [
            "2026-10-28",
            ship_no,
            "102",
            "MISSING-DRAWING",
            "仓库没有的托盘",
            "前置仓",
            "已到",
            "测试供应商",
            "2026-10-28 10:00:00~2026-10-28 10:30:00",
        ]
    )
    worksheet.append(
        [
            "2026-10-28",
            ship_no,
            "103",
            "UNAVAILABLE-DRAWING",
            "配送单标注前置仓未到",
            "未到",
            "29号发",
            "测试供应商",
            "2026-10-28 11:00:00~2026-10-28 11:30:00",
        ]
    )
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def _seed_in_stock_ticket(client, headers, ship_no, drawing, quantity=6):
    create_binding(client, headers, ship_no, drawing=drawing)
    create_zone_mapping(client, headers, ship_no)
    preview = upload_manifest(
        client,
        headers,
        single_line_manifest_bytes(
            ship_no,
            quantity=quantity,
            drawing=drawing,
        ),
        manifest_no=f"IN-PICK-{ship_no}",
    )
    assert preview.status_code == 201, preview.get_json()
    manifest_id = preview.get_json()["manifest"]["manifest_id"]
    confirmed = client.post(
        f"/api/shipyard/inbound/manifests/{manifest_id}/confirm",
        json={},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    ticket_id = confirmed.get_json()["tickets"][0]["ticket_id"]
    connection = get_raw_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE shipyard_cargo_tickets
           SET received_quantity = %s,
               on_hand_quantity = %s,
               expected_pallet_count = 1,
               status = 'IN_STOCK',
               delivery_status = 'RECEIVED'
         WHERE ticket_id = %s
        """,
        (quantity, quantity, ticket_id),
    )
    cursor.execute(
        """
        INSERT INTO shipyard_pallet_units
            (ticket_id, tag_suffix, status, received_by, received_at,
             label_fields)
        VALUES (%s, '1-1', 'STORED', 'admin', NOW(), '{}'::jsonb)
        """,
        (ticket_id,),
    )
    cursor.close()
    return ticket_id


def test_daily_pickup_keeps_warehouse_missing_lines_and_creates_whole_ticket_task(
    client,
    auth_headers,
    monkeypatch,
):
    ship_no = "HPICK01"
    drawing = "D-PICK-001"
    _seed_in_stock_ticket(client, auth_headers, ship_no, drawing)

    preview = client.post(
        "/api/shipyard/pickup-requests/daily/preview",
        data={
            "warehouse_id": "1",
            "request_no": "PICK-TEST-001",
            "file": (
                BytesIO(_pickup_bytes(ship_no, drawing)),
                "日配送需求.xlsx",
            ),
        },
        content_type="multipart/form-data",
        headers=auth_headers,
    )
    assert preview.status_code == 201, preview.get_json()
    payload = preview.get_json()
    assert payload["summary"]["line_count"] == 3
    assert payload["summary"]["available_count"] == 1
    assert payload["summary"]["warehouse_missing_count"] == 1
    assert payload["summary"]["source_unavailable_count"] == 1
    assert payload["request"]["status"] == "CONFIRMED"
    available = next(
        line for line in payload["lines"] if line["match_status"] == "AVAILABLE"
    )
    missing = next(
        line for line in payload["lines"] if line["match_status"] == "WAREHOUSE_MISSING"
    )
    source_unavailable = next(
        line
        for line in payload["lines"]
        if line["match_status"] == "SOURCE_UNAVAILABLE"
    )
    assert Decimal(available["available_quantity"]) == Decimal(6)
    assert missing["pallet_no"] == "MISSING-DRAWING"
    assert source_unavailable["pallet_no"] == "UNAVAILABLE-DRAWING"
    assert source_unavailable["delivery_status_raw"] == "未到"

    printed = client.get(
        f"/api/shipyard/pickup-requests/{payload['request']['request_id']}/print",
        headers=auth_headers,
    )
    assert printed.status_code == 200, printed.get_data(as_text=True)
    print_text = printed.get_data(as_text=True)
    assert drawing in print_text
    assert "本批可提 1 行" in print_text
    assert "MISSING-DRAWING" not in print_text
    assert "UNAVAILABLE-DRAWING" not in print_text
    assert "仓内无货" not in print_text
    full_print = client.get(
        f"/api/shipyard/pickup-requests/{payload['request']['request_id']}/print"
        "?include_unavailable=1",
        headers=auth_headers,
    )
    assert full_print.status_code == 200, full_print.get_data(as_text=True)
    assert "MISSING-DRAWING" in full_print.get_data(as_text=True)
    assert "UNAVAILABLE-DRAWING" in full_print.get_data(as_text=True)
    assert "本批无货 1 行，记录异常 1 行" in full_print.get_data(as_text=True)

    request_id = payload["request"]["request_id"]
    outbound = client.post(
        f"/api/shipyard/pickup-requests/{request_id}/outbound",
        json={"line_ids": [available["line_id"]]},
        headers=auth_headers,
    )
    assert outbound.status_code == 201, outbound.get_json()
    generated = outbound.get_json()
    assert len(generated["created_task_links"]) == 1
    assert generated["outbound_order"]["order"]["status"] == "PENDING"
    assert Decimal(
        generated["outbound_order"]["tasks"][0]["planned_quantity"]
    ) == Decimal(6)
    statuses = {line["line_id"]: line["match_status"] for line in generated["lines"]}
    assert statuses[available["line_id"]] == "OUTBOUND_PENDING"
    assert statuses[missing["line_id"]] == "WAREHOUSE_MISSING"
    assert statuses[source_unavailable["line_id"]] == "SOURCE_UNAVAILABLE"

    task = generated["outbound_order"]["tasks"][0]
    monkeypatch.setattr(
        outbound_turnover_service,
        "recognize_label_image",
        lambda _content: LabelOcrResult(
            provider="baidu",
            raw_text="",
            raw_lines=[],
            fields={
                "ship_no": ship_no,
                "engineering_no": ship_no,
                "production_drawing_no": drawing,
                "manufacturer_name": "测试船舶配件有限公司",
                "installation_pallet_no": "INSTALL-1-1",
                "installation_pallet_name": "制作图A",
                "destination": "外高桥",
                "tag_suffix": "1-1",
                "label_quantity": "6",
            },
            confidence=0.98,
        ),
    )
    recognized = client.post(
        f"/api/shipyard/outbound/tasks/{task['task_id']}/label-recognition",
        data={"file": (BytesIO(PNG_BYTES), "pickup-label.png")},
        content_type="multipart/form-data",
        headers=auth_headers,
    )
    assert recognized.status_code == 201, recognized.get_json()
    recognition = recognized.get_json()
    assert recognition["matched"] is True
    assert recognition["mismatches"] == []

    confirmed_outbound = client.post(
        f"/api/shipyard/outbound/tasks/{task['task_id']}/confirm",
        json={
            "idempotency_key": "pickup-outbound-test-001",
            "pallets": [
                {
                    "tag_suffix": "1-1",
                    "label_recognition_id": (
                        recognition["recognition"]["recognition_id"]
                    ),
                    "photo_id": recognition["photo"]["photo_id"],
                }
            ],
        },
        headers=auth_headers,
    )
    assert confirmed_outbound.status_code == 200, confirmed_outbound.get_json()
    refreshed = client.get(
        f"/api/shipyard/pickup-requests/{request_id}",
        headers=auth_headers,
    )
    assert refreshed.status_code == 200, refreshed.get_json()
    refreshed_available = next(
        line
        for line in refreshed.get_json()["lines"]
        if line["line_id"] == available["line_id"]
    )
    assert refreshed_available["match_status"] == "OUTBOUND"


def test_special_dispatch_uses_its_own_source_and_matches_ticket_quantity(
    client,
    auth_headers,
):
    ship_no = "HPICK02"
    drawing = "657ME201EM"
    _seed_in_stock_ticket(
        client,
        auth_headers,
        ship_no,
        drawing,
        quantity=6,
    )

    response = client.post(
        "/api/shipyard/pickup-requests/special",
        json={
            "warehouse_id": 1,
            "request_no": "SPECIAL-TEST-001",
            "dispatch_no": "PT202610298A00910004",
            "request_date": "2026-10-29",
            "lines": [
                {
                    "line_no": "1",
                    "engineering_no": ship_no,
                    "section_no": "657",
                    "pallet_no": drawing,
                    "pallet_name": "657分段电舾件安装托盘表",
                    "item_spec": "导缆式本安隔离栅基座-U1",
                    "quantity": 6,
                    "unit": "件",
                    "keeper_name": "马降纲",
                    "remarks": "0#平台",
                }
            ],
        },
        headers=auth_headers,
    )

    assert response.status_code == 201, response.get_json()
    payload = response.get_json()
    assert payload["request"]["source_type"] == "SPECIAL_DISPATCH"
    assert payload["request"]["dispatch_no"] == "PT202610298A00910004"
    assert payload["summary"]["available_count"] == 1
    line = payload["lines"][0]
    assert line["match_status"] == "AVAILABLE"
    assert Decimal(line["available_quantity"]) == Decimal(6)
    assert line["match_details"]["matching_policy"] == ("WHOLE_TICKET_EXACT_QUANTITY")
