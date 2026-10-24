from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any


SCHEMA_VERSION = "storage_v31"

DDL = """
CREATE TABLE IF NOT EXISTS raw_events (
    id BIGSERIAL PRIMARY KEY,
    ts_ms BIGINT NOT NULL,
    source TEXT NOT NULL,
    symbol TEXT,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    payload_hash TEXT
);
CREATE INDEX IF NOT EXISTS ix_raw_events_symbol_ts ON raw_events(symbol, ts_ms);
CREATE INDEX IF NOT EXISTS ix_raw_events_type_ts ON raw_events(event_type, ts_ms);
CREATE INDEX IF NOT EXISTS ix_raw_events_hash ON raw_events(payload_hash);

CREATE TABLE IF NOT EXISTS oi_samples (
    symbol TEXT NOT NULL,
    ts_ms BIGINT NOT NULL,
    open_interest DOUBLE PRECISION NOT NULL,
    PRIMARY KEY(symbol, ts_ms)
);
CREATE INDEX IF NOT EXISTS ix_oi_symbol_ts ON oi_samples(symbol, ts_ms);

CREATE TABLE IF NOT EXISTS bars_1m (
    symbol TEXT NOT NULL,
    bucket_ms BIGINT NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume_quote DOUBLE PRECISION NOT NULL,
    buy_quote DOUBLE PRECISION NOT NULL,
    sell_quote DOUBLE PRECISION NOT NULL,
    trades BIGINT NOT NULL,
    source TEXT NOT NULL DEFAULT 'live',
    PRIMARY KEY(symbol, bucket_ms)
);
CREATE INDEX IF NOT EXISTS ix_bars_1m_symbol_bucket ON bars_1m(symbol, bucket_ms);

CREATE TABLE IF NOT EXISTS funding_samples (
    symbol TEXT NOT NULL,
    ts_ms BIGINT NOT NULL,
    funding_rate DOUBLE PRECISION NOT NULL,
    mark_price DOUBLE PRECISION,
    rate_type TEXT NOT NULL DEFAULT 'Regular',
    source TEXT NOT NULL DEFAULT 'binance',
    PRIMARY KEY(symbol, ts_ms)
);
CREATE INDEX IF NOT EXISTS ix_funding_symbol_ts ON funding_samples(symbol, ts_ms);

CREATE TABLE IF NOT EXISTS data_quality_runs (
    id BIGSERIAL PRIMARY KEY,
    run_ts_ms BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    start_ms BIGINT NOT NULL,
    end_ms BIGINT NOT NULL,
    expected_bars BIGINT NOT NULL,
    actual_bars BIGINT NOT NULL,
    missing_bars BIGINT NOT NULL,
    max_gap_ms BIGINT NOT NULL,
    coverage_pct DOUBLE PRECISION NOT NULL,
    status TEXT NOT NULL,
    details TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_quality_symbol_run ON data_quality_runs(symbol, run_ts_ms);

CREATE TABLE IF NOT EXISTS feature_snapshots (
    id BIGSERIAL PRIMARY KEY,
    ts_ms BIGINT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    features TEXT NOT NULL,
    pump_score DOUBLE PRECISION,
    dump_score DOUBLE PRECISION,
    stage TEXT,
    anomaly_score DOUBLE PRECISION,
    anomaly_classification TEXT,
    anomaly_flags TEXT
);
CREATE INDEX IF NOT EXISTS ix_features_symbol_ts ON feature_snapshots(symbol, ts_ms);

CREATE TABLE IF NOT EXISTS signal_episodes (
    id BIGSERIAL PRIMARY KEY,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    start_ts_ms BIGINT NOT NULL,
    end_ts_ms BIGINT NOT NULL,
    peak_score DOUBLE PRECISION NOT NULL,
    entry_price DOUBLE PRECISION NOT NULL,
    snapshots BIGINT NOT NULL,
    stage_at_entry TEXT NOT NULL,
    score_version TEXT NOT NULL DEFAULT 'score_features_v1',
    entry_score DOUBLE PRECISION NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_signal_episodes_symbol_start ON signal_episodes(symbol, start_ts_ms);

CREATE TABLE IF NOT EXISTS cfr_storage_meta_v31 (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _canon(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PostgreSQLStoreV31:
    """PostgreSQL implementation of the market-data DurableStore contract.

    The application only imports psycopg when this backend is actually selected.
    Specialized audit ledgers remain on their existing stores until their own
    migration is validated; this class owns the canonical market-data store.
    """

    def __init__(self, dsn: str, connect_timeout: int = 5) -> None:
        if not dsn:
            raise ValueError("PostgreSQL DSN is required")
        try:
            import psycopg  # type: ignore
        except ImportError as exc:
            raise RuntimeError("psycopg is required for PostgreSQL storage") from exc
        self.dsn = dsn
        self._lock = threading.Lock()
        self.conn = psycopg.connect(dsn, connect_timeout=connect_timeout, autocommit=False)
        self.conn.execute(DDL)
        self.conn.commit()
        self.conn.execute(
            "INSERT INTO cfr_storage_meta_v31(key,value) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            ("schema_version", SCHEMA_VERSION),
        )
        self.conn.execute(
            "INSERT INTO cfr_storage_meta_v31(key,value) VALUES(%s,%s) "
            "ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",
            ("created_at_ms", str(int(time.time() * 1000))),
        )
        self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def write_raw(self, ts_ms: int, source: str, symbol: str | None, event_type: str, payload: dict[str, Any]) -> None:
        payload_text = _canon(payload)
        payload_hash = _hash_text(payload_text)
        with self._lock, self.conn.transaction():
            self.conn.execute(
                "INSERT INTO raw_events(ts_ms,source,symbol,event_type,payload,payload_hash) VALUES(%s,%s,%s,%s,%s,%s)",
                (int(ts_ms), source, symbol, event_type, payload_text, payload_hash),
            )

    def write_oi(self, symbol: str, ts_ms: int, open_interest: float) -> None:
        with self._lock, self.conn.transaction():
            self.conn.execute(
                "INSERT INTO oi_samples(symbol,ts_ms,open_interest) VALUES(%s,%s,%s) "
                "ON CONFLICT(symbol,ts_ms) DO UPDATE SET open_interest=EXCLUDED.open_interest",
                (symbol, int(ts_ms), float(open_interest)),
            )

    def oi_change_pct_at(self, symbol: str, ts_ms: int, lookback_ms: int = 5 * 60_000) -> float | None:
        target = int(ts_ms) - int(lookback_ms)
        old = self.conn.execute(
            "SELECT open_interest FROM oi_samples WHERE symbol=%s AND ts_ms<=%s ORDER BY ts_ms DESC LIMIT 1",
            (symbol, target),
        ).fetchone()
        new = self.conn.execute(
            "SELECT open_interest FROM oi_samples WHERE symbol=%s AND ts_ms<=%s ORDER BY ts_ms DESC LIMIT 1",
            (symbol, int(ts_ms)),
        ).fetchone()
        if not old or not new or float(old[0]) == 0:
            return None
        return (float(new[0]) / float(old[0]) - 1.0) * 100.0

    def oi_change_pct(self, symbol: str, lookback_ms: int = 5 * 60_000) -> float | None:
        return self.oi_change_pct_at(symbol, int(time.time() * 1000), lookback_ms)

    def write_funding(self, symbol: str, ts_ms: int, funding_rate: float, mark_price: float | None = None, rate_type: str = "Regular", source: str = "binance") -> None:
        with self._lock, self.conn.transaction():
            self.conn.execute(
                "INSERT INTO funding_samples(symbol,ts_ms,funding_rate,mark_price,rate_type,source) VALUES(%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT(symbol,ts_ms) DO UPDATE SET funding_rate=EXCLUDED.funding_rate, mark_price=EXCLUDED.mark_price, rate_type=EXCLUDED.rate_type, source=EXCLUDED.source",
                (symbol, int(ts_ms), float(funding_rate), mark_price, rate_type, source),
            )

    def funding_at(self, symbol: str, ts_ms: int) -> float | None:
        row = self.conn.execute(
            "SELECT funding_rate FROM funding_samples WHERE symbol=%s AND ts_ms<=%s ORDER BY ts_ms DESC LIMIT 1",
            (symbol, int(ts_ms)),
        ).fetchone()
        return float(row[0]) if row else None

    def upsert_bar(self, row: dict[str, Any]) -> None:
        values = tuple(row.get(k, "live") if k == "source" else row[k] for k in (
            "symbol","bucket_ms","open","high","low","close","volume_quote","buy_quote","sell_quote","trades","source"
        ))
        with self._lock, self.conn.transaction():
            self.conn.execute(
                """INSERT INTO bars_1m(symbol,bucket_ms,open,high,low,close,volume_quote,buy_quote,sell_quote,trades,source)
                   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT(symbol,bucket_ms) DO UPDATE SET
                     high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
                     volume_quote=EXCLUDED.volume_quote, buy_quote=EXCLUDED.buy_quote,
                     sell_quote=EXCLUDED.sell_quote, trades=EXCLUDED.trades, source=EXCLUDED.source""",
                values,
            )

    def recent_bars(self, symbol: str, limit: int = 120) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT symbol,bucket_ms,open,high,low,close,volume_quote,buy_quote,sell_quote,trades,source "
            "FROM bars_1m WHERE symbol=%s ORDER BY bucket_ms DESC LIMIT %s", (symbol, int(limit))
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in reversed(cur.fetchall())]

    def bars_between(self, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT symbol,bucket_ms,open,high,low,close,volume_quote,buy_quote,sell_quote,trades,source "
            "FROM bars_1m WHERE symbol=%s AND bucket_ms BETWEEN %s AND %s ORDER BY bucket_ms",
            (symbol, int(start_ms), int(end_ms)),
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def write_features(self, ts_ms: int, symbol: str, timeframe: str, features: dict[str, Any], pump_score: float | None, dump_score: float | None, stage: str | None, anomaly_score: float | None = None, anomaly_classification: str | None = None, anomaly_flags: list[str] | tuple[str, ...] | None = None) -> None:
        with self._lock, self.conn.transaction():
            self.conn.execute(
                "INSERT INTO feature_snapshots(ts_ms,symbol,timeframe,features,pump_score,dump_score,stage,anomaly_score,anomaly_classification,anomaly_flags) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (int(ts_ms), symbol, timeframe, _canon(features), pump_score, dump_score, stage, anomaly_score, anomaly_classification, _canon(list(anomaly_flags or ()))),
            )

    def clear_signal_episodes(self, symbol: str | None = None) -> None:
        with self._lock, self.conn.transaction():
            if symbol:
                self.conn.execute("DELETE FROM signal_episodes WHERE symbol=%s", (symbol.lower(),))
            else:
                self.conn.execute("DELETE FROM signal_episodes")

    def write_signal_episode(self, episode: dict[str, Any]) -> None:
        with self._lock, self.conn.transaction():
            self.conn.execute(
                "INSERT INTO signal_episodes(symbol,direction,start_ts_ms,end_ts_ms,peak_score,entry_price,snapshots,stage_at_entry,score_version,entry_score) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    episode["symbol"], episode["direction"], episode["start_ts_ms"], episode["end_ts_ms"],
                    episode["peak_score"], episode["entry_price"], episode["snapshots"], episode["stage_at_entry"],
                    episode.get("score_version", "score_features_v1"), episode.get("entry_score", episode.get("peak_score", 0)),
                ),
            )

    def signal_episodes(self, symbol: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id,symbol,direction,start_ts_ms,end_ts_ms,peak_score,entry_price,snapshots,stage_at_entry,score_version,entry_score FROM signal_episodes"
        args: tuple[Any, ...] = ()
        if symbol:
            sql += " WHERE symbol=%s"
            args = (symbol.lower(),)
        sql += " ORDER BY start_ts_ms"
        cur = self.conn.execute(sql, args)
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def bar_coverage(self, symbol: str, start_ms: int, end_ms: int, interval_ms: int = 60_000) -> dict[str, Any]:
        if end_ms < start_ms:
            raise ValueError("end_ms must be >= start_ms")
        expected = ((int(end_ms) - int(start_ms)) // int(interval_ms)) + 1
        rows = self.conn.execute(
            "SELECT bucket_ms FROM bars_1m WHERE symbol=%s AND bucket_ms>=%s AND bucket_ms<=%s ORDER BY bucket_ms",
            (symbol, int(start_ms), int(end_ms)),
        ).fetchall()
        stamps = [int(r[0]) for r in rows]
        gaps = [cur - prev for prev, cur in zip(stamps, stamps[1:]) if cur - prev != interval_ms]
        missing = max(0, expected - len(stamps))
        coverage = (len(stamps) / expected * 100.0) if expected else 100.0
        max_gap = max(gaps) if gaps else interval_ms
        status = "PASS" if missing == 0 and max_gap <= interval_ms else ("WARN" if coverage >= 99.0 else "FAIL")
        return {"symbol": symbol, "start_ms": start_ms, "end_ms": end_ms, "expected_bars": expected, "actual_bars": len(stamps), "missing_bars": missing, "max_gap_ms": max_gap, "coverage_pct": coverage, "status": status}

    def record_quality_run(self, report: dict[str, Any]) -> None:
        with self._lock, self.conn.transaction():
            self.conn.execute(
                "INSERT INTO data_quality_runs(run_ts_ms,symbol,start_ms,end_ms,expected_bars,actual_bars,missing_bars,max_gap_ms,coverage_pct,status,details) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (int(time.time()*1000), report["symbol"], report["start_ms"], report["end_ms"], report["expected_bars"], report["actual_bars"], report["missing_bars"], report["max_gap_ms"], report["coverage_pct"], report["status"], _canon(report)),
            )

    def table_counts(self) -> dict[str, int]:
        tables = ["raw_events", "oi_samples", "bars_1m", "funding_samples", "data_quality_runs", "feature_snapshots", "signal_episodes"]
        return {table: int(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in tables}

    def schema_version(self) -> str:
        row = self.conn.execute("SELECT value FROM cfr_storage_meta_v31 WHERE key='schema_version'").fetchone()
        return str(row[0]) if row else ""
