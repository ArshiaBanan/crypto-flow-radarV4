from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "storage_v32"
DEFAULT_TABLES = (
    "raw_events", "oi_samples", "bars_1m", "funding_samples", "data_quality_runs",
    "feature_snapshots", "signal_episodes", "paper_trades", "monitoring_runs",
    "monitoring_state", "alert_events_v20", "model_registry_v20", "runtime_heartbeats_v20",
    "system_state_v20", "validation_reports_v21", "model_promotion_v21",
    "observation_sessions_v22", "market_observations_v22", "observation_events_v22",
    "source_snapshots_v23", "source_rows_v23", "source_health_v23",
    "external_snapshots_v24", "external_rows_v24", "consensus_v24",
)

DDL_PG = """
CREATE TABLE IF NOT EXISTS cfr_ledger_events_v32 (
    event_id BIGINT PRIMARY KEY,
    table_name TEXT NOT NULL,
    op TEXT NOT NULL CHECK(op IN ('INSERT','UPDATE','DELETE','SNAPSHOT')),
    row_key TEXT NOT NULL,
    row_json JSONB,
    row_hash TEXT,
    created_ts_ms BIGINT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cfr_ledger_events_v32_table ON cfr_ledger_events_v32(table_name, event_id);

CREATE TABLE IF NOT EXISTS cfr_ledger_state_v32 (
    table_name TEXT NOT NULL,
    row_key TEXT NOT NULL,
    row_json JSONB,
    row_hash TEXT,
    updated_ts_ms BIGINT NOT NULL,
    source_event_id BIGINT NOT NULL,
    PRIMARY KEY(table_name, row_key)
);
CREATE INDEX IF NOT EXISTS ix_cfr_ledger_state_v32_table ON cfr_ledger_state_v32(table_name);

CREATE TABLE IF NOT EXISTS cfr_dualwrite_meta_v32 (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_ts_ms BIGINT NOT NULL
);
"""


def canon(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False, default=str)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def row_hash(row: dict[str, Any]) -> str:
    return sha256_text(canon(row))


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]


def table_pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [str(r[1]) for r in sorted(rows, key=lambda x: int(x[5] or 0)) if int(r[5] or 0) > 0]


