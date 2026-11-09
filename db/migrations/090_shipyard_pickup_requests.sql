-- 090: daily pickup demand, special dispatch orders, retained shortages,
-- deferred inbound batches, and four-part arrival-slip evidence.

SET lock_timeout = '5s';
SET statement_timeout = '120s';

BEGIN;

CREATE TABLE IF NOT EXISTS shipyard_pickup_requests (
    request_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    warehouse_id            INT NOT NULL REFERENCES warehouses(warehouse_id),
    request_no               VARCHAR(128) NOT NULL,
    source_type              VARCHAR(24) NOT NULL,
    request_date             DATE,
    dispatch_no              VARCHAR(128),
    source_file_name         VARCHAR(255),
    source_file_sha256       CHAR(64),
    source_template_version  VARCHAR(64),
    status                   VARCHAR(16) NOT NULL DEFAULT 'PARSED',
    metadata                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_by               VARCHAR(100) NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_pickup_request_no
        UNIQUE (warehouse_id, request_no),
    CONSTRAINT shipyard_pickup_request_source_type
        CHECK (source_type IN (
            'DAILY_DEMAND', 'ON_DEMAND', 'SPECIAL_DISPATCH'
        )),
    CONSTRAINT shipyard_pickup_request_status
        CHECK (status IN ('PARSED', 'CONFIRMED', 'VOID')),
    CONSTRAINT shipyard_pickup_request_sha256
        CHECK (
            source_file_sha256 IS NULL
            OR length(source_file_sha256) = 64
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_shipyard_pickup_request_file
    ON shipyard_pickup_requests (warehouse_id, source_file_sha256)
    WHERE source_file_sha256 IS NOT NULL
      AND status <> 'VOID';

CREATE INDEX IF NOT EXISTS ix_shipyard_pickup_request_status
    ON shipyard_pickup_requests (
        warehouse_id, status, request_date DESC, created_at DESC
    );

CREATE TABLE IF NOT EXISTS shipyard_pickup_request_files (
    file_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id       UUID NOT NULL
                         REFERENCES shipyard_pickup_requests(request_id)
                         ON DELETE CASCADE,
    file_kind        VARCHAR(16) NOT NULL,
    file_name        VARCHAR(255) NOT NULL,
    content_type     VARCHAR(128) NOT NULL,
    file_size        BIGINT NOT NULL,
    file_sha256      CHAR(64) NOT NULL,
    file_bytes       BYTEA NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT shipyard_pickup_request_file_kind
        CHECK (file_kind IN ('SOURCE', 'EVIDENCE')),
    CONSTRAINT shipyard_pickup_request_file_size
        CHECK (file_size > 0 AND file_size <= 10485760),
    CONSTRAINT shipyard_pickup_request_file_sha256
        CHECK (length(file_sha256) = 64)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_shipyard_pickup_request_file_hash
    ON shipyard_pickup_request_files (request_id, file_kind, file_sha256);

CREATE TABLE IF NOT EXISTS shipyard_pickup_request_lines (
    line_id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id               UUID NOT NULL
                                 REFERENCES shipyard_pickup_requests(request_id)
                                 ON DELETE CASCADE,
    source_sheet             VARCHAR(128) NOT NULL,
    source_row_no            VARCHAR(32) NOT NULL,
    source_row_hash          CHAR(64) NOT NULL,
    source_kind              VARCHAR(24) NOT NULL,
    source_availability_status VARCHAR(16) NOT NULL DEFAULT 'UNKNOWN',
    request_date             DATE,
    engineering_no           VARCHAR(64) NOT NULL,
    section_no               VARCHAR(64),
    pallet_no                VARCHAR(128) NOT NULL,
    pallet_name              VARCHAR(255) NOT NULL,
    delivery_status_raw      VARCHAR(128),
    prewarehouse_status_raw  VARCHAR(128),
    pickup_status_raw        VARCHAR(128),
    supplier_name            VARCHAR(200),
    work_order_status_raw    VARCHAR(128),
    applicant_raw            VARCHAR(255),
    department               VARCHAR(255),
    delivery_address         VARCHAR(255),
    contact_name             VARCHAR(100),
    contact_phone            VARCHAR(50),
    demand_window_raw        VARCHAR(255),
    demand_start_at          TIMESTAMPTZ,
    demand_end_at            TIMESTAMPTZ,
    material_code            VARCHAR(128),
    material_name            VARCHAR(255),
    item_spec                TEXT,
    demand_quantity          NUMERIC(18,3),
    demand_unit              VARCHAR(32) NOT NULL DEFAULT '件',
    expected_arrival_date    DATE,
    keeper_name              VARCHAR(100),
    keeper_phone             VARCHAR(50),
    dispatch_no              VARCHAR(128),
    remarks                  VARCHAR(500),
    resource_1               VARCHAR(100),
    driver_1                 VARCHAR(100),
    resource_2               VARCHAR(100),
    driver_2                 VARCHAR(100),
    resource_3               VARCHAR(100),
    driver_3                 VARCHAR(100),
    confirmer                VARCHAR(100),
    completed_at_raw         VARCHAR(100),
    completed_at             TIMESTAMPTZ,
    confirmation_status_raw  VARCHAR(128),
    match_status             VARCHAR(24) NOT NULL DEFAULT 'PENDING',
    available_quantity       NUMERIC(18,3) NOT NULL DEFAULT 0,
    ticket_id                UUID
                                 REFERENCES shipyard_cargo_tickets(ticket_id)
                                 ON DELETE SET NULL,
    outbound_order_id        UUID
                                 REFERENCES shipyard_outbound_orders(outbound_order_id)
                                 ON DELETE SET NULL,
    outbound_task_id         UUID
                                 REFERENCES shipyard_outbound_tasks(task_id)
                                 ON DELETE SET NULL,
    match_details            JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_data                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_shipyard_pickup_request_line
        UNIQUE (request_id, source_sheet, source_row_no),
    CONSTRAINT shipyard_pickup_request_line_hash
        CHECK (length(source_row_hash) = 64),
    CONSTRAINT shipyard_pickup_request_line_source_kind
        CHECK (source_kind IN (
            'DAILY_DEMAND', 'ON_DEMAND', 'SPECIAL_DISPATCH'
        )),
    CONSTRAINT shipyard_pickup_request_line_quantity
        CHECK (demand_quantity IS NULL OR demand_quantity > 0),
    CONSTRAINT shipyard_pickup_request_line_available
        CHECK (available_quantity >= 0),
    CONSTRAINT shipyard_pickup_request_line_status
        CHECK (match_status IN (
            'PENDING',
            'AVAILABLE',
            'AMBIGUOUS',
            'SOURCE_UNAVAILABLE',
            'WAREHOUSE_MISSING',
            'QUANTITY_MISMATCH',
            'OUTBOUND_PENDING',
            'OUTBOUND',
            'DUPLICATE_DEMAND',
            'VOID'
        ))
);

ALTER TABLE shipyard_pickup_request_lines
    ADD COLUMN IF NOT EXISTS source_availability_status VARCHAR(16)
        NOT NULL DEFAULT 'UNKNOWN';

ALTER TABLE shipyard_pickup_request_lines
    DROP CONSTRAINT IF EXISTS shipyard_pickup_request_line_source_availability;

ALTER TABLE shipyard_pickup_request_lines
    ADD CONSTRAINT shipyard_pickup_request_line_source_availability
    CHECK (source_availability_status IN (
        'AVAILABLE', 'UNAVAILABLE', 'UNKNOWN'
    ));

ALTER TABLE shipyard_pickup_request_lines
    DROP CONSTRAINT IF EXISTS shipyard_pickup_request_line_status;

ALTER TABLE shipyard_pickup_request_lines
    ADD CONSTRAINT shipyard_pickup_request_line_status
    CHECK (match_status IN (
        'PENDING',
        'AVAILABLE',
        'AMBIGUOUS',
        'SOURCE_UNAVAILABLE',
        'WAREHOUSE_MISSING',
        'QUANTITY_MISMATCH',
        'OUTBOUND_PENDING',
        'OUTBOUND',
        'DUPLICATE_DEMAND',
        'VOID'
    ));

CREATE INDEX IF NOT EXISTS ix_shipyard_pickup_request_line_ticket
    ON shipyard_pickup_request_lines (ticket_id)
    WHERE ticket_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_shipyard_pickup_request_line_match
    ON shipyard_pickup_request_lines (
        engineering_no, pallet_no, match_status
    );

CREATE INDEX IF NOT EXISTS ix_shipyard_pickup_request_line_outbound
    ON shipyard_pickup_request_lines (outbound_task_id)
    WHERE outbound_task_id IS NOT NULL;

ALTER TABLE shipyard_cargo_tickets
    ADD COLUMN IF NOT EXISTS delivery_status VARCHAR(24)
        NOT NULL DEFAULT 'PENDING',
    ADD COLUMN IF NOT EXISTS deferred_to_date DATE,
    ADD COLUMN IF NOT EXISTS deferred_reason VARCHAR(500),
    ADD COLUMN IF NOT EXISTS deferred_by VARCHAR(100),
    ADD COLUMN IF NOT EXISTS deferred_at TIMESTAMPTZ;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conname = 'shipyard_cargo_ticket_delivery_status'
    ) THEN
        ALTER TABLE shipyard_cargo_tickets
            ADD CONSTRAINT shipyard_cargo_ticket_delivery_status
            CHECK (delivery_status IN (
                'PENDING', 'DEFERRED', 'PARTIAL', 'RECEIVED'
            ));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS shipyard_arrival_slips (
    arrival_slip_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    warehouse_id             INT NOT NULL REFERENCES warehouses(warehouse_id),
    manifest_id              UUID NOT NULL
                                 REFERENCES shipyard_inbound_manifests(manifest_id)
                                 ON DELETE RESTRICT,
    slip_no                  VARCHAR(128) NOT NULL,
    delivery_note_no         VARCHAR(128) NOT NULL,
    supplier_name            VARCHAR(200) NOT NULL,
    engineering_no           VARCHAR(64),
    purchase_order_no        VARCHAR(128),
    is_urgent                BOOLEAN NOT NULL DEFAULT FALSE,
    factory_copy_count       SMALLINT NOT NULL DEFAULT 2,
    shipyard_copy_count      SMALLINT NOT NULL DEFAULT 2,
    warehouse_copy_retained  BOOLEAN NOT NULL DEFAULT TRUE,
    file_name                VARCHAR(255) NOT NULL,
    content_type             VARCHAR(64) NOT NULL,
    file_size                BIGINT NOT NULL,
    file_sha256              CHAR(64) NOT NULL,
    file_bytes               BYTEA NOT NULL,
    received_by              VARCHAR(100) NOT NULL,
    received_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT shipyard_arrival_slip_copy_counts
        CHECK (
            factory_copy_count BETWEEN 0 AND 10
            AND shipyard_copy_count BETWEEN 0 AND 10
        ),
    CONSTRAINT shipyard_arrival_slip_content_type
        CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp')),
    CONSTRAINT shipyard_arrival_slip_size
        CHECK (file_size > 0 AND file_size <= 10485760),
    CONSTRAINT shipyard_arrival_slip_sha256
        CHECK (length(file_sha256) = 64)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_shipyard_arrival_slip_photo
    ON shipyard_arrival_slips (manifest_id, file_sha256);

CREATE INDEX IF NOT EXISTS ix_shipyard_arrival_slip_delivery
    ON shipyard_arrival_slips (
        warehouse_id, delivery_note_no, received_at DESC
    );

COMMIT;
