"""Migration 089 removes the obsolete distribution and dispatch tables."""

import os
from pathlib import Path

import psycopg2

MIGRATION_PATH = (
    Path(__file__).resolve().parents[2]
    / "db"
    / "migrations"
    / "089_shipyard_remove_distribution_dispatch.sql"
)

LEGACY_TABLES = {
    "shipyard_delivery_lines",
    "shipyard_delivery_notes",
    "shipyard_dispatch_lines",
    "shipyard_dispatch_orders",
    "shipyard_distribution_lines",
    "shipyard_distribution_files",
    "shipyard_distribution_imports",
}


def _apply_migration(connection):
    connection.cursor().execute(MIGRATION_PATH.read_text(encoding="utf-8"))


def test_legacy_distribution_tables_are_removed_and_migration_is_idempotent():
    connection = psycopg2.connect(os.environ["DATABASE_URL"])
    connection.autocommit = True
    try:
        cursor = connection.cursor()
        for table_name in LEGACY_TABLES:
            cursor.execute(f"CREATE TABLE IF NOT EXISTS {table_name} (id INT)")
        _apply_migration(connection)
        _apply_migration(connection)

        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT table_name
              FROM information_schema.tables
             WHERE table_schema = 'public'
               AND table_name = ANY(%s)
            """,
            (sorted(LEGACY_TABLES),),
        )
        assert cursor.fetchall() == []
    finally:
        connection.close()
