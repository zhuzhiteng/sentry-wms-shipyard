import uuid
from io import BytesIO

from db_test_context import get_raw_connection
from shipyard.helpers import (
    PNG_BYTES,
    create_binding,
    create_zone_mapping,
    outbound_manifest_bytes,
    single_line_manifest_bytes,
    upload_manifest,
    upload_outbound_manifest,
)

from services.shipyard import outbound_turnover_service, receipt_service
from services.shipyard.label_ocr import LabelOcrResult


def _confirm_manifest(client, headers, manifest_id):
    response = client.post(
        f"/api/shipyard/inbound/manifests/{manifest_id}/confirm",
        json={},
        headers=headers,
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def _received_material_ticket(
    client,
    headers,
    monkeypatch,
    *,
    ship_no=None,
    material_code="MAT-001",
    item_name="船用支架",
    item_spec="S-100",
    quantity=4,
    suffixes=("1-1", "1-2"),
):
    ship_no = ship_no or f"H{uuid.uuid4().hex[:8].upper()}"
    create_binding(client, headers, ship_no)
    create_zone_mapping(client, headers, ship_no)
    preview = upload_manifest(
        client,
        headers,
        single_line_manifest_bytes(
            ship_no,
            quantity=quantity,
            material_code=material_code,
            item_name=item_name,
            item_spec=item_spec,
        ),
        manifest_no=f"IN-{uuid.uuid4().hex[:10]}",
    )
    assert preview.status_code == 201, preview.get_json()
    confirmed = _confirm_manifest(
        client,
        headers,
        preview.get_json()["manifest"]["manifest_id"],
    )
    ticket = confirmed["tickets"][0]
    connection = get_raw_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE shipyard_cargo_tickets
           SET expected_pallet_count = %s
         WHERE ticket_id = %s
        """,
        (len(suffixes), ticket["ticket_id"]),
    )
    cursor.close()

    results = [
        LabelOcrResult(
            provider="baidu",
            raw_text="",
            raw_lines=[],
            fields={
                "ship_no": ship_no,
                "engineering_no": ship_no,
                "production_drawing_no": "D-001",
                "material_code": material_code,
                "item_name": item_name,
                "item_spec": item_spec,
                "batch_no": f"{ship_no}-BATCH-1",
                "tag_suffix": suffix,
                "label_quantity": str(quantity),
            },
            confidence=0.98,
        )
        for suffix in suffixes
    ]
    monkeypatch.setattr(
        receipt_service,
        "recognize_label_image",
        lambda _content: results.pop(0),
    )
    pallets = []
    for suffix in suffixes:
        response = client.post(
            f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/label-recognition",
            data={"file": (BytesIO(PNG_BYTES), f"label-{suffix}.png")},
            content_type="multipart/form-data",
            headers=headers,
        )
        assert response.status_code == 201, response.get_json()
        data = response.get_json()
        pallets.append(
            {
                "tag_suffix": suffix,
                "label_recognition_id": data["recognition"]["recognition_id"],
                "photo_id": data["photo"]["photo_id"],
            }
        )
    receipt = client.post(
        f"/api/shipyard/inbound/tickets/{ticket['ticket_id']}/receipts",
        json={
            "idempotency_key": f"receipt-{uuid.uuid4()}",
            "received_quantity": quantity,
            "pallets": pallets,
        },
        headers=headers,
    )
    assert receipt.status_code == 200, receipt.get_json()
    return {**ticket, **receipt.get_json()["ticket"]}


def _preview_and_confirm_outbound(
    client,
    headers,
    *,
    ship_no="H9301",
    material_code="MAT-001",
    item_name="船用支架",
    demand_quantity=4,
    file_name=None,
):
    preview = upload_outbound_manifest(
        client,
        headers,
        outbound_manifest_bytes(
            ship_no=ship_no,
            material_code=material_code,
            item_name=item_name,
            demand_quantity=demand_quantity,
        ),
        file_name=file_name or f"outbound-{uuid.uuid4().hex}.xlsx",
    )
    assert preview.status_code == 201, preview.get_json()
    manifest = preview.get_json()
    assert manifest["validation"]["summary"]["task_count"] == 1, manifest["lines"][0]
    confirmed = client.post(
        "/api/shipyard/outbound/formal-manifests/"
        f"{manifest['manifest']['manifest_id']}/confirm",
        json={},
        headers=headers,
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    return manifest, confirmed.get_json()


def test_formal_outbound_preview_requires_exact_material_identity(
    client,
    auth_headers,
    monkeypatch,
):
    ticket = _received_material_ticket(client, auth_headers, monkeypatch)

    mismatch = upload_outbound_manifest(
        client,
        auth_headers,
        outbound_manifest_bytes(
            ship_no=ticket["ship_no"],
            material_code="MAT-001",
            item_name="另一个品名",
        ),
        file_name="wrong-material-name.xlsx",
    )
    assert mismatch.status_code == 201, mismatch.get_json()
    mismatch_data = mismatch.get_json()
    assert mismatch_data["validation"]["summary"]["task_count"] == 0
    assert mismatch_data["lines"][0]["match_status"] == "SHORTAGE"
    assert mismatch_data["lines"][0]["shortage_quantity"] == "4.000"

    missing_code = client.post(
        "/api/shipyard/outbound/formal-manifests/preview",
        data={"warehouse_id": "1"},
        content_type="multipart/form-data",
        headers=auth_headers,
    )
    assert missing_code.status_code == 400


def test_outbound_binding_mismatch_names_the_conflicting_field(
    client,
    auth_headers,
    monkeypatch,
):
    ticket = _received_material_ticket(client, auth_headers, monkeypatch)
    preview = upload_outbound_manifest(
        client,
        auth_headers,
        outbound_manifest_bytes(
            ship_no=ticket["ship_no"],
            manufacturer="另一个厂家",
            material_code="MAT-001",
            item_name="船用支架",
            item_spec="S-100",
        ),
        file_name="wrong-manufacturer.xlsx",
    )
    assert preview.status_code == 201, preview.get_json()
    line = preview.get_json()["lines"][0]
    assert line["binding"]["status"] == "MISMATCH"
    assert line["binding"]["mismatched_fields"] == ["manufacturer_name"]
    assert "厂家" in line["binding"]["message"]


def test_ocr_wrong_task_returns_the_candidate_task(
    client,
    auth_headers,
    monkeypatch,
):
    first_ticket = _received_material_ticket(
        client,
        auth_headers,
        monkeypatch,
        material_code="MAT-FIRST",
        item_name="首票物料",
    )
    second_ticket = _received_material_ticket(
        client,
        auth_headers,
        monkeypatch,
        material_code="MAT-SECOND",
        item_name="第二票物料",
    )
    _first_manifest, first_order = _preview_and_confirm_outbound(
        client,
        auth_headers,
        ship_no=first_ticket["ship_no"],
        material_code="MAT-FIRST",
        item_name="首票物料",
    )
    _second_manifest, second_order = _preview_and_confirm_outbound(
        client,
        auth_headers,
        ship_no=second_ticket["ship_no"],
        material_code="MAT-SECOND",
        item_name="第二票物料",
    )
    first_task = first_order["tasks"][0]
    second_task = second_order["tasks"][0]
    monkeypatch.setattr(
        outbound_turnover_service,
        "recognize_label_image",
        lambda _content: LabelOcrResult(
            provider="e2e-stub",
            raw_text="",
            raw_lines=[],
            fields={
                "material_code": "MAT-SECOND",
                "item_name": "第二票物料",
                "item_spec": "S-100",
                "tag_suffix": "1-1",
                "label_quantity": "4",
            },
            confidence=0.99,
        ),
    )

    response = client.post(
        f"/api/shipyard/outbound/tasks/{first_task['task_id']}/label-recognition",
        data={"file": (BytesIO(PNG_BYTES), "wrong-task.png")},
        content_type="multipart/form-data",
        headers=auth_headers,
    )

    assert response.status_code == 201, response.get_json()
    data = response.get_json()
    assert data["matched"] is False
    assert {
        item["field"] for item in data["mismatches"] if item["reason"] == "MISMATCH"
    } >= {"material_code", "item_name"}
    assert [item["task_id"] for item in data["candidate_tasks"]] == [
        second_task["task_id"]
    ]


def test_outbound_ocr_rejects_wrong_item_spec(
    client,
    auth_headers,
    monkeypatch,
):
    ticket = _received_material_ticket(client, auth_headers, monkeypatch)
    _manifest, order = _preview_and_confirm_outbound(
        client,
        auth_headers,
        ship_no=ticket["ship_no"],
    )
    task = order["tasks"][0]
    monkeypatch.setattr(
        outbound_turnover_service,
        "recognize_label_image",
        lambda _content: LabelOcrResult(
            provider="baidu",
            raw_text="",
            raw_lines=[],
            fields={
                "ship_no": ticket["ship_no"],
                "engineering_no": ticket["ship_no"],
                "production_drawing_no": "D-001",
                "material_code": "MAT-001",
                "item_name": "船用支架",
                "item_spec": "S-WRONG",
                "tag_suffix": "1-1",
                "label_quantity": "4",
            },
            confidence=0.98,
        ),
    )

    response = client.post(
        f"/api/shipyard/outbound/tasks/{task['task_id']}/label-recognition",
        data={"file": (BytesIO(PNG_BYTES), "wrong-spec-label.png")},
        content_type="multipart/form-data",
        headers=auth_headers,
    )

    assert response.status_code == 201, response.get_json()
    data = response.get_json()
    assert data["matched"] is False
    assert data["mismatches"] == [
        {
            "field": "item_spec",
            "expected": "S-100",
            "actual": "S-WRONG",
            "reason": "MISMATCH",
        }
    ]


def test_outbound_does_not_split_ticket_larger_than_remaining_demand(
    client,
    auth_headers,
    monkeypatch,
):
    ticket = _received_material_ticket(client, auth_headers, monkeypatch)
    preview = upload_outbound_manifest(
        client,
        auth_headers,
        outbound_manifest_bytes(
            ship_no=ticket["ship_no"],
            material_code="MAT-001",
            item_name="船用支架",
            demand_quantity=3,
        ),
        file_name="demand-less-than-ticket.xlsx",
    )
    assert preview.status_code == 201, preview.get_json()
    data = preview.get_json()
    assert data["lines"][0]["tasks"] == []
    assert data["lines"][0]["match_status"] == "SHORTAGE"
    assert any(
        item["code"] == "WHOLE_TICKET_EXCEEDS_REMAINING_DEMAND"
        for item in data["lines"][0]["diagnostics"]
    )


def test_partial_received_stock_can_be_allocated_without_waiting_for_shortage(
    client,
    auth_headers,
    monkeypatch,
):
    ticket = _received_material_ticket(client, auth_headers, monkeypatch)
    connection = get_raw_connection()
    cursor = connection.cursor()
    cursor.execute(
        """
        UPDATE shipyard_cargo_tickets
           SET status = 'PARTIAL',
               on_hand_quantity = 3
         WHERE ticket_id = %s
        """,
        (ticket["ticket_id"],),
    )
    cursor.close()

    preview = upload_outbound_manifest(
        client,
        auth_headers,
        outbound_manifest_bytes(
            ship_no=ticket["ship_no"],
            material_code="MAT-001",
            item_name="船用支架",
            demand_quantity=3,
        ),
        file_name="partial-stock-outbound.xlsx",
    )
    assert preview.status_code == 201, preview.get_json()
    data = preview.get_json()
    assert data["validation"]["summary"]["task_count"] == 1
    assert data["lines"][0]["tasks"][0]["available_quantity"] == "3.000"
    assert data["lines"][0]["shortage_quantity"] == "0.000"


def test_duplicate_formal_outbound_file_is_rejected(
    client,
    auth_headers,
    monkeypatch,
):
    ticket = _received_material_ticket(client, auth_headers, monkeypatch)
    content = outbound_manifest_bytes(
        ship_no=ticket["ship_no"],
        material_code="MAT-001",
        item_name="船用支架",
    )
    first = upload_outbound_manifest(
        client,
        auth_headers,
        content,
        file_name="formal-duplicate.xlsx",
    )
    assert first.status_code == 201, first.get_json()
    repeated = upload_outbound_manifest(
        client,
        auth_headers,
        content,
        file_name="formal-duplicate-copy.xlsx",
    )
    assert repeated.status_code == 409
    assert repeated.get_json()["error"] == "duplicate_outbound_manifest_file"


def test_outbound_role_can_use_mobile_tasks_but_cannot_import(
    client,
    auth_headers,
    monkeypatch,
):
    ticket = _received_material_ticket(client, auth_headers, monkeypatch)
    _manifest, order = _preview_and_confirm_outbound(
        client,
        auth_headers,
        ship_no=ticket["ship_no"],
    )
    task = order["tasks"][0]

    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin"},
    )
    assert login.status_code == 200
    username = f"outbound-{uuid.uuid4().hex[:8]}"
    user = client.post(
        "/api/admin/users",
        json={
            "username": username,
            "password": "RoleDemo2026!",
            "full_name": "出库员",
            "role": "OUTBOUND",
            "warehouse_ids": [1],
        },
        headers=auth_headers,
    )
    assert user.status_code == 201, user.get_json()
    permissions = client.put(
        f"/api/admin/users/{user.get_json()['user_id']}/permissions",
        json={
            "page_keys": [
                "dashboard",
                "inventory",
                "shipyard-outbound",
            ]
        },
        headers=auth_headers,
    )
    assert permissions.status_code == 200, permissions.get_json()
    role_login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "RoleDemo2026!"},
    )
    role_headers = {
        "Authorization": f"Bearer {role_login.get_json()['token']}",
    }
    listing = client.get(
        "/api/shipyard/outbound/mobile/orders?warehouse_id=1",
        headers=role_headers,
    )
    assert listing.status_code == 200, listing.get_json()
    assert listing.get_json()["orders"][0]["task_count"] == 1

    monkeypatch.setattr(
        outbound_turnover_service,
        "recognize_label_image",
        lambda _content: LabelOcrResult(
            provider="baidu",
            raw_text="",
            raw_lines=[],
            fields={
                "ship_no": ticket["ship_no"],
                "engineering_no": ticket["ship_no"],
                "production_drawing_no": "D-001",
                "material_code": "MAT-001",
                "item_name": "船用支架",
                "item_spec": "S-100",
                "batch_no": f"{ticket['ship_no']}-BATCH-1",
                "tag_suffix": "1-1",
                "label_quantity": "4",
            },
            confidence=0.98,
        ),
    )
    label = client.post(
        f"/api/shipyard/outbound/tasks/{task['task_id']}/label-recognition",
        data={"file": (BytesIO(PNG_BYTES), "outbound-label.png")},
        content_type="multipart/form-data",
        headers=role_headers,
    )
    assert label.status_code == 201, label.get_json()

    import_attempt = client.post(
        "/api/shipyard/outbound/formal-manifests/preview",
        data={"warehouse_id": "1"},
        content_type="multipart/form-data",
        headers=role_headers,
    )
    assert import_attempt.status_code == 403
    assert import_attempt.get_json()["error"] == "role_forbidden"

    # Keep the received ticket referenced to make the fixture intent explicit.
    assert ticket["status"] == "IN_STOCK"
