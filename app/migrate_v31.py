from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TABLES = (
    "raw_events", "oi_samples", "bars_1m", "funding_samples",
    "data_quality_runs", "feature_snapshots", "signal_episodes",
)


def _canon(row: tuple[Any, ...]) -> str:
    return json.dumps(list(row), separators=(",", ":"), ensure_ascii=False, sort_keys=False)


def _hash_rows(rows: list[tuple[Any, ...]]) -> str:
    h = hashlib.sha256()
    for row in rows:
        h.update(_canon(row).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


@dataclass(frozen=True)
class MigrationReportV31:
    source_tables: dict[str, int]
    target_tables: dict[str, int]
    verified_tables: tuple[str, ...]
    source_hashes: dict[str, str]
    target_hashes: dict[str, str]
    status: str


def _source_rows(conn: sqlite3.Connection, table: str) -> list[tuple[Any, ...]]:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    return [tuple(r) for r in rows]


def _target_rows(conn, table: str, order_by: str) -> list[tuple[Any, ...]]:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_by}").fetchall()
    return [tuple(r) for r in rows]


def migrate_sqlite_to_postgres(sqlite_path: str, postgres_dsn: str, *, batch_size: int = 1000, verify: bool = True) -> MigrationReportV31:
    if not postgres_dsn:
        raise ValueError("postgres_dsn is required")
    import psycopg  # type: ignore
    from app.storage.postgres_v31 import PostgreSQLStoreV31

    src = sqlite3.connect(sqlite_path)
    src.execute("PRAGMA query_only=ON")
    target = PostgreSQLStoreV31(postgres_dsn)
    order_map = {
        "raw_events": "id",
        "oi_samples": "symbol,ts_ms",
        "bars_1m": "symbol,bucket_ms",
        "funding_samples": "symbol,ts_ms",
        "data_quality_runs": "id",
        "feature_snapshots": "id",
        "signal_episodes": "id",
    }
    source_counts: dict[str, int] = {}
    target_counts: dict[str, int] = {}
    source_hashes: dict[str, str] = {}
    target_hashes: dict[str, str] = {}
    verified: list[str] = []
    try:
        with target.conn.transaction():
            for table in TABLES:
                rows = _source_rows(src, table)
                source_counts[table] = len(rows)
                source_hashes[table] = _hash_rows(rows)
                # Full refresh of the target is intentional for the first controlled cutover.
                target.conn.execute(f"TRUNCATE TABLE {table} RESTART IDENTITY CASCADE")
                if table == "raw_events":
                    sql = "INSERT INTO raw_events(ts_ms,source,symbol,event_type,payload,payload_hash) VALUES(%s,%s,%s,%s,%s,%s)"
                elif table == "oi_samples":
                    sql = "INSERT INTO oi_samples(symbol,ts_ms,open_interest) VALUES(%s,%s,%s)"
                elif table == "bars_1m":
                    sql = "INSERT INTO bars_1m(symbol,bucket_ms,open,high,low,close,volume_quote,buy_quote,sell_quote,trades,source) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                elif table == "funding_samples":
                    sql = "INSERT INTO funding_samples(symbol,ts_ms,funding_rate,mark_price,rate_type,source) VALUES(%s,%s,%s,%s,%s,%s)"
                elif table == "data_quality_runs":
                    sql = "INSERT INTO data_quality_runs(id,run_ts_ms,symbol,start_ms,end_ms,expected_bars,actual_bars,missing_bars,max_gap_ms,coverage_pct,status,details) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                elif table == "feature_snapshots":
                    sql = "INSERT INTO feature_snapshots(id,ts_ms,symbol,timeframe,features,pump_score,dump_score,stage,anomaly_score,anomaly_classification,anomaly_flags) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                else:
                    sql = "INSERT INTO signal_episodes(id,symbol,direction,start_ts_ms,end_ts_ms,peak_score,entry_price,snapshots,stage_at_entry,score_version,entry_score) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                # SQLite rows match the PostgreSQL column order, including id columns.
                for start in range(0, len(rows), max(1, int(batch_size))):
                    target.conn.executemany(sql, rows[start:start + max(1, int(batch_size))])

            for seq_table, seq_col in (("raw_events", "id"), ("data_quality_runs", "id"), ("feature_snapshots", "id"), ("signal_episodes", "id")):
                max_id = target.conn.execute("SELECT MAX(" + seq_col + ") FROM " + seq_table).fetchone()[0]
                if max_id is not None:
                    target.conn.execute(
                        "SELECT setval(pg_get_serial_sequence(%s,%s), %s, true)",
                        (seq_table, seq_col, int(max_id)),
                    )
        if verify:
            for table in TABLES:
                target_rows = _target_rows(target.conn, table, order_map[table])
                target_counts[table] = len(target_rows)
                target_hashes[table] = _hash_rows(target_rows)
                if source_hashes[table] != target_hashes[table] or source_counts[table] != target_counts[table]:
                    raise RuntimeError(f"verification failed for {table}")
                verified.append(table)
        else:
            target_counts = target.table_counts()
            target_hashes = {t: "" for t in TABLES}
        status = "VERIFIED" if verify else "COPIED_UNVERIFIED"
        return MigrationReportV31(source_counts, target_counts, tuple(verified), source_hashes, target_hashes, status)
    finally:
        src.close()
        target.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Controlled SQLite -> PostgreSQL v3.1 migration")
    parser.add_argument("--sqlite", required=True)
    parser.add_argument("--postgres", required=True)
    parser.add_argument("--no-verify", action="store_true")
    args = parser.parse_args()
    report = migrate_sqlite_to_postgres(args.sqlite, args.postgres, verify=not args.no_verify)
    print(json.dumps(report.__dict__, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
