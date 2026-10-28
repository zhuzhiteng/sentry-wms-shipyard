"""Schema coverage for removing legacy raw QR storage."""

import os

import psycopg2

MIGRATION_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "db",
    "migrations",
    "088_shipyard_remove_legacy_qr.sql",
)


def test_legacy_qr_columns_are_absent_and_migration_is_idempotent():
    with open(MIGRATION_PATH, encoding="utf-8") as migration:
        sql = migration.read()
    connection = psycopg2.connect(os.environ["DATABASE_URL"])
    connection.autocommit = True
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            ALTER TABLE shipyard_pallet_units
                ADD COLUMN IF NOT EXISTS qr_payload TEXT,
                ADD COLUMN IF NOT EXISTS qr_payload_sha256 CHAR(64)
            """
        )
        cursor.execute(
            """
            ALTER TABLE shipyard_outbound_confirmation_pallets
                ADD COLUMN IF NOT EXISTS qr_payload TEXT,
                ADD COLUMN IF NOT EXISTS qr_payload_sha256 CHAR(64)
            """
        )
        cursor.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_shipyard_pallet_qr_payload_sha256
                ON shipyard_pallet_units (qr_payload_sha256)
            """
        )
        cursor.execute(sql)
        cursor.execute(sql)
        cursor.execute(
            """
            SELECT table_name, column_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND (
                    (
                        table_name = 'shipyard_pallet_units'
                        AND column_name IN ('qr_payload', 'qr_payload_sha256')
                    )
                    OR
                    (
                        table_name = 'shipyard_outbound_confirmation_pallets'
                        AND column_name IN ('qr_payload', 'qr_payload_sha256')
                    )
               )
            """
        )
        assert cursor.fetchall() == []
    finally:
        connection.close()
