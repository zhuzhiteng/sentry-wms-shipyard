-- Read-only production schema gate for the shipyard WMS.
-- Run with ON_ERROR_STOP=1 after migrations and before releasing the API.

\set ON_ERROR_STOP on

DO $$
DECLARE
    missing_tables text[];
BEGIN
    SELECT array_agg(required.name ORDER BY required.name)
      INTO missing_tables
      FROM (
          VALUES
              ('shipyard_project_bindings'),
              ('shipyard_zone_ship_mappings'),
              ('shipyard_inbound_manifests'),
              ('shipyard_manifest_files'),
              ('shipyard_inbound_lines'),
              ('shipyard_cargo_tickets'),
              ('shipyard_photos'),
              ('shipyard_label_recognitions'),
              ('shipyard_receipts'),
              ('shipyard_receipt_pallets'),
              ('shipyard_pallet_units'),
              ('shipyard_stock_ledger'),
              ('shipyard_outbound_manifests'),
              ('shipyard_outbound_manifest_files'),
              ('shipyard_outbound_manifest_lines'),
              ('shipyard_outbound_orders'),
              ('shipyard_outbound_order_lines'),
              ('shipyard_outbound_tasks'),
              ('shipyard_outbound_confirmations'),
              ('shipyard_outbound_confirmation_pallets'),
              ('shipyard_outbound_loading_confirmations'),
              ('shipyard_pickup_requests'),
              ('shipyard_pickup_request_files'),
              ('shipyard_pickup_request_lines'),
              ('shipyard_arrival_slips')
      ) AS required(name)
     WHERE to_regclass('public.' || required.name) IS NULL;

    IF missing_tables IS NOT NULL THEN
        RAISE EXCEPTION 'missing shipyard tables: %', missing_tables;
    END IF;
END $$;

DO $$
DECLARE
    missing_columns text[];
BEGIN
    SELECT array_agg(
               required.table_name || '.' || required.column_name
               ORDER BY required.table_name, required.column_name
           )
      INTO missing_columns
      FROM (
          VALUES
              ('shipyard_cargo_tickets', 'delivery_status'),
              ('shipyard_cargo_tickets', 'deferred_to_date'),
              ('shipyard_cargo_tickets', 'deferred_reason'),
              ('shipyard_cargo_tickets', 'deferred_by'),
              ('shipyard_cargo_tickets', 'deferred_at'),
              ('shipyard_pickup_request_lines', 'source_availability_status'),
              ('shipyard_pickup_request_lines', 'match_status'),
              ('shipyard_pickup_request_lines', 'available_quantity'),
              ('shipyard_arrival_slips', 'warehouse_copy_retained'),
              ('shipyard_arrival_slips', 'file_sha256')
      ) AS required(table_name, column_name)
     WHERE NOT EXISTS (
           SELECT 1
             FROM information_schema.columns c
            WHERE c.table_schema = 'public'
              AND c.table_name = required.table_name
              AND c.column_name = required.column_name
     );

    IF missing_columns IS NOT NULL THEN
        RAISE EXCEPTION 'missing shipyard columns: %', missing_columns;
    END IF;
END $$;

DO $$
DECLARE
    missing_indexes text[];
BEGIN
    SELECT array_agg(required.name ORDER BY required.name)
      INTO missing_indexes
      FROM (
          VALUES
              ('uq_shipyard_pickup_request_file'),
              ('ix_shipyard_pickup_request_status'),
              ('uq_shipyard_pickup_request_file_hash'),
              ('ix_shipyard_pickup_request_line_ticket'),
              ('ix_shipyard_pickup_request_line_match'),
              ('ix_shipyard_pickup_request_line_outbound'),
              ('uq_shipyard_arrival_slip_photo'),
              ('ix_shipyard_arrival_slip_delivery')
      ) AS required(name)
     WHERE NOT EXISTS (
           SELECT 1
             FROM pg_indexes i
            WHERE i.schemaname = 'public'
              AND i.indexname = required.name
     );

    IF missing_indexes IS NOT NULL THEN
        RAISE EXCEPTION 'missing shipyard indexes: %', missing_indexes;
    END IF;
END $$;

DO $$
DECLARE
    legacy_objects text[];
BEGIN
    SELECT array_agg(name ORDER BY name)
      INTO legacy_objects
      FROM (
          SELECT table_name AS name
            FROM information_schema.tables
           WHERE table_schema = 'public'
             AND table_name IN (
                 'shipyard_delivery_lines',
                 'shipyard_delivery_notes',
                 'shipyard_dispatch_lines',
                 'shipyard_dispatch_orders',
                 'shipyard_distribution_lines',
                 'shipyard_distribution_files',
                 'shipyard_distribution_imports'
             )
          UNION ALL
          SELECT table_name || '.' || column_name
            FROM information_schema.columns
           WHERE table_schema = 'public'
             AND column_name IN ('qr_payload', 'qr_payload_sha256')
      ) legacy;

    IF legacy_objects IS NOT NULL THEN
        RAISE EXCEPTION 'legacy QR/distribution objects remain: %', legacy_objects;
    END IF;
END $$;

DO $$
DECLARE
    invalid_audit_at timestamptz;
BEGIN
    IF to_regprocedure('public.verify_audit_log_chain()') IS NULL THEN
        RAISE EXCEPTION 'verify_audit_log_chain() is missing';
    END IF;
    SELECT public.verify_audit_log_chain() INTO invalid_audit_at;
    IF invalid_audit_at IS NOT NULL THEN
        RAISE EXCEPTION 'audit hash chain is broken at %', invalid_audit_at;
    END IF;
END $$;

SELECT 'shipyard_go_live_schema' AS check_name,
       'ok' AS status,
       (
           SELECT count(*)
             FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name LIKE 'shipyard_%'
       ) AS shipyard_table_count;
