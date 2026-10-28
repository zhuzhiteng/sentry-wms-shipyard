"""Current schema coverage for photo-based label OCR."""

import os

import psycopg2


def _connection():
    connection = psycopg2.connect(os.environ["DATABASE_URL"])
    connection.autocommit = False
    return connection


def test_ocr_recognition_table_and_evidence_columns_exist():
    connection = _connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT column_name, data_type, is_nullable
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'shipyard_pallet_units'
               AND column_name = 'label_fields'
             ORDER BY column_name
            """
        )
        assert cursor.fetchall() == [("label_fields", "jsonb", "NO")]

        cursor.execute(
            """
            SELECT column_name, data_type
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'shipyard_label_recognitions'
            """
        )
        columns = dict(cursor.fetchall())
        assert columns["photo_id"] == "uuid"
        assert columns["ticket_id"] == "uuid"
        assert columns["task_id"] == "uuid"
        assert columns["fields"] == "jsonb"
        assert columns["status"] == "character varying"

        cursor.execute(
            """
            SELECT data_type
              FROM information_schema.columns
             WHERE table_schema = 'public'
               AND table_name = 'shipyard_outbound_confirmation_pallets'
               AND column_name = 'label_recognition_id'
            """
        )
        assert cursor.fetchone() == ("uuid",)
    finally:
        connection.rollback()
        connection.close()
