"""Daily-delivery and special-dispatch pickup request API."""

from __future__ import annotations

import html
from io import BytesIO

from flask import Response, g, jsonify, request, send_file

from middleware.auth_middleware import (
    check_warehouse_access,
    require_admin_or_page_permission,
    require_auth,
)
from middleware.db import with_db
from schemas.shipyard import (
    ConfirmPickupRequest,
    CreateSpecialPickupRequest,
    GeneratePickupOutboundRequest,
    VoidPickupRequest,
)
from services.shipyard.pickup_parser import PickupParseError
from services.shipyard.pickup_request_service import (
    confirm_pickup_request,
    create_daily_pickup_request,
    create_special_pickup_request,
    generate_pickup_outbound,
    get_pickup_request_file,
    get_pickup_request_payload,
    list_pickup_requests,
    rematch_pickup_request,
    save_pickup_evidence,
    select_pickup_line_ticket,
    void_pickup_request,
)
from utils.uploads import UploadTooLargeError, read_upload_bytes, safe_upload_name
from utils.validation import validate_body

from . import require_shipyard_roles, shipyard_bp

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OUTBOUND_PAGE = "shipyard-outbound"
OUTBOUND_MANAGER_ROLES = (
    "SHIPYARD_MANAGER",
    "OUTBOUND",
)
RESULT_LABELS = {
    "PENDING": "处理中",
    "AVAILABLE": "可提货",
    "AMBIGUOUS": "需核对",
    "SOURCE_UNAVAILABLE": "本批无货",
    "WAREHOUSE_MISSING": "记录异常",
    "QUANTITY_MISMATCH": "数量需核对",
    "OUTBOUND_PENDING": "已生成出库任务",
    "OUTBOUND": "已出库",
    "DUPLICATE_DEMAND": "重复需求",
    "VOID": "已作废",
}


def _limit() -> int:
    try:
        return max(1, min(int(request.args.get("limit", "50")), 200))
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc


@shipyard_bp.get("/pickup-requests")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_pickup_requests():
    try:
        warehouse_id = int(request.args.get("warehouse_id", ""))
        limit = _limit()
    except (TypeError, ValueError):
        return jsonify({"error": "warehouse_id and limit must be integers"}), 400
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    return jsonify(
        list_pickup_requests(
            g.db,
            warehouse_id=warehouse_id,
            limit=limit,
        )
    )


@shipyard_bp.post("/pickup-requests/daily/preview")
@require_auth
@require_shipyard_roles(*OUTBOUND_MANAGER_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def preview_daily_pickup_request():
    if not request.mimetype.startswith("multipart/form-data"):
        return jsonify(
            {
                "error": "unsupported_media_type",
                "message": "请使用 multipart/form-data 上传配送需求 Excel",
            }
        ), 415
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "file_required"}), 400
    source_file_name = safe_upload_name(
        uploaded.filename,
        fallback="shipyard-pickup-demand.xlsx",
    )
    if not source_file_name.lower().endswith(".xlsx"):
        return jsonify(
            {
                "error": "unsupported_file_type",
                "message": "首期仅接受 .xlsx 日配送/按需需求表",
            }
        ), 415
    try:
        warehouse_id = int(request.form.get("warehouse_id", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "warehouse_id is required"}), 400
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    try:
        content = read_upload_bytes(uploaded)
    except UploadTooLargeError as exc:
        return jsonify({"error": "file_too_large", "message": str(exc)}), 413
    if not content:
        return jsonify({"error": "empty_file"}), 400
    try:
        payload = create_daily_pickup_request(
            g.db,
            warehouse_id=warehouse_id,
            source_file_name=source_file_name,
            content_type=XLSX_CONTENT_TYPE,
            content=content,
            request_no=request.form.get("request_no"),
            created_by=g.current_user["username"],
        )
    except PickupParseError as exc:
        return jsonify(
            {
                "error": "pickup_parse_failed",
                "message": str(exc),
                "diagnostics": exc.diagnostics,
            }
        ), 422
    return jsonify(payload), 201


