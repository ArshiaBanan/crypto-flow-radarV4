from __future__ import annotations

import sqlite3

from app.storage.factory import build_durable_store
from app.storage.postgres_v31 import SCHEMA_VERSION, DDL
from app.migrate_v31 import _hash_rows


def test_v31_sqlite_backend_factory(tmp_path):
    class S:
        storage_backend = "sqlite"
        sqlite_path = str(tmp_path / "radar.db")
    store = build_durable_store(S())
    assert store.__class__.__name__ == "SQLiteStore"
    store.close()


def test_v31_factory_rejects_unknown_backend():
    class S:
        storage_backend = "unknown"
        sqlite_path = "data/test.db"
    try:
        build_durable_store(S())
    except ValueError as exc:
        assert "Unsupported storage backend" in str(exc)
    else:
        raise AssertionError("unknown backend must fail closed")


def test_v31_postgres_backend_fails_closed_without_driver():
    class S:
        storage_backend = "postgres"
        sqlite_path = "data/radar.db"
        postgres_url = "postgresql://example.invalid/db"
    try:
        build_durable_store(S())
    except RuntimeError as exc:
        assert "psycopg" in str(exc)
    except Exception as exc:
        # Environment without psycopg should fail at the driver boundary, not silently use SQLite.
        assert "psycopg" in str(exc).lower()
    else:
        raise AssertionError("postgres backend must not silently downgrade to sqlite")


def test_v31_schema_marker_and_core_tables_present():
    for name in ("raw_events", "oi_samples", "bars_1m", "funding_samples", "feature_snapshots", "signal_episodes", "cfr_storage_meta_v31"):
        assert f"CREATE TABLE IF NOT EXISTS {name}" in DDL
    assert SCHEMA_VERSION == "storage_v31"


def test_v31_row_hash_is_order_sensitive_and_deterministic():
    rows = [(1, "a"), (2, "b")]
    assert _hash_rows(rows) == _hash_rows(list(rows))
    assert _hash_rows(rows) != _hash_rows(list(reversed(rows)))


def test_v31_readonly_sqlite_source_can_be_opened_for_migration(tmp_path):
    db = tmp_path / "source.db"
    conn = sqlite3.connect(db)
    conn.execute("create table sample(x integer)")
    conn.execute("insert into sample values(1)")
    conn.commit()
    conn.execute("PRAGMA query_only=ON")
    assert conn.execute("select count(*) from sample").fetchone()[0] == 1
    conn.close()
