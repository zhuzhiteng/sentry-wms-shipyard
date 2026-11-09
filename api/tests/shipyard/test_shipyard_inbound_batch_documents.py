from io import BytesIO

from services.shipyard import inbound_document_service
from services.shipyard.label_ocr import LabelOcrResult
from shipyard.helpers import (
    PNG_BYTES,
    create_binding,
    create_zone_mapping,
    single_line_manifest_bytes,
    upload_manifest,
)


def test_arrival_slip_parser_uses_table_values_not_instruction_notes(
    monkeypatch,
):
    raw_lines = [
        "上海外高桥造船有限公司到货验收单",
        "供应单位名称（可盖章）",
        "NoS 0916842",
        "宝应帆洋船舶电器配件制造有限公司送货人",
        "PO NO",
        "送货单号",
        "工程号",
        "916842",
        "H1405",
        "SWS202607080016",
        "注：1.阴影部分为保管员填写，其它各项由供应商在送货前填好。",
        "2.如送货物资为生产部门易耗品，则“工程号”填使用部门。",
        "3.如“订货量”与“供货量”相同，则直接在□内打√。",
        "供应商在送货前填好。",
    ]
    monkeypatch.setattr(
        inbound_document_service,
        "recognize_label_image",
        lambda _content: LabelOcrResult(
            provider="rapidocr",
            raw_text="\n".join(raw_lines),
            raw_lines=raw_lines,
            fields={},
            confidence=0.91,
        ),
    )

    payload = inbound_document_service.recognize_arrival_slip_fields(b"image")

    assert payload["missing_labels"] == []
    assert payload["fields"] == {
        "slip_no": "0916842",
        "delivery_note_no": "916842",
        "supplier_name": "宝应帆洋船舶电器配件制造有限公司",
        "engineering_no": "H1405",
        "purchase_order_no": "SWS202607080016",
    }


def test_arrival_slip_photo_returns_editable_ocr_fields(
    client,
    auth_headers,
    monkeypatch,
):
    ship_no = "HBATCH02"
    create_binding(client, auth_headers, ship_no)
    create_zone_mapping(client, auth_headers, ship_no)
    preview = upload_manifest(
        client,
        auth_headers,
        single_line_manifest_bytes(ship_no),
        manifest_no="IN-BATCH-002",
    )
    assert preview.status_code == 201, preview.get_json()
    manifest_id = preview.get_json()["manifest"]["manifest_id"]

    monkeypatch.setattr(
        inbound_document_service,
        "recognize_label_image",
        lambda _content: LabelOcrResult(
            provider="baidu",
            raw_text="",
            raw_lines=[
                "到货验收单号：0920156 送货单号：920156",
                "供应商：宝应帆洋船舶电器配件制造有限公司",
                "工程号：H1405",
                "PO NO：SWS202607080016",
            ],
            fields={},
            confidence=0.97,
        ),
    )

    recognized = client.post(
        f"/api/shipyard/inbound/manifests/{manifest_id}/arrival-slips/recognize",
        data={"file": (BytesIO(PNG_BYTES), "arrival-slip.png")},
        content_type="multipart/form-data",
        headers=auth_headers,
    )

    assert recognized.status_code == 200, recognized.get_json()
    payload = recognized.get_json()
    assert payload["missing_labels"] == []
    assert payload["fields"] == {
        "slip_no": "0920156",
        "delivery_note_no": "920156",
        "supplier_name": "宝应帆洋船舶电器配件制造有限公司",
        "engineering_no": "H1405",
        "purchase_order_no": "SWS202607080016",
    }


def test_inbound_ticket_can_be_deferred_and_arrival_slip_copies_are_recorded(
    client,
    auth_headers,
):
    ship_no = "HBATCH01"
    create_binding(client, auth_headers, ship_no)
    create_zone_mapping(client, auth_headers, ship_no)
    preview = upload_manifest(
        client,
        auth_headers,
        single_line_manifest_bytes(ship_no),
        manifest_no="IN-BATCH-001",
    )
    assert preview.status_code == 201, preview.get_json()
    manifest_id = preview.get_json()["manifest"]["manifest_id"]
    confirmed = client.post(
        f"/api/shipyard/inbound/manifests/{manifest_id}/confirm",
        json={},
        headers=auth_headers,
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    ticket_id = confirmed.get_json()["tickets"][0]["ticket_id"]

    deferred = client.post(
        f"/api/shipyard/inbound/tickets/{ticket_id}/deferral",
        json={
            "next_batch_date": "2026-10-30",
            "reason": "车辆装不下，随下一批送达",
        },
        headers=auth_headers,
    )
    assert deferred.status_code == 200, deferred.get_json()
    assert deferred.get_json()["ticket"]["delivery_status"] == "DEFERRED"
    assert deferred.get_json()["ticket"]["deferred_to_date"] == "2026-10-30"

    restored = client.delete(
        f"/api/shipyard/inbound/tickets/{ticket_id}/deferral",
        headers=auth_headers,
    )
    assert restored.status_code == 200, restored.get_json()
    assert restored.get_json()["ticket"]["delivery_status"] == "PENDING"
    assert restored.get_json()["ticket"]["deferred_to_date"] is None

    arrival = client.post(
        f"/api/shipyard/inbound/manifests/{manifest_id}/arrival-slips",
        data={
            "slip_no": "0920156",
            "delivery_note_no": "920156",
            "supplier_name": "宝应帆洋船舶电器配件制造有限公司",
            "engineering_no": ship_no,
            "purchase_order_no": "SWS202607080016",
            "is_urgent": "true",
            "factory_copy_count": "2",
            "shipyard_copy_count": "2",
            "warehouse_copy_retained": "true",
            "file": (BytesIO(PNG_BYTES), "four-part-arrival-slip.png"),
        },
        content_type="multipart/form-data",
        headers=auth_headers,
    )
    assert arrival.status_code == 201, arrival.get_json()
    slip = arrival.get_json()["arrival_slips"][0]
    assert slip["slip_no"] == "0920156"
    assert slip["factory_copy_count"] == 2
    assert slip["shipyard_copy_count"] == 2
    assert slip["warehouse_copy_retained"] is True

    updated = client.put(
        f"/api/shipyard/inbound/arrival-slips/{slip['arrival_slip_id']}",
        json={
            "slip_no": "0920156-A",
            "delivery_note_no": "920156-A",
            "supplier_name": "宝应帆洋船舶电器配件制造有限公司",
            "engineering_no": ship_no,
            "purchase_order_no": "SWS202607080016",
        },
        headers=auth_headers,
    )
    assert updated.status_code == 200, updated.get_json()
    assert updated.get_json()["arrival_slips"][0]["slip_no"] == "0920156-A"

    manifest = client.get(
        f"/api/shipyard/inbound/manifests/{manifest_id}",
        headers=auth_headers,
    )
    assert manifest.status_code == 200, manifest.get_json()
    assert manifest.get_json()["arrival_slips"][0]["slip_no"] == "0920156-A"