def normalized_table_set(conn: sqlite3.Connection, tables: Iterable[str]) -> list[str]:
    existing = {str(r[0]) for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    return [t for t in tables if t in existing]


def row_key_from_dict(conn: sqlite3.Connection, table: str, row: dict[str, Any]) -> str:
    pks = table_pk_columns(conn, table)
    if pks:
        return canon({k: row.get(k) for k in pks})
    return canon({"__row_hash__": row_hash(row)})


def install_sqlite_outbox(conn: sqlite3.Connection, tables: Iterable[str]) -> list[str]:
    """Install append-only triggers so every relevant SQLite mutation is captured."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cfr_outbox_v32 (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            op TEXT NOT NULL,
            row_key TEXT NOT NULL,
            row_json TEXT,
            row_hash TEXT,
            created_ts_ms INTEGER NOT NULL,
            applied_ts_ms INTEGER,
            last_error TEXT,
            retry_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS ix_cfr_outbox_v32_pending ON cfr_outbox_v32(applied_ts_ms, event_id);
        CREATE INDEX IF NOT EXISTS ix_cfr_outbox_v32_table ON cfr_outbox_v32(table_name, event_id);
        """
    )
    installed: list[str] = []
    existing = set(normalized_table_set(conn, tables))
    for table in sorted(existing):
        cols = table_columns(conn, table)
        if not cols:
            continue
        pks = table_pk_columns(conn, table)
        key_new = canon({k: f"__NEW__.{k}" for k in pks}) if pks else None
        # SQLite trigger values cannot be interpolated by a JSON builder with a dict comprehension,
        # so build json_object SQL fragments dynamically.
        def obj(alias: str) -> str:
            pairs = []
            for c in cols:
                pairs.append(repr(c))
                pairs.append(f'{alias}."{c}"')
            return f"json_object({','.join(pairs)})"
        def key(alias: str, fallback_obj: str) -> str:
            if pks:
                pairs = []
                for c in pks:
                    pairs.append(repr(c))
                    pairs.append(f'{alias}."{c}"')
                return f"json_object({','.join(pairs)})"
            return fallback_obj
        # Recreate triggers to keep definitions deterministic across upgrades.
        conn.execute(f'DROP TRIGGER IF EXISTS "cfr_v32_ins_{table}"')
        conn.execute(f'DROP TRIGGER IF EXISTS "cfr_v32_upd_{table}"')
        conn.execute(f'DROP TRIGGER IF EXISTS "cfr_v32_del_{table}"')
        new_obj = obj("NEW")
        old_obj = obj("OLD")
        conn.execute(f'DROP TRIGGER IF EXISTS "cfr_v32_ins_{table}"')
        conn.execute(f'''CREATE TRIGGER "cfr_v32_ins_{table}" AFTER INSERT ON "{table}" BEGIN
            INSERT INTO cfr_outbox_v32(table_name,op,row_key,row_json,row_hash,created_ts_ms)
            VALUES ('{table}','INSERT',json(CASE WHEN {"1" if pks else "0"} THEN {key('NEW', new_obj)} ELSE {new_obj} END),{new_obj},NULL,CAST(strftime('%s','now') AS INTEGER)*1000);
        END;''')
        conn.execute(f'''CREATE TRIGGER "cfr_v32_upd_{table}" AFTER UPDATE ON "{table}" BEGIN
            INSERT INTO cfr_outbox_v32(table_name,op,row_key,row_json,row_hash,created_ts_ms)
            VALUES ('{table}','UPDATE',json(CASE WHEN {"1" if pks else "0"} THEN {key('NEW', new_obj)} ELSE {new_obj} END),{new_obj},NULL,CAST(strftime('%s','now') AS INTEGER)*1000);
        END;''')
        conn.execute(f'''CREATE TRIGGER "cfr_v32_del_{table}" AFTER DELETE ON "{table}" BEGIN
            INSERT INTO cfr_outbox_v32(table_name,op,row_key,row_json,row_hash,created_ts_ms)
            VALUES ('{table}','DELETE',json(CASE WHEN {"1" if pks else "0"} THEN {key('OLD', old_obj)} ELSE {old_obj} END),{old_obj},NULL,CAST(strftime('%s','now') AS INTEGER)*1000);
        END;''')
        installed.append(table)
    conn.commit()
    return installed


class SQLiteOutboxV32:
    def __init__(self, path: str, tables: Iterable[str] = DEFAULT_TABLES) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        self.tables = tuple(tables)
        self.installed_tables = install_sqlite_outbox(self.conn, self.tables)

    def pending(self, limit: int = 500) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT event_id,table_name,op,row_key,row_json,row_hash,created_ts_ms,applied_ts_ms,last_error,retry_count "
                "FROM cfr_outbox_v32 WHERE applied_ts_ms IS NULL ORDER BY event_id LIMIT ?",
                (max(1, min(int(limit), 5000)),),
            ).fetchall()
        cols = ["event_id","table_name","op","row_key","row_json","row_hash","created_ts_ms","applied_ts_ms","last_error","retry_count"]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            if d["row_json"] is not None:
                try:
                    d["row_json"] = json.loads(d["row_json"])
                except json.JSONDecodeError:
                    d["row_json"] = None
            d["row_hash"] = d["row_hash"] or (row_hash(d["row_json"]) if isinstance(d["row_json"], dict) else None)
            out.append(d)
        return out

    def mark_applied(self, event_ids: Iterable[int]) -> None:
        ids = [int(x) for x in event_ids]
        if not ids:
            return
        now = int(time.time() * 1000)
        with self._lock, self.conn:
            self.conn.executemany("UPDATE cfr_outbox_v32 SET applied_ts_ms=?,last_error=NULL WHERE event_id=?", [(now, x) for x in ids])

    def mark_error(self, event_id: int, error: str) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "UPDATE cfr_outbox_v32 SET last_error=?,retry_count=retry_count+1 WHERE event_id=?",
                (str(error)[:2000], int(event_id)),
            )

    def pending_count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM cfr_outbox_v32 WHERE applied_ts_ms IS NULL").fetchone()[0])

    def oldest_pending_age_ms(self) -> int | None:
        row = self.conn.execute("SELECT MIN(created_ts_ms) FROM cfr_outbox_v32 WHERE applied_ts_ms IS NULL").fetchone()
        if not row or row[0] is None:
            return None
        return max(0, int(time.time() * 1000) - int(row[0]))

    def snapshot_all(self) -> int:
        count = 0
        conn = self.conn
        existing = normalized_table_set(conn, self.tables)
        for table in existing:
            cols = table_columns(conn, table)
            rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
            for values in rows:
                row = dict(zip(cols, values))
                key = row_key_from_dict(conn, table, row)
                payload = canon(row)
                with conn:
                    conn.execute(
                        "INSERT INTO cfr_outbox_v32(table_name,op,row_key,row_json,row_hash,created_ts_ms) VALUES(?,?,?,?,?,?)",
                        (table, "SNAPSHOT", key, payload, sha256_text(payload), int(time.time() * 1000)),
                    )
                count += 1
        return count

    def reconcile_sqlite(self, table: str) -> dict[str, Any]:
        conn = self.conn
        cols = table_columns(conn, table)
        rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
        items = []
        for values in rows:
            row = dict(zip(cols, values))
            items.append((row_key_from_dict(conn, table, row), row_hash(row)))
        items.sort()
        digest = sha256_text(canon(items))
        return {"table": table, "count": len(items), "digest": digest}

    def close(self) -> None:
        self.conn.close()


