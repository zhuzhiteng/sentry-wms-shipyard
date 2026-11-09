"""Inbound manifest drag-in parsing, review and confirmation."""

from __future__ import annotations

from io import BytesIO

from flask import g, jsonify, request, send_file

from middleware.auth_middleware import (
    check_warehouse_access,
    require_admin_or_page_permission,
    require_auth,
)
from middleware.db import with_db
from schemas.shipyard import (
    ConfirmInboundManifestRequest,
    UpdateArrivalSlipRequest,
    VoidInboundManifestRequest,
)
from services.shipyard.inbound_document_service import (
    get_arrival_slip_file,
    get_arrival_slip_warehouse_id,
    recognize_arrival_slip_fields,
    record_arrival_slip,
    update_arrival_slip,
)
from services.shipyard.manifest_parser import ManifestParseError
from services.shipyard.manifest_service import (
    confirm_manifest,
    create_manifest_preview,
    get_manifest_file,
    get_manifest_payload,
    list_manifests,
    void_manifest,
)
from utils.uploads import (
    UnsupportedImageError,
    UploadTooLargeError,
    detect_image_content_type,
    read_upload_bytes,
    safe_upload_name,
)
from utils.validation import validate_body

from . import require_shipyard_roles, shipyard_bp


def _validated_image_upload(uploaded) -> tuple[bytes, str]:
    content = read_upload_bytes(uploaded)
    return content, detect_image_content_type(content)


MANIFEST_EDIT_ROLES = ("SHIPYARD_MANAGER",)

XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@shipyard_bp.get("/inbound/manifests")
@require_auth
@require_admin_or_page_permission("inbound")
@with_db
def get_inbound_manifests():
    try:
        warehouse_id = int(request.args.get("warehouse_id", ""))
        limit = int(request.args.get("limit", "50"))
    except (TypeError, ValueError):
        return jsonify({"error": "warehouse_id and limit must be integers"}), 400
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    return jsonify(list_manifests(g.db, warehouse_id=warehouse_id, limit=limit))


@shipyard_bp.post("/inbound/manifests/preview")
@require_auth
@require_shipyard_roles(*MANIFEST_EDIT_ROLES)
@require_admin_or_page_permission("inbound")
@with_db
def preview_inbound_manifest():
    if not request.mimetype.startswith("multipart/form-data"):
        return jsonify(
            {
                "error": "unsupported_media_type",
                "message": "请使用 multipart/form-data 上传 Excel 清单",
            }
        ), 415

    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "file_required"}), 400
    source_file_name = safe_upload_name(
        uploaded.filename,
        fallback="manifest.xlsx",
    )
    if not source_file_name.lower().endswith(".xlsx"):
        return jsonify(
            {
                "error": "unsupported_file_type",
                "message": "首期仅接受 .xlsx 发货清单",
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
        payload = create_manifest_preview(
            g.db,
            warehouse_id=warehouse_id,
            manifest_no=request.form.get("manifest_no"),
            source_file_name=source_file_name,
            content_type=XLSX_CONTENT_TYPE,
            content=content,
            manufacturer_name=request.form.get("manufacturer_name"),
            created_by=g.current_user["username"],
        )
    except ManifestParseError as exc:
        return jsonify(
            {
                "error": "manifest_parse_failed",
                "message": str(exc),
                "diagnostics": exc.diagnostics,
            }
        ), 422
    return jsonify(payload), 201


@shipyard_bp.get("/inbound/manifests/<uuid:manifest_id>")
@require_auth
@require_admin_or_page_permission("inbound")
@with_db
def get_inbound_manifest(manifest_id):
    payload = get_manifest_payload(g.db, str(manifest_id))
    ok, denied = check_warehouse_access(payload["manifest"]["warehouse_id"])
    if not ok:
        return denied
    return jsonify(payload)


@shipyard_bp.get("/inbound/manifests/<uuid:manifest_id>/file")
@require_auth
@require_admin_or_page_permission("inbound")
@with_db
def download_inbound_manifest_file(manifest_id):
    manifest_file = get_manifest_file(g.db, manifest_id=str(manifest_id))
    ok, denied = check_warehouse_access(manifest_file["warehouse_id"])
    if not ok:
        return denied
    return send_file(
        BytesIO(manifest_file["file_bytes"]),
        mimetype=manifest_file["content_type"],
        as_attachment=True,
        download_name=manifest_file["file_name"],
        max_age=0,
    )


@shipyard_bp.post("/inbound/manifests/<uuid:manifest_id>/confirm")
@require_auth
@require_shipyard_roles(*MANIFEST_EDIT_ROLES)
@require_admin_or_page_permission("inbound")
@validate_body(ConfirmInboundManifestRequest)
@with_db
def confirm_inbound_manifest(manifest_id, validated):
    del validated
    current = get_manifest_payload(g.db, str(manifest_id))
    ok, denied = check_warehouse_access(current["manifest"]["warehouse_id"])
    if not ok:
        return denied
    payload = confirm_manifest(
        g.db,
        manifest_id=str(manifest_id),
        confirmed_by=g.current_user["username"],
    )
    return jsonify(payload)


@shipyard_bp.post("/inbound/manifests/<uuid:manifest_id>/void")
@require_auth
@require_shipyard_roles(*MANIFEST_EDIT_ROLES)
@require_admin_or_page_permission("inbound")
@validate_body(VoidInboundManifestRequest)
@with_db
def void_inbound_manifest(manifest_id, validated):
    current = get_manifest_payload(g.db, str(manifest_id))
    ok, denied = check_warehouse_access(current["manifest"]["warehouse_id"])
    if not ok:
        return denied
    payload = void_manifest(
        g.db,
        manifest_id=str(manifest_id),
        reason=validated.reason,
        voided_by=g.current_user["username"],
    )
    return jsonify(payload)


@shipyard_bp.post("/inbound/manifests/<uuid:manifest_id>/arrival-slips")
@require_auth
@require_shipyard_roles("SHIPYARD_MANAGER", "RECEIVER")
@require_admin_or_page_permission("inbound")
@with_db
def upload_arrival_slip(manifest_id):
    if not request.mimetype.startswith("multipart/form-data"):
        return jsonify({"error": "unsupported_media_type"}), 415
    payload = get_manifest_payload(g.db, str(manifest_id))
    ok, denied = check_warehouse_access(payload["manifest"]["warehouse_id"])
    if not ok:
        return denied
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "file_required"}), 400
    try:
        content, content_type = _validated_image_upload(uploaded)
    except UploadTooLargeError as exc:
        return jsonify({"error": "file_too_large", "message": str(exc)}), 413
    except UnsupportedImageError as exc:
        return jsonify({"error": "unsupported_file_type", "message": str(exc)}), 415
    slip_no = (request.form.get("slip_no") or "").strip()
    delivery_note_no = (request.form.get("delivery_note_no") or "").strip()
    supplier_name = (request.form.get("supplier_name") or "").strip()
    if not slip_no or not delivery_note_no or not supplier_name:
        return jsonify(
            {
                "error": "required_fields_missing",
                "message": "验收单号、送货单号和供应商为必填项",
            }
        ), 400
    try:
        factory_copy_count = int(request.form.get("factory_copy_count", "2"))
        shipyard_copy_count = int(request.form.get("shipyard_copy_count", "2"))
    except ValueError:
        return jsonify({"error": "copy_count_must_be_integer"}), 400
    warehouse_copy_retained = request.form.get(
        "warehouse_copy_retained", "true"
    ).lower() not in {"0", "false", "no"}
    is_urgent = request.form.get("is_urgent", "false").lower() in {"1", "true", "yes"}
    return jsonify(
        record_arrival_slip(
            g.db,
            manifest_id=str(manifest_id),
            slip_no=slip_no,
            delivery_note_no=delivery_note_no,
            supplier_name=supplier_name,
            engineering_no=(request.form.get("engineering_no") or "").strip() or None,
            purchase_order_no=(request.form.get("purchase_order_no") or "").strip()
            or None,
            is_urgent=is_urgent,
            factory_copy_count=factory_copy_count,
            shipyard_copy_count=shipyard_copy_count,
            warehouse_copy_retained=warehouse_copy_retained,
            file_name=safe_upload_name(
                uploaded.filename,
                fallback="arrival-slip",
            ),
            content_type=content_type,
            content=content,
            received_by=g.current_user["username"],
        )
    ), 201


