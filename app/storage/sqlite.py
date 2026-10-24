from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class SQLiteStore:
    """Durable local store for development/replay. PostgreSQL adapter can replace this later."""

    def __init__(self, path: str = "data/radar.db") -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._init()

    def _init(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS raw_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    symbol TEXT,
                    event_type TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_raw_events_symbol_ts ON raw_events(symbol, ts_ms);
                CREATE INDEX IF NOT EXISTS ix_raw_events_type_ts ON raw_events(event_type, ts_ms);

                CREATE TABLE IF NOT EXISTS oi_samples (
                    symbol TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    open_interest REAL NOT NULL,
                    PRIMARY KEY(symbol, ts_ms)
                );
                CREATE INDEX IF NOT EXISTS ix_oi_symbol_ts ON oi_samples(symbol, ts_ms);

                CREATE TABLE IF NOT EXISTS bars_1m (
                    symbol TEXT NOT NULL,
                    bucket_ms INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume_quote REAL NOT NULL,
                    buy_quote REAL NOT NULL,
                    sell_quote REAL NOT NULL,
                    trades INTEGER NOT NULL,
                    source TEXT NOT NULL DEFAULT 'live',
                    PRIMARY KEY(symbol, bucket_ms)
                );

                CREATE TABLE IF NOT EXISTS funding_samples (
                    symbol TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    funding_rate REAL NOT NULL,
                    mark_price REAL,
                    rate_type TEXT NOT NULL DEFAULT 'Regular',
                    source TEXT NOT NULL DEFAULT 'binance',
                    PRIMARY KEY(symbol, ts_ms)
                );
                CREATE INDEX IF NOT EXISTS ix_funding_symbol_ts ON funding_samples(symbol, ts_ms);

                CREATE TABLE IF NOT EXISTS data_quality_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_ts_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    start_ms INTEGER NOT NULL,
                    end_ms INTEGER NOT NULL,
                    expected_bars INTEGER NOT NULL,
                    actual_bars INTEGER NOT NULL,
                    missing_bars INTEGER NOT NULL,
                    max_gap_ms INTEGER NOT NULL,
                    coverage_pct REAL NOT NULL,
                    status TEXT NOT NULL,
                    details TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_quality_symbol_run ON data_quality_runs(symbol, run_ts_ms);

                CREATE TABLE IF NOT EXISTS feature_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    features TEXT NOT NULL,
                    pump_score REAL,
                    dump_score REAL,
                    stage TEXT,
                    anomaly_score REAL,
                    anomaly_classification TEXT,
                    anomaly_flags TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_features_symbol_ts ON feature_snapshots(symbol, ts_ms);

                CREATE TABLE IF NOT EXISTS signal_episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    start_ts_ms INTEGER NOT NULL,
                    end_ts_ms INTEGER NOT NULL,
                    peak_score REAL NOT NULL,
                    entry_price REAL NOT NULL,
                    snapshots INTEGER NOT NULL,
                    stage_at_entry TEXT NOT NULL,
                    score_version TEXT NOT NULL DEFAULT 'score_features_v1',
                    entry_score REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS ix_signal_episodes_symbol_start ON signal_episodes(symbol, start_ts_ms);
                """
            )
            # Lightweight migrations for databases created by earlier versions.
            columns = {r[1] for r in self.conn.execute("PRAGMA table_info(bars_1m)").fetchall()}
            if "source" not in columns:
                self.conn.execute("ALTER TABLE bars_1m ADD COLUMN source TEXT NOT NULL DEFAULT 'live'")
            raw_columns = {r[1] for r in self.conn.execute("PRAGMA table_info(raw_events)").fetchall()}
            if "payload_hash" not in raw_columns:
                self.conn.execute("ALTER TABLE raw_events ADD COLUMN payload_hash TEXT")
                self.conn.execute("CREATE INDEX IF NOT EXISTS ix_raw_events_hash ON raw_events(payload_hash)")
            feature_columns = {r[1] for r in self.conn.execute("PRAGMA table_info(feature_snapshots)").fetchall()}
            if "anomaly_score" not in feature_columns:
                self.conn.execute("ALTER TABLE feature_snapshots ADD COLUMN anomaly_score REAL")
            if "anomaly_classification" not in feature_columns:
                self.conn.execute("ALTER TABLE feature_snapshots ADD COLUMN anomaly_classification TEXT")
            if "anomaly_flags" not in feature_columns:
                self.conn.execute("ALTER TABLE feature_snapshots ADD COLUMN anomaly_flags TEXT")
            episode_columns = {r[1] for r in self.conn.execute("PRAGMA table_info(signal_episodes)").fetchall()}
            if "entry_score" not in episode_columns:
                self.conn.execute("ALTER TABLE signal_episodes ADD COLUMN entry_score REAL NOT NULL DEFAULT 0")

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def write_raw(self, ts_ms: int, source: str, symbol: str | None, event_type: str, payload: dict[str, Any]) -> None:
        payload_text = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        payload_hash = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO raw_events(ts_ms,source,symbol,event_type,payload,payload_hash) VALUES(?,?,?,?,?,?)",
                (ts_ms, source, symbol, event_type, payload_text, payload_hash),
            )

    def write_oi(self, symbol: str, ts_ms: int, open_interest: float) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO oi_samples(symbol,ts_ms,open_interest) VALUES(?,?,?)",
                (symbol, ts_ms, open_interest),
            )

    def oi_change_pct_at(self, symbol: str, ts_ms: int, lookback_ms: int = 5 * 60_000) -> float | None:
        target = int(ts_ms) - lookback_ms
        cur = self.conn.execute(
            "SELECT open_interest FROM oi_samples WHERE symbol=? AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1",
            (symbol, target),
        )
        old = cur.fetchone()
        cur = self.conn.execute(
            "SELECT open_interest FROM oi_samples WHERE symbol=? AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1",
            (symbol, int(ts_ms)),
        )
        new = cur.fetchone()
        if not old or not new or old[0] == 0:
            return None
        return (new[0] / old[0] - 1.0) * 100.0

    def oi_change_pct(self, symbol: str, lookback_ms: int = 5 * 60_000) -> float | None:
        return self.oi_change_pct_at(symbol, int(time.time() * 1000), lookback_ms)

    def write_funding(self, symbol: str, ts_ms: int, funding_rate: float, mark_price: float | None = None, rate_type: str = "Regular", source: str = "binance") -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO funding_samples(symbol,ts_ms,funding_rate,mark_price,rate_type,source) VALUES(?,?,?,?,?,?)",
                (symbol, ts_ms, funding_rate, mark_price, rate_type, source),
            )

    def funding_at(self, symbol: str, ts_ms: int) -> float | None:
        cur = self.conn.execute(
            "SELECT funding_rate FROM funding_samples WHERE symbol=? AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1",
            (symbol, int(ts_ms)),
        )
        row = cur.fetchone()
        return float(row[0]) if row else None

    def upsert_bar(self, row: dict[str, Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO bars_1m(symbol,bucket_ms,open,high,low,close,volume_quote,buy_quote,sell_quote,trades,source)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(symbol,bucket_ms) DO UPDATE SET
                     high=excluded.high, low=excluded.low, close=excluded.close,
                     volume_quote=excluded.volume_quote, buy_quote=excluded.buy_quote,
                     sell_quote=excluded.sell_quote, trades=excluded.trades, source=excluded.source""",
                tuple(row.get(k, "live") if k == "source" else row[k] for k in ("symbol","bucket_ms","open","high","low","close","volume_quote","buy_quote","sell_quote","trades","source")),
            )

    def recent_bars(self, symbol: str, limit: int = 120) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT symbol,bucket_ms,open,high,low,close,volume_quote,buy_quote,sell_quote,trades,source FROM bars_1m WHERE symbol=? ORDER BY bucket_ms DESC LIMIT ?",
            (symbol, limit),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in reversed(cur.fetchall())]

    def bars_between(self, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        cur = self.conn.execute(
            "SELECT symbol,bucket_ms,open,high,low,close,volume_quote,buy_quote,sell_quote,trades,source FROM bars_1m WHERE symbol=? AND bucket_ms BETWEEN ? AND ? ORDER BY bucket_ms",
            (symbol, start_ms, end_ms),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def write_features(self, ts_ms: int, symbol: str, timeframe: str, features: dict[str, Any], pump_score: float | None, dump_score: float | None, stage: str | None, anomaly_score: float | None = None, anomaly_classification: str | None = None, anomaly_flags: list[str] | tuple[str, ...] | None = None) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO feature_snapshots(ts_ms,symbol,timeframe,features,pump_score,dump_score,stage,anomaly_score,anomaly_classification,anomaly_flags) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (ts_ms, symbol, timeframe, json.dumps(features, separators=(",", ":")), pump_score, dump_score, stage, anomaly_score, anomaly_classification, json.dumps(list(anomaly_flags or ()), separators=(",", ":"))),
            )

    def clear_signal_episodes(self, symbol: str | None = None) -> None:
        with self._lock, self.conn:
            if symbol:
                self.conn.execute("DELETE FROM signal_episodes WHERE symbol=?", (symbol.lower(),))
            else:
                self.conn.execute("DELETE FROM signal_episodes")

    def write_signal_episode(self, episode: dict[str, Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                """INSERT INTO signal_episodes(symbol,direction,start_ts_ms,end_ts_ms,peak_score,entry_price,snapshots,stage_at_entry,score_version,entry_score)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (episode["symbol"], episode["direction"], episode["start_ts_ms"], episode["end_ts_ms"],
                 episode["peak_score"], episode["entry_price"], episode["snapshots"], episode["stage_at_entry"],
                 episode.get("score_version", "score_features_v1"), episode.get("entry_score", episode.get("peak_score", 0))),
            )

    def signal_episodes(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol:
            cur = self.conn.execute(
                "SELECT id,symbol,direction,start_ts_ms,end_ts_ms,peak_score,entry_price,snapshots,stage_at_entry,score_version,entry_score FROM signal_episodes WHERE symbol=? ORDER BY start_ts_ms",
                (symbol.lower(),),
            )
        else:
            cur = self.conn.execute(
                "SELECT id,symbol,direction,start_ts_ms,end_ts_ms,peak_score,entry_price,snapshots,stage_at_entry,score_version,entry_score FROM signal_episodes ORDER BY start_ts_ms"
            )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def bar_coverage(self, symbol: str, start_ms: int, end_ms: int, interval_ms: int = 60_000) -> dict[str, Any]:
        if end_ms < start_ms:
            raise ValueError("end_ms must be >= start_ms")
        expected = ((end_ms - start_ms) // interval_ms) + 1
        rows = self.conn.execute(
            "SELECT bucket_ms FROM bars_1m WHERE symbol=? AND bucket_ms>=? AND bucket_ms<=? ORDER BY bucket_ms",
            (symbol, start_ms, end_ms),
        ).fetchall()
        stamps = [int(r[0]) for r in rows]
        gaps = []
        for prev, cur in zip(stamps, stamps[1:]):
            if cur - prev != interval_ms:
                gaps.append(cur - prev)
        missing = max(0, expected - len(stamps))
        coverage = (len(stamps) / expected * 100.0) if expected else 100.0
        status = "PASS" if missing == 0 and (max(gaps) if gaps else interval_ms) <= interval_ms else ("WARN" if coverage >= 99.0 else "FAIL")
        return {"symbol": symbol, "start_ms": start_ms, "end_ms": end_ms, "expected_bars": expected, "actual_bars": len(stamps), "missing_bars": missing, "max_gap_ms": max(gaps) if gaps else interval_ms, "coverage_pct": coverage, "status": status}

    def record_quality_run(self, report: dict[str, Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO data_quality_runs(run_ts_ms,symbol,start_ms,end_ms,expected_bars,actual_bars,missing_bars,max_gap_ms,coverage_pct,status,details) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (int(time.time()*1000), report["symbol"], report["start_ms"], report["end_ms"], report["expected_bars"], report["actual_bars"], report["missing_bars"], report["max_gap_ms"], report["coverage_pct"], report["status"], json.dumps(report, separators=(",", ":"))),
            )


# v1.8 paper-trading ledger helpers are kept in app.paper_v18 to avoid coupling
# the general storage API to simulation-specific result types.