@shipyard_bp.post("/pickup-requests/special")
@require_auth
@require_shipyard_roles(*OUTBOUND_MANAGER_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(CreateSpecialPickupRequest)
@with_db
def create_special_pickup(validated):
    ok, denied = check_warehouse_access(validated.warehouse_id)
    if not ok:
        return denied
    payload = create_special_pickup_request(
        g.db,
        warehouse_id=validated.warehouse_id,
        request_no=validated.request_no,
        dispatch_no=validated.dispatch_no,
        request_date=validated.request_date,
        lines=validated.lines,
        evidence=None,
        created_by=g.current_user["username"],
    )
    return jsonify(payload), 201


@shipyard_bp.get("/pickup-requests/<uuid:request_id>")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def get_pickup_request(request_id):
    payload = get_pickup_request_payload(g.db, request_id=str(request_id))
    ok, denied = check_warehouse_access(payload["request"]["warehouse_id"])
    if not ok:
        return denied
    return jsonify(payload)


@shipyard_bp.get("/pickup-requests/<uuid:request_id>/file")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def download_pickup_request_file(request_id):
    payload = get_pickup_request_file(g.db, request_id=str(request_id))
    ok, denied = check_warehouse_access(payload["warehouse_id"])
    if not ok:
        return denied
    return send_file(
        BytesIO(payload["file_bytes"]),
        mimetype=payload["content_type"],
        as_attachment=True,
        download_name=payload["file_name"],
        max_age=0,
    )


@shipyard_bp.post("/pickup-requests/<uuid:request_id>/evidence")
@require_auth
@require_shipyard_roles(*OUTBOUND_MANAGER_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def upload_pickup_evidence(request_id):
    if not request.mimetype.startswith("multipart/form-data"):
        return jsonify({"error": "unsupported_media_type"}), 415
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "file_required"}), 400
    current = get_pickup_request_payload(g.db, request_id=str(request_id))
    ok, denied = check_warehouse_access(current["request"]["warehouse_id"])
    if not ok:
        return denied
    content_type = uploaded.mimetype
    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
        return jsonify(
            {
                "error": "unsupported_file_type",
                "message": "派工单照片必须是 JPEG、PNG 或 WebP",
            }
        ), 415
    try:
        content = read_upload_bytes(uploaded)
    except UploadTooLargeError as exc:
        return jsonify({"error": "file_too_large", "message": str(exc)}), 413
    payload = save_pickup_evidence(
        g.db,
        request_id=str(request_id),
        file_name=safe_upload_name(
            uploaded.filename,
            fallback="special-dispatch.png",
        ),
        content_type=content_type,
        content=content,
        uploaded_by=g.current_user["username"],
    )
    return jsonify(payload), 201