class PostgreSQLLedgerV32:
    def __init__(self, dsn: str, connect_timeout: int = 5) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        try:
            import psycopg  # type: ignore
        except ImportError as exc:
            raise RuntimeError("psycopg is required for v3.2 PostgreSQL ledger") from exc
        self.conn = psycopg.connect(dsn, connect_timeout=connect_timeout, autocommit=False)
        self._lock = threading.Lock()
        self.conn.execute(DDL_PG)
        self.conn.commit()
        self.set_meta("schema_version", SCHEMA_VERSION)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def set_meta(self, key: str, value: Any) -> None:
        with self._lock, self.conn.transaction():
            self.conn.execute(
                "INSERT INTO cfr_dualwrite_meta_v32(key,value,updated_ts_ms) VALUES(%s,%s,%s) "
                "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value,updated_ts_ms=EXCLUDED.updated_ts_ms",
                (key, str(value), int(time.time() * 1000)),
            )

    def apply_events(self, events: list[dict[str, Any]]) -> list[int]:
        if not events:
            return []
        applied: list[int] = []
        with self._lock, self.conn.transaction():
            for e in events:
                event_id = int(e["event_id"])
                payload = e.get("row_json")
                payload_hash = e.get("row_hash") or (row_hash(payload) if isinstance(payload, dict) else None)
                self.conn.execute(
                    "INSERT INTO cfr_ledger_events_v32(event_id,table_name,op,row_key,row_json,row_hash,created_ts_ms) "
                    "VALUES(%s,%s,%s,%s,%s::jsonb,%s,%s) ON CONFLICT(event_id) DO NOTHING",
                    (event_id, e["table_name"], e["op"], e["row_key"], canon(payload) if payload is not None else None, payload_hash, int(e["created_ts_ms"])),
                )
                if str(e["op"]).upper() == "DELETE":
                    self.conn.execute(
                        "DELETE FROM cfr_ledger_state_v32 WHERE table_name=%s AND row_key=%s",
                        (e["table_name"], e["row_key"]),
                    )
                else:
                    self.conn.execute(
                        "INSERT INTO cfr_ledger_state_v32(table_name,row_key,row_json,row_hash,updated_ts_ms,source_event_id) "
                        "VALUES(%s,%s,%s::jsonb,%s,%s,%s) "
                        "ON CONFLICT(table_name,row_key) DO UPDATE SET row_json=EXCLUDED.row_json,row_hash=EXCLUDED.row_hash,updated_ts_ms=EXCLUDED.updated_ts_ms,source_event_id=EXCLUDED.source_event_id",
                        (e["table_name"], e["row_key"], canon(payload or {}), payload_hash, int(e["created_ts_ms"]), event_id),
                    )
                applied.append(event_id)
        if applied:
            self.set_meta("last_applied_event_id", max(applied))
        return applied

    def reconcile_table(self, table: str, sqlite_report: dict[str, Any]) -> dict[str, Any]:
        rows = self.conn.execute(
            "SELECT row_key,row_hash FROM cfr_ledger_state_v32 WHERE table_name=%s ORDER BY row_key",
            (table,),
        ).fetchall()
        items = [(str(r[0]), str(r[1] or "")) for r in rows]
        digest = sha256_text(canon(items))
        result = {
            "table": table,
            "sqlite_count": int(sqlite_report["count"]),
            "postgres_count": len(items),
            "sqlite_digest": sqlite_report["digest"],
            "postgres_digest": digest,
            "match": int(sqlite_report["count"]) == len(items) and sqlite_report["digest"] == digest,
        }
        return result

    def apply_snapshot_from_sqlite(self, outbox: SQLiteOutboxV32, tables: Iterable[str] = DEFAULT_TABLES) -> dict[str, Any]:
        snapshots = outbox.snapshot_all()
        applied_total = 0
        while True:
            batch = outbox.pending(1000)
            if not batch:
                break
            applied = self.apply_events(batch)
            outbox.mark_applied(applied)
            applied_total += len(applied)
        reports = []
        for table in normalized_table_set(outbox.conn, tables):
            reports.append(self.reconcile_table(table, outbox.reconcile_sqlite(table)))
        return {"snapshots_enqueued": snapshots, "applied": applied_total, "reports": reports}


