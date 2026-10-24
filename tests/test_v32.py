from __future__ import annotations

import sqlite3

from app.storage_v32 import (
    DEFAULT_TABLES,
    DualWriteControllerV32,
    SQLiteOutboxV32,
    install_sqlite_outbox,
    row_hash,
)


def test_v32_outbox_captures_insert_update_delete(tmp_path):
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, name TEXT NOT NULL, value REAL)")
    install_sqlite_outbox(conn, ("demo",))

    conn.execute("INSERT INTO demo(id,name,value) VALUES(1,'alpha',1.0)")
    conn.execute("UPDATE demo SET value=2.5 WHERE id=1")
    conn.execute("DELETE FROM demo WHERE id=1")
    conn.commit()
    rows = conn.execute("SELECT op,row_key,row_json FROM cfr_outbox_v32 ORDER BY event_id").fetchall()
    conn.close()

    assert [r[0] for r in rows] == ["INSERT", "UPDATE", "DELETE"]
    assert '"id":1' in rows[0][1]
    assert '"value":2.5' in rows[1][2]
    assert '"name":"alpha"' in rows[2][2]


def test_v32_outbox_installs_only_existing_tables(tmp_path):
    db = tmp_path / "ledger.db"
    outbox = SQLiteOutboxV32(str(db), ("missing_table",))
    assert outbox.installed_tables == []
    assert outbox.pending_count() == 0
    outbox.close()


def test_v32_outbox_pending_hash_is_deterministic(tmp_path):
    db = tmp_path / "ledger.db"
    outbox = SQLiteOutboxV32(str(db), ())
    outbox.conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, x TEXT)")
    install_sqlite_outbox(outbox.conn, ("demo",))
    outbox.conn.execute("INSERT INTO demo VALUES(1,'x')")
    outbox.conn.commit()
    event = outbox.pending(1)[0]
    assert event["row_hash"] == row_hash(event["row_json"])
    outbox.close()


def test_v32_snapshot_is_idempotency_ready(tmp_path):
    db = tmp_path / "ledger.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, x TEXT)")
    conn.executemany("INSERT INTO demo VALUES(?,?)", [(1, "a"), (2, "b")])
    conn.commit()
    conn.close()
    outbox = SQLiteOutboxV32(str(db), ("demo",))
    count = outbox.snapshot_all()
    assert count == 2
    events = outbox.pending(10)
    assert len(events) == 2
    assert all(e["op"] == "SNAPSHOT" for e in events)
    outbox.close()


def test_v32_disabled_controller_is_ready(tmp_path):
    outbox = SQLiteOutboxV32(str(tmp_path / "db.sqlite"), ())
    controller = DualWriteControllerV32(outbox, None, "off")
    ready, reasons = controller.ready()
    assert ready is True
    assert reasons == []
    outbox.close()


def test_v32_required_without_postgres_is_not_ready(tmp_path):
    outbox = SQLiteOutboxV32(str(tmp_path / "db.sqlite"), ())
    controller = DualWriteControllerV32(outbox, None, "required")
    ready, reasons = controller.ready()
    assert ready is False
    assert "POSTGRES_LEDGER_UNREACHABLE" in reasons
    outbox.close()


def test_v32_shadow_without_postgres_does_not_drop_events(tmp_path):
    outbox = SQLiteOutboxV32(str(tmp_path / "db.sqlite"), ())
    outbox.conn.execute("CREATE TABLE demo (id INTEGER PRIMARY KEY, x TEXT)")
    install_sqlite_outbox(outbox.conn, ("demo",))
    outbox.conn.execute("INSERT INTO demo VALUES(1,'x')")
    outbox.conn.commit()
    controller = DualWriteControllerV32(outbox, None, "shadow")
    assert controller.pump_once(10) == 0
    assert outbox.pending_count() == 1
    assert controller.last_error == "POSTGRES_LEDGER_NOT_CONFIGURED"
    outbox.close()


def test_v32_table_inventory_is_stable():
    assert "paper_trades" in DEFAULT_TABLES
    assert "alert_events_v20" in DEFAULT_TABLES
    assert "consensus_v24" in DEFAULT_TABLES


def test_v32_installs_on_real_project_store_schema(tmp_path):
    from app.storage.sqlite import SQLiteStore

    db = tmp_path / "real.db"
    store = SQLiteStore(str(db))
    outbox = SQLiteOutboxV32(str(db), DEFAULT_TABLES)
    store.write_oi("BTCUSDT", 1_000, 100.0)
    store.write_funding("BTCUSDT", 1_000, 0.0001, 100.0)
    store.upsert_bar({
        "symbol": "BTCUSDT", "bucket_ms": 1_000, "open": 100.0, "high": 101.0,
        "low": 99.0, "close": 100.5, "volume_quote": 1000.0,
        "buy_quote": 600.0, "sell_quote": 400.0, "trades": 10, "source": "test",
    })
    pending = outbox.pending(100)
    tables = {x["table_name"] for x in pending}
    assert {"oi_samples", "funding_samples", "bars_1m"}.issubset(tables)
    assert any('"symbol":"BTCUSDT"' in x["row_key"] for x in pending if x["table_name"] == "oi_samples")
    outbox.close()
    store.close()