@shipyard_bp.post("/inbound/manifests/<uuid:manifest_id>/arrival-slips/recognize")
@require_auth
@require_shipyard_roles("SHIPYARD_MANAGER", "RECEIVER")
@require_admin_or_page_permission("inbound")
@with_db
def recognize_arrival_slip(manifest_id):
    if not request.mimetype.startswith("multipart/form-data"):
        return jsonify({"error": "unsupported_media_type"}), 415
    payload = get_manifest_payload(g.db, str(manifest_id))
    ok, denied = check_warehouse_access(payload["manifest"]["warehouse_id"])
    if not ok:
        return denied
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "file_required"}), 400
    try:
        content, _content_type = _validated_image_upload(uploaded)
    except UploadTooLargeError as exc:
        return jsonify({"error": "file_too_large", "message": str(exc)}), 413
    except UnsupportedImageError as exc:
        return jsonify({"error": "unsupported_file_type", "message": str(exc)}), 415
    return jsonify(recognize_arrival_slip_fields(content))


@shipyard_bp.put("/inbound/arrival-slips/<uuid:arrival_slip_id>")
@require_auth
@require_shipyard_roles("SHIPYARD_MANAGER", "RECEIVER")
@require_admin_or_page_permission("inbound")
@validate_body(UpdateArrivalSlipRequest)
@with_db
def update_saved_arrival_slip(arrival_slip_id, validated):
    warehouse_id = get_arrival_slip_warehouse_id(
        g.db,
        str(arrival_slip_id),
    )
    ok, denied = check_warehouse_access(warehouse_id)
    if not ok:
        return denied
    payload = update_arrival_slip(
        g.db,
        arrival_slip_id=str(arrival_slip_id),
        slip_no=validated.slip_no,
        delivery_note_no=validated.delivery_note_no,
        supplier_name=validated.supplier_name,
        engineering_no=validated.engineering_no,
        purchase_order_no=validated.purchase_order_no,
        updated_by=g.current_user["username"],
    )
    return jsonify(payload)


@shipyard_bp.get("/inbound/arrival-slips/<uuid:arrival_slip_id>/file")
@require_auth
@require_admin_or_page_permission("inbound")
@with_db
def download_arrival_slip(arrival_slip_id):
    payload = get_arrival_slip_file(g.db, str(arrival_slip_id))
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
