"""Migration 090 adds pickup requests, deferred batches and arrival slips."""

import os
from pathlib import Path

import psycopg2

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations"
    / "090_shipyard_pickup_requests.sql"
)


def _apply_migration(connection):
    cursor = connection.cursor()
    cursor.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
    cursor.close()


def test_pickup_request_migration_is_idempotent():
    connection = psycopg2.connect(os.environ["DATABASE_URL"])
    connection.autocommit = True
    try:
        _apply_migration(connection)
        _apply_migration(connection)
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name = ANY(%s)
             ORDER BY table_name
            """,
            (
                [
                    "shipyard_arrival_slips",
                    "shipyard_pickup_request_files",
                    "shipyard_pickup_request_lines",
                    "shipyard_pickup_requests",
                ],
            ),
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "shipyard_arrival_slips",
            "shipyard_pickup_request_files",
            "shipyard_pickup_request_lines",
            "shipyard_pickup_requests",
        ]
        cursor.execute(
            """
            SELECT column_name
              FROM information_schema.columns
             WHERE table_name = 'shipyard_cargo_tickets'
               AND column_name = ANY(%s)
             ORDER BY column_name
            """,
            (
                [
                    "deferred_at",
                    "deferred_by",
                    "deferred_reason",
                    "deferred_to_date",
                    "delivery_status",
                ],
            ),
        )
        assert [row[0] for row in cursor.fetchall()] == [
            "deferred_at",
            "deferred_by",
            "deferred_reason",
            "deferred_to_date",
            "delivery_status",
        ]
        cursor.execute(
            """
            SELECT is_nullable, column_default
              FROM information_schema.columns
             WHERE table_name = 'shipyard_pickup_request_lines'
               AND column_name = 'source_availability_status'
            """
        )
        source_availability = cursor.fetchone()
        assert source_availability[0] == "NO"
        assert "'UNKNOWN'" in source_availability[1]
        cursor.close()
    finally:
        connection.close()