@shipyard_bp.post("/pickup-requests/<uuid:request_id>/confirm")
@require_auth
@require_shipyard_roles(*OUTBOUND_MANAGER_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(ConfirmPickupRequest)
@with_db
def confirm_pickup(request_id, validated):
    del validated
    current = get_pickup_request_payload(g.db, request_id=str(request_id))
    ok, denied = check_warehouse_access(current["request"]["warehouse_id"])
    if not ok:
        return denied
    return jsonify(
        confirm_pickup_request(
            g.db,
            request_id=str(request_id),
            confirmed_by=g.current_user["username"],
        )
    )


@shipyard_bp.post("/pickup-requests/<uuid:request_id>/rematch")
@require_auth
@require_shipyard_roles(*OUTBOUND_MANAGER_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def rematch_pickup(request_id):
    current = get_pickup_request_payload(g.db, request_id=str(request_id))
    ok, denied = check_warehouse_access(current["request"]["warehouse_id"])
    if not ok:
        return denied
    return jsonify(
        rematch_pickup_request(
            g.db,
            request_id=str(request_id),
            actor=g.current_user["username"],
        )
    )


@shipyard_bp.put("/pickup-requests/<uuid:request_id>/lines/<uuid:line_id>/ticket")
@require_auth
@require_shipyard_roles(*OUTBOUND_MANAGER_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def select_pickup_ticket(request_id, line_id):
    ticket_id = (request.json or {}).get("ticket_id")
    if not ticket_id:
        return jsonify({"error": "ticket_id is required"}), 400
    current = get_pickup_request_payload(g.db, request_id=str(request_id))
    ok, denied = check_warehouse_access(current["request"]["warehouse_id"])
    if not ok:
        return denied
    return jsonify(
        select_pickup_line_ticket(
            g.db,
            request_id=str(request_id),
            line_id=str(line_id),
            ticket_id=str(ticket_id),
            actor=g.current_user["username"],
        )
    )


@shipyard_bp.post("/pickup-requests/<uuid:request_id>/outbound")
@require_auth
@require_shipyard_roles(*OUTBOUND_MANAGER_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(GeneratePickupOutboundRequest)
@with_db
def create_pickup_outbound(request_id, validated):
    current = get_pickup_request_payload(g.db, request_id=str(request_id))
    ok, denied = check_warehouse_access(current["request"]["warehouse_id"])
    if not ok:
        return denied
    payload = generate_pickup_outbound(
        g.db,
        request_id=str(request_id),
        line_ids=(
            [str(item) for item in validated.line_ids] if validated.line_ids else None
        ),
        actor=g.current_user["username"],
    )
    return jsonify(payload), 201


@shipyard_bp.post("/pickup-requests/<uuid:request_id>/void")
@require_auth
@require_shipyard_roles(*OUTBOUND_MANAGER_ROLES)
@require_admin_or_page_permission(OUTBOUND_PAGE)
@validate_body(VoidPickupRequest)
@with_db
def void_pickup(request_id, validated):
    current = get_pickup_request_payload(g.db, request_id=str(request_id))
    ok, denied = check_warehouse_access(current["request"]["warehouse_id"])
    if not ok:
        return denied
    return jsonify(
        void_pickup_request(
            g.db,
            request_id=str(request_id),
            reason=validated.reason,
            actor=g.current_user["username"],
        )
    )


@shipyard_bp.get("/pickup-requests/<uuid:request_id>/print")
@require_auth
@require_admin_or_page_permission(OUTBOUND_PAGE)
@with_db
def print_pickup_request(request_id):
    payload = get_pickup_request_payload(g.db, request_id=str(request_id))
    ok, denied = check_warehouse_access(payload["request"]["warehouse_id"])
    if not ok:
        return denied
    include_unavailable = request.args.get("include_unavailable") in {
        "1",
        "true",
        "yes",
    }
    print_lines = [
        line
        for line in payload["lines"]
        if include_unavailable
        or line["match_status"] in {"AVAILABLE", "OUTBOUND_PENDING"}
    ]
    actionable_count = sum(
        line["match_status"] in {"AVAILABLE", "OUTBOUND_PENDING"}
        for line in payload["lines"]
    )
    exception_count = (
        payload["summary"]["warehouse_missing_count"]
        + payload["summary"]["ambiguous_count"]
        + payload["summary"]["quantity_mismatch_count"]
    )
    rows = []
    for index, line in enumerate(print_lines, start=1):
        rows.append(
            "<tr>"
            f"<td>{index}</td>"
            f"<td>{html.escape(line['request_date'] or '')}</td>"
            f"<td>{html.escape(line['engineering_no'])}</td>"
            f"<td>{html.escape(line['section_no'] or '')}</td>"
            f"<td>{html.escape(line['pallet_no'])}</td>"
            f"<td>{html.escape(line['pallet_name'])}</td>"
            f"<td>{html.escape(line['material_name'] or '')}</td>"
            f"<td>{html.escape(line['item_spec'] or '')}</td>"
            f"<td class='num'>"
            f"{html.escape(line['demand_quantity'] or '整托')}</td>"
            f"<td>{html.escape(line['demand_unit'] or '')}</td>"
            f"<td>{html.escape(line['delivery_address'] or '')}</td>"
            f"<td>{html.escape(line['keeper_name'] or '')}</td>"
            f"<td>{html.escape(RESULT_LABELS.get(line['match_status'], line['match_status']))}</td>"
            f"<td>{html.escape(line['remarks'] or '')}</td>"
            "</tr>"
        )
    request_data = payload["request"]
    title_suffix = "完整需求" if include_unavailable else "前置仓有货提货清单"
    counts_text = (
        f"本批可提 {actionable_count} 行，本批无货 "
        f"{payload['summary']['source_unavailable_count']} 行，记录异常 "
        f"{exception_count} 行"
        if include_unavailable
        else f"本批可提 {actionable_count} 行"
    )
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>{title_suffix} {html.escape(request_data["request_no"])}</title>
  <style>
    @page {{ size: A4 landscape; margin: 9mm; }}
    * {{ box-sizing: border-box; }}
    body {{ margin: 0; font-family: "Microsoft YaHei", "SimSun", serif; color: #111; }}
    .sheet {{ border: 2px solid #111; }}
    .head {{ display: grid; grid-template-columns: 1fr 1.8fr 1fr; border-bottom: 1px solid #111; }}
    .head > div {{ min-height: 17mm; padding: 3mm; border-right: 1px solid #111; }}
    .head > div:last-child {{ border-right: 0; }}
    .title {{ text-align: center; font-size: 23px; font-weight: 700; }}
    .label {{ font-size: 10px; font-weight: 700; }}
    .value {{ margin-top: 2mm; font-size: 15px; font-weight: 700; }}
    .meta {{ padding: 2.5mm 3mm; border-bottom: 1px solid #111; font-size: 11px; }}
    table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
    th, td {{ border-right: 1px solid #111; border-bottom: 1px solid #111; padding: 1.7mm; font-size: 9.5px; }}
    th {{ text-align: center; font-weight: 700; }}
    th:last-child, td:last-child {{ border-right: 0; }}
    td.num {{ text-align: right; }}
    .signatures {{ display: grid; grid-template-columns: repeat(3, 1fr); }}
    .signatures > div {{ min-height: 20mm; padding: 3mm; border-right: 1px solid #111; }}
    .signatures > div:last-child {{ border-right: 0; }}
  </style>
</head>
<body>
  <main class="sheet">
    <div class="head">
      <div><div class="label">需求日期</div><div class="value">{html.escape(request_data["request_date"] or "")}</div></div>
      <div><div class="title">启东前置仓{title_suffix}</div></div>
      <div><div class="label">需求号</div><div class="value">{html.escape(request_data["request_no"])}</div></div>
    </div>
    <div class="meta">
      来源：{html.escape(request_data["source_type"])}
      　派工单号：{html.escape(request_data["dispatch_no"] or "—")}
      　文件：{html.escape(request_data["source_file_name"] or "手工录入")}
      　{counts_text}
      　打印 {len(print_lines)} 行
    </div>
    <table>
      <colgroup>
        <col style="width:4%"><col style="width:7%"><col style="width:7%">
        <col style="width:5%"><col style="width:11%"><col style="width:17%">
        <col style="width:11%"><col style="width:12%"><col style="width:6%">
        <col style="width:4%"><col style="width:8%"><col style="width:6%">
        <col style="width:7%"><col style="width:5%">
      </colgroup>
      <thead>
        <tr><th>序号</th><th>日期</th><th>工程号</th><th>分段</th><th>托盘号</th><th>托盘名称</th><th>物料/品名</th><th>规格</th><th>数量</th><th>单位</th><th>配送地址</th><th>保管员</th><th>处理结果</th><th>备注</th></tr>
      </thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    <div class="signatures">
      <div>前置仓信息员：<br><br>日期：</div>
      <div>前置仓保管员：<br><br>日期：</div>
      <div>船厂接收人：<br><br>日期：</div>
    </div>
  </main>
</body>
</html>"""
    return Response(document, mimetype="text/html")
