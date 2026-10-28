"""Schema tests for migration 083 (shipyard temporal and import integrity)."""

import hashlib
import os
import uuid

import psycopg2


def _make_conn():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    return conn


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def test_temporal_constraints_and_file_table_exist():
    conn = _make_conn()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT conname
              FROM pg_constraint
             WHERE conname = ANY(%s)
            """,
            (
                [
                    "shipyard_project_binding_no_overlap",
                    "shipyard_zone_ship_primary_no_overlap",
                    "shipyard_zone_ship_bin_no_overlap",
                ],
            ),
        )
        constraints = {row[0] for row in cur.fetchall()}
        assert constraints == {
            "shipyard_project_binding_no_overlap",
            "shipyard_zone_ship_primary_no_overlap",
            "shipyard_zone_ship_bin_no_overlap",
        }

        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM information_schema.tables
                 WHERE table_schema = 'public'
                   AND table_name = 'shipyard_manifest_files'
            )
            """
        )
        assert cur.fetchone()[0] is True
    finally:
        conn.rollback()
        conn.close()


def test_project_binding_windows_must_not_overlap():
    conn = _make_conn()
    try:
        ship_no = f"H83-{uuid.uuid4().hex[:8]}"
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO shipyard_project_bindings
                (ship_no, engineering_no, production_drawing_no,
                 manufacturer_name, effective_from, effective_to)
            VALUES
                (%s, 'E-1', 'D-1', 'Factory',
                 '2030-01-01T00:00:00Z', '2030-02-01T00:00:00Z'),
                (%s, 'E-1', 'D-1', 'Factory',
                 '2030-02-01T00:00:00Z', '2030-03-01T00:00:00Z')
            """,
            (ship_no, ship_no),
        )

        try:
            cur.execute(
                """
                INSERT INTO shipyard_project_bindings
                    (ship_no, engineering_no, production_drawing_no,
                     manufacturer_name, effective_from, effective_to)
                VALUES
                    (%s, 'E-1', 'D-1', 'Factory',
                     '2030-01-15T00:00:00Z', '2030-02-15T00:00:00Z')
                """,
                (ship_no,),
            )
            assert False, "overlapping binding window should be rejected"
        except psycopg2.errors.ExclusionViolation:
            conn.rollback()
            assert True
    finally:
        conn.close()


def test_primary_zone_can_be_scheduled_in_adjacent_windows():
    conn = _make_conn()
    try:
        ship_no = f"H83-Z-{uuid.uuid4().hex[:8]}"
        cur = conn.cursor()
        cur.execute("SELECT bin_id FROM bins ORDER BY bin_id LIMIT 2")
        first_bin, second_bin = [row[0] for row in cur.fetchall()]
        cur.execute(
            """
            INSERT INTO shipyard_zone_ship_mappings
                (bin_id, ship_no, is_primary, effective_from, effective_to)
            VALUES
                (%s, %s, TRUE,
                 '2030-01-01T00:00:00Z', '2030-02-01T00:00:00Z'),
                (%s, %s, TRUE,
                 '2030-02-01T00:00:00Z', '2030-03-01T00:00:00Z')
            """,
            (first_bin, ship_no, second_bin, ship_no),
        )
        cur.execute(
            """
            SELECT COUNT(*)
              FROM shipyard_zone_ship_mappings
             WHERE ship_no = %s
            """,
            (ship_no,),
        )
        assert cur.fetchone()[0] == 2
    finally:
        conn.rollback()
        conn.close()


def test_voided_manifest_file_hash_can_be_imported_again():
    conn = _make_conn()
    try:
        file_hash = _sha(f"FILE-{uuid.uuid4()}")
        first_manifest = uuid.uuid4()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO shipyard_inbound_manifests
                (manifest_id, warehouse_id, manifest_no,
                 source_file_sha256, created_by)
            VALUES (%s, 1, %s, %s, 'tester')
            """,
            (first_manifest, f"IN-V-{uuid.uuid4().hex[:8]}", file_hash),
        )
        cur.execute(
            """
            UPDATE shipyard_inbound_manifests
               SET status = 'VOID'
             WHERE manifest_id = %s
            """,
            (first_manifest,),
        )
        cur.execute(
            """
            INSERT INTO shipyard_inbound_manifests
                (warehouse_id, manifest_no, source_file_sha256, created_by)
            VALUES (1, %s, %s, 'tester')
            """,
            (f"IN-V2-{uuid.uuid4().hex[:8]}", file_hash),
        )
    finally:
        conn.rollback()
        conn.close()
