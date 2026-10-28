import uuid
from io import BytesIO

from db_test_context import get_raw_connection
from shipyard.helpers import (
    PNG_BYTES,
    confirm_manifest,
    create_binding,
    create_zone_mapping,
    single_line_manifest_bytes,
    upload_manifest,
)

from services.shipyard import receipt_service
from services.shipyard.label_ocr import LabelOcrResult


def _confirmed_ticket(client, headers, ship_no):
    create_binding(client, headers, ship_no, drawing="D-001")
    create_zone_mapping(client, headers, ship_no)
    preview = upload_manifest(
        client,
        headers,
        single_line_manifest_bytes(
            ship_no,
            quantity=4,
            drawing="D-001",
            material_code="MAT-RECEIPT",
            item_name="船用支架",
            item_spec="S-RECEIPT",
        ),
        manifest_no=f"IN-RECEIPT-{uuid.uuid4().hex[:10]}",
    )
    assert preview.status_code == 201, preview.get_json()
    ticket = confirm_manifest(
        client,
        headers,
        preview.get_json()["manifest"]["manifest_id"],
    )["tickets"][0]
    connection = get_raw_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE shipyard_cargo_tickets
           SET expected_pallet_count = 2
         WHERE ticket_id = %s
        """,
        (ticket["ticket_id"],),
    )
    cursor.close()
    return ticket


def _labels(ship_no, suffixes, *, quantity=4):
    return [
        LabelOcrResult(
            provider="baidu",
            raw_text="",
            raw_lines=[],
            fields={
                "ship_no": ship_no,
                "engineering_no": ship_no,
                "production_drawing_no": "D-001",
                "material_code": "MAT-RECEIPT",
                "item_name": "船用支架",
                "item_spec": "S-RECEIPT",
                "batch_no": f"{ship_no}-BATCH-1",
                "tag_suffix": suffix,
                "label_quantity": str(quantity),
            },
            confidence=0.98,
        )
        for suffix in suffixes
    ]


def _recognize(client, headers, ticket_id, monkeypatch, labels):
    monkeypatch.setattr(
        receipt_service,
        "recognize_label_image",
        lambda _content: labels.pop(0),
    )
    evidence = []
    responses = []
    for index in range(len(labels) + len(evidence)):
        response = client.post(
            f"/api/shipyard/inbound/tickets/{ticket_id}/label-recognition",
            data={"file": (BytesIO(PNG_BYTES), f"label-{index}.png")},
            content_type="multipart/form-data",
            headers=headers,
        )
        assert response.status_code == 201, response.get_json()
        data = response.get_json()
        evidence.append(
            {
                "tag_suffix": data["recognized"]["tag_suffix"],
                "label_recognition_id": data["recognition"]["recognition_id"],
                "photo_id": data["photo"]["photo_id"],
            }
        )
        responses.append(data)
        if not labels:
            break
    return evidence, responses


def _receipt(client, headers, ticket_id, *, quantity, evidence, key):
    return client.post(
        f"/api/shipyard/inbound/tickets/{ticket_id}/receipts",
        json={
            "idempotency_key": key,
            "received_quantity": quantity,
            "client_device_id": "WX-TEST",
            "pallets": evidence,
        },
        headers=headers,
    )


def test_ocr_receipt_books_stock_and_is_idempotent(client, auth_headers, monkeypatch):
    ship_no = f"H{uuid.uuid4().hex[:8].upper()}"
    ticket = _confirmed_ticket(client, auth_headers, ship_no)
    evidence, responses = _recognize(
        client,
        auth_headers,
        ticket["ticket_id"],
        monkeypatch,
        _labels(ship_no, ("1-1", "1-2")),
    )
    assert len(responses) == 2

    key = f"receipt-{uuid.uuid4()}"
    received = _receipt(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=4,
        evidence=evidence,
        key=key,
    )
    assert received.status_code == 200, received.get_json()
    data = received.get_json()
    assert data["ticket"]["status"] == "IN_STOCK"
    assert data["ticket"]["on_hand_quantity"] == "4.000"
    assert {item["tag_suffix"] for item in data["pallets"]} == {"1-1", "1-2"}

    repeated = _receipt(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=4,
        evidence=evidence,
        key=key,
    )
    assert repeated.status_code == 200
    assert (
        repeated.get_json()["receipt"]["receipt_id"]
        == data["receipt"]["receipt_id"]
    )

    conflicting = _receipt(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=4,
        evidence=evidence,
        key=f"receipt-new-{uuid.uuid4()}",
    )
    assert conflicting.status_code == 409


def test_partial_receipt_and_shortage_replenishment(
    client,
    auth_headers,
    monkeypatch,
):
    ship_no = f"H{uuid.uuid4().hex[:8].upper()}"
    ticket = _confirmed_ticket(client, auth_headers, ship_no)
    first, _ = _recognize(
        client,
        auth_headers,
        ticket["ticket_id"],
        monkeypatch,
        _labels(ship_no, ("1-1",)),
    )
    partial = _receipt(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=2,
        evidence=first,
        key=f"receipt-partial-{uuid.uuid4()}",
    )
    assert partial.status_code == 200, partial.get_json()
    assert partial.get_json()["ticket"]["status"] == "PARTIAL"
    assert partial.get_json()["ticket"]["short_quantity"] == "2.000"

    shortage = client.put(
        f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/shortage",
        json={"status": "IN_TRANSIT", "note": "补发在途"},
        headers=auth_headers,
    )
    assert shortage.status_code == 200, shortage.get_json()

    second, _ = _recognize(
        client,
        auth_headers,
        ticket["ticket_id"],
        monkeypatch,
        _labels(ship_no, ("1-2",)),
    )
    replenished = _receipt(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=2,
        evidence=second,
        key=f"receipt-replenish-{uuid.uuid4()}",
    )
    assert replenished.status_code == 200, replenished.get_json()
    data = replenished.get_json()
    assert data["ticket"]["status"] == "IN_STOCK"
    assert data["ticket"]["shortage_status"] == "REPLENISHED"


def test_receipt_rejects_duplicate_pallet_and_over_receipt(
    client,
    auth_headers,
    monkeypatch,
):
    ship_no = f"H{uuid.uuid4().hex[:8].upper()}"
    ticket = _confirmed_ticket(client, auth_headers, ship_no)
    evidence, _ = _recognize(
        client,
        auth_headers,
        ticket["ticket_id"],
        monkeypatch,
        _labels(ship_no, ("1-1",)),
    )

    over = _receipt(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=5,
        evidence=evidence,
        key=f"receipt-over-{uuid.uuid4()}",
    )
    assert over.status_code == 422

    duplicate = _receipt(
        client,
        auth_headers,
        ticket["ticket_id"],
        quantity=2,
        evidence=evidence + evidence,
        key=f"receipt-duplicate-{uuid.uuid4()}",
    )
    assert duplicate.status_code == 422