@dataclass(frozen=True)
class DualWriteStatusV32:
    mode: str
    configured: bool
    reachable: bool
    pending_events: int
    oldest_pending_age_ms: int | None
    last_applied_event_id: int | None
    last_error: str | None


class DualWriteControllerV32:
    """At-least-once SQLite->Postgres replication using the SQLite outbox as source of truth."""
    def __init__(self, outbox: SQLiteOutboxV32, postgres: PostgreSQLLedgerV32 | None, mode: str = "shadow") -> None:
        mode = str(mode).lower().strip()
        if mode not in {"off", "shadow", "required"}:
            raise ValueError("v32 dual-write mode must be off, shadow, or required")
        self.outbox = outbox
        self.postgres = postgres
        self.mode = mode
        self.last_error: str | None = None

    def pump_once(self, limit: int = 500) -> int:
        if self.mode == "off":
            return 0
        if self.postgres is None:
            self.last_error = "POSTGRES_LEDGER_NOT_CONFIGURED"
            if self.mode == "required":
                raise RuntimeError(self.last_error)
            return 0
        batch = self.outbox.pending(limit)
        if not batch:
            return 0
        try:
            applied = self.postgres.apply_events(batch)
            self.outbox.mark_applied(applied)
            self.last_error = None
            return len(applied)
        except Exception as exc:
            self.last_error = str(exc)[:2000]
            for e in batch[:3]:
                self.outbox.mark_error(int(e["event_id"]), self.last_error)
            if self.mode == "required":
                raise
            return 0

    def status(self) -> DualWriteStatusV32:
        reachable = False
        if self.postgres is not None:
            try:
                self.postgres.conn.execute("SELECT 1").fetchone()
                reachable = True
            except Exception:
                reachable = False
        last = None
        if self.postgres is not None:
            try:
                row = self.postgres.conn.execute("SELECT value FROM cfr_dualwrite_meta_v32 WHERE key='last_applied_event_id'").fetchone()
                last = int(row[0]) if row else None
            except Exception:
                last = None
        return DualWriteStatusV32(
            mode=self.mode,
            configured=self.postgres is not None,
            reachable=reachable,
            pending_events=self.outbox.pending_count(),
            oldest_pending_age_ms=self.outbox.oldest_pending_age_ms(),
            last_applied_event_id=last,
            last_error=self.last_error,
        )

    def ready(self, max_pending: int = 100, max_age_ms: int = 120_000) -> tuple[bool, list[str]]:
        st = self.status()
        reasons: list[str] = []
        if self.mode == "off":
            return True, reasons
        if self.postgres is None or not st.reachable:
            reasons.append("POSTGRES_LEDGER_UNREACHABLE")
        if st.pending_events > int(max_pending):
            reasons.append("DUAL_WRITE_PENDING_BACKLOG")
        if st.oldest_pending_age_ms is not None and st.oldest_pending_age_ms > int(max_age_ms):
            reasons.append("DUAL_WRITE_LAG_TOO_HIGH")
        return len(reasons) == 0, reasons
