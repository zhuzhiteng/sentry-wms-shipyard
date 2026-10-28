-- 088: remove legacy raw QR storage.
--
-- Shipyard labels are captured as photos and recognized through OCR. The raw
-- factory QR bytes and their hash are not part of the active workflow.

SET lock_timeout = '5s';
SET statement_timeout = '120s';

BEGIN;

DROP INDEX IF EXISTS uq_shipyard_pallet_qr_payload_sha256;

ALTER TABLE shipyard_pallet_units
    DROP COLUMN IF EXISTS qr_payload,
    DROP COLUMN IF EXISTS qr_payload_sha256;

ALTER TABLE shipyard_outbound_confirmation_pallets
    DROP COLUMN IF EXISTS qr_payload,
    DROP COLUMN IF EXISTS qr_payload_sha256;

COMMIT;
