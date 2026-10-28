"""Shared fixtures for shipyard API workflow tests."""

from io import BytesIO

from db_test_context import get_raw_connection
from openpyxl import Workbook

PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


def workbook_bytes(rows, title="外高桥"):
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = title
    for row in rows:
        worksheet.append(row)
    auxiliary = workbook.create_sheet("Sheet1")
    auxiliary.append([None, None])
    auxiliary.append(["辅助物料", 24])
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


def manifest_bytes(ship_no="H9001", manufacturer="测试船舶配件有限公司"):
    return workbook_bytes(
        [
            [manufacturer],
            ["2026舾装件发货清单"],
            [
                "序号",
                "船号",
                "品名",
                "图号",
                "图名",
                "件数",
                "PO NO",
                "送货单号",
                "PO号",
                "重量",
                "螺丝",
            ],
            [
                "1",
                ship_no,
                "舾装件",
                "D-001",
                "制作图A",
                15,
                "9001",
                "0929501",
                "SWS202600001",
                39,
                "基座",
            ],
            [
                "2",
                ship_no,
                "舾装件",
                "D-002",
                "制作图B",
                5,
                "9002",
                "0929501",
                "SWS202600002",
                8,
                "托架",
            ],
        ]
    )


def single_line_manifest_bytes(
    ship_no,
    *,
    quantity=4,
    drawing="D-001",
    manufacturer="测试船舶配件有限公司",
    material_code=None,
    item_name="舾装件",
    item_spec=None,
):
    headers = ["序号", "船号", "品名", "图号", "图名", "件数"]
    values = ["1", ship_no, item_name, drawing, "制作图A", quantity]
    if material_code is not None:
        headers.insert(3, "物料编码")
        values.insert(3, material_code)
    if item_spec is not None:
        headers.insert(-1, "型号规格")
        values.insert(-1, item_spec)
    return workbook_bytes(
        [
            [manufacturer],
            ["2026舾装件发货清单"],
            headers,
            values,
        ]
    )


def manifest_bytes_without_drawing(ship_no, manufacturer="测试船舶配件有限公司"):
    return workbook_bytes(
        [
            [manufacturer],
            ["2026舾装件发货清单"],
            ["序号", "船号", "品名", "件数"],
            ["1", ship_no, "舾装件", 5],
        ]
    )


def outbound_manifest_bytes(
    *,
    factory_shipment_no="CX-20261027-001",
    ship_no="H9301",
    engineering_no=None,
    drawing="D-001",
    manufacturer="测试船舶配件有限公司",
    material_code="MAT-001",
    item_name="船用支架",
    item_spec="S-100",
    demand_quantity=4,
    expected_pallet_count=2,
    suffixes="1-1、1-2",
):
    return workbook_bytes(
        [
            ["外高桥船厂正式出库清单"],
            [
                "清单行号",
                "船厂出货单号",
                "托组号",
                "船号",
                "工程号",
                "制作图号",
                "厂家",
                "物料编码",
                "物料名称",
                "规格型号",
                "需求数量",
                "单位",
                "需求日期",
                "预计托盘数",
                "托盘后缀",
            ],
            [
                "1",
                factory_shipment_no,
                "T930101",
                ship_no,
                engineering_no or ship_no,
                drawing,
                manufacturer,
                material_code,
                item_name,
                item_spec,
                demand_quantity,
                "件",
                "2026-10-27",
                expected_pallet_count,
                suffixes,
            ],
        ]
    )


def first_bin():
    conn = get_raw_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT b.bin_id, b.bin_code, z.zone_code
          FROM bins b
          JOIN zones z ON z.zone_id = b.zone_id
         WHERE b.warehouse_id = 1
         ORDER BY b.bin_id
         LIMIT 1
        """
    )
    row = cur.fetchone()
    cur.close()
    return row


def create_binding(client, headers, ship_no, drawing="D-001"):
    response = client.post(
        "/api/shipyard/bindings",
        json={
            "ship_no": ship_no,
            "engineering_no": ship_no,
            "production_drawing_no": drawing,
            "manufacturer_name": "测试船舶配件有限公司",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["binding"]


def create_zone_mapping(client, headers, ship_no):
    bin_id, _bin_code, _zone_code = first_bin()
    response = client.post(
        "/api/shipyard/zone-mappings",
        json={
            "warehouse_id": 1,
            "bin_id": bin_id,
            "ship_no": ship_no,
            "is_primary": True,
        },
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()["mapping"]


def upload_manifest(
    client,
    headers,
    content,
    manifest_no="IN-H9001-001",
):
    return client.post(
        "/api/shipyard/inbound/manifests/preview",
        data={
            "warehouse_id": "1",
            "manifest_no": manifest_no,
            "file": (BytesIO(content), "外高桥发货清单.xlsx"),
        },
        content_type="multipart/form-data",
        headers=headers,
    )


def confirm_manifest(client, headers, manifest_id):
    response = client.post(
        f"/api/shipyard/inbound/manifests/{manifest_id}/confirm",
        json={},
        headers=headers,
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def upload_outbound_manifest(
    client,
    headers,
    content,
    *,
    file_name="shipyard-outbound.xlsx",
    warehouse_id=1,
):
    return client.post(
        "/api/shipyard/outbound/formal-manifests/preview",
        data={
            "warehouse_id": str(warehouse_id),
            "file": (BytesIO(content), file_name),
        },
        content_type="multipart/form-data",
        headers=headers,
    )
