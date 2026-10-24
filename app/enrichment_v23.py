from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from app.collectors.livecoinwatch import LiveCoinWatchClient
from app.collectors.cryptometer import CryptoMeterClient


SCHEMA_VERSION = "enrichment_v23"


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def _symbol_from_row(row: dict[str, Any]) -> str | None:
    for key in ("symbol", "code", "pair", "market_pair", "marketPair", "name"):
        value = row.get(key)
        if value is None:
            continue
        text = str(value).upper().replace("/", "").replace("-", "")
        if text and any(ch.isalpha() for ch in text):
            return text
    return None


def _num(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        if key not in row:
            continue
        value = row.get(key)
        try:
            x = float(value)
            return x if math.isfinite(x) else None
        except (TypeError, ValueError):
            continue
    return None


def _walk_records(value: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if _symbol_from_row(value) is not None:
            out.append(value)
        for child in value.values():
            out.extend(_walk_records(child))
    elif isinstance(value, list):
        for child in value:
            out.extend(_walk_records(child))
    return out


def normalize_payload(source: str, endpoint: str, payload: Any, fetched_at_ms: int) -> list[dict[str, Any]]:
    """Best-effort source normalization. Unrecognized fields remain in raw payload storage."""
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in _walk_records(payload):
        symbol = _symbol_from_row(row)
        if not symbol:
            continue
        item = {
            "symbol": symbol,
            "source": source,
            "endpoint": endpoint,
            "ts_ms": fetched_at_ms,
        }
        candidates = {
            "change_pct": ("change_pct", "change", "price_change_pct", "percent_change", "percentage"),
            "volume": ("volume", "volume_usd", "volume_24h", "quote_volume"),
            "funding_rate": ("funding_rate", "fundingRate", "funding"),
            "open_interest": ("open_interest", "openInterest", "oi", "open_interest_usd"),
            "liquidation_usd": ("liquidation_usd", "liquidation", "liquidations", "liq_usd"),
            "volume_flow": ("volume_flow", "flow", "net_flow", "buy_sell_flow"),
            "liquidity": ("liquidity", "liquidity_usd", "depth", "depth_usd"),
            "whale_volume": ("whale_volume", "whale_trades", "whale_trade_volume"),
            "price": ("price", "last_price", "lastPrice", "close"),
        }
        for dst, keys in candidates.items():
            value = _num(row, keys)
            if value is not None:
                item[dst] = value
        delta = row.get("delta")
        if isinstance(delta, dict):
            day = delta.get("day")
            try:
                if day is not None and math.isfinite(float(day)):
                    raw = float(day)
                    item.setdefault("change_pct", (raw - 1.0) * 100.0 if abs(raw) <= 2.0 else raw)
            except (TypeError, ValueError):
                pass
        key = (symbol, endpoint)
        if key not in seen:
            normalized.append(item)
            seen.add(key)
    return normalized


@dataclass(frozen=True, slots=True)
class SourceHealth:
    source: str
    endpoint: str
    ts_ms: int
    status: str
    latency_ms: int
    error: str | None = None


class EnrichmentStoreV23:
    def __init__(self, path: str = "data/radar.db") -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._init()

    def _init(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS source_snapshots_v23 (
                    snapshot_id TEXT PRIMARY KEY,
                    ts_ms INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    normalized_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_source_snapshots_ts ON source_snapshots_v23(ts_ms DESC);
                CREATE INDEX IF NOT EXISTS ix_source_snapshots_source_ts ON source_snapshots_v23(source,ts_ms DESC);

                CREATE TABLE IF NOT EXISTS source_rows_v23 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    payload_hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_source_rows_symbol_ts ON source_rows_v23(symbol,ts_ms DESC);
                CREATE INDEX IF NOT EXISTS ix_source_rows_source_ts ON source_rows_v23(source,ts_ms DESC);

                CREATE TABLE IF NOT EXISTS source_health_v23 (
                    source TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    error TEXT,
                    PRIMARY KEY(source,endpoint)
                );
                """
            )

    def record(self, health: SourceHealth, payload: Any, normalized: list[dict[str, Any]]) -> str:
        payload_hash = _hash(payload)
        snapshot_id = hashlib.sha256(f"{health.source}|{health.endpoint}|{health.ts_ms}|{payload_hash}".encode()).hexdigest()[:24]
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO source_snapshots_v23(snapshot_id,ts_ms,source,endpoint,payload_hash,payload,normalized_count,status,latency_ms,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (snapshot_id, health.ts_ms, health.source, health.endpoint, payload_hash, _canon(payload), len(normalized), health.status, health.latency_ms, health.error),
            )
            for row in normalized:
                self.conn.execute(
                    "INSERT INTO source_rows_v23(ts_ms,source,endpoint,symbol,normalized,payload_hash) VALUES(?,?,?,?,?,?)",
                    (health.ts_ms, health.source, health.endpoint, row["symbol"], _canon(row), payload_hash),
                )
            self.conn.execute(
                "INSERT OR REPLACE INTO source_health_v23(source,endpoint,ts_ms,status,latency_ms,error) VALUES(?,?,?,?,?,?)",
                (health.source, health.endpoint, health.ts_ms, health.status, health.latency_ms, health.error),
            )
        return snapshot_id

    def latest_health(self) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT source,endpoint,ts_ms,status,latency_ms,error FROM source_health_v23 ORDER BY source,endpoint").fetchall()
        return [dict(zip(("source","endpoint","ts_ms","status","latency_ms","error"), r)) for r in rows]

    def recent_snapshots(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT snapshot_id,ts_ms,source,endpoint,payload_hash,normalized_count,status,latency_ms,error FROM source_snapshots_v23 ORDER BY ts_ms DESC LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [dict(zip(("snapshot_id","ts_ms","source","endpoint","payload_hash","normalized_count","status","latency_ms","error"), r)) for r in rows]

    def recent_rows(self, limit: int = 100, symbol: str | None = None) -> list[dict[str, Any]]:
        if symbol:
            rows = self.conn.execute("SELECT ts_ms,source,endpoint,symbol,normalized,payload_hash FROM source_rows_v23 WHERE symbol=? ORDER BY ts_ms DESC LIMIT ?", (symbol.upper(), max(1, min(int(limit), 500)))).fetchall()
        else:
            rows = self.conn.execute("SELECT ts_ms,source,endpoint,symbol,normalized,payload_hash FROM source_rows_v23 ORDER BY ts_ms DESC LIMIT ?", (max(1, min(int(limit), 500)),)).fetchall()
        out = []
        for row in rows:
            d = dict(zip(("ts_ms","source","endpoint","symbol","normalized","payload_hash"), row))
            try:
                d["normalized"] = json.loads(d["normalized"])
            except json.JSONDecodeError:
                d["normalized"] = {}
            out.append(d)
        return out

    def close(self) -> None:
        self.conn.close()


class MultiSourceRuntimeV23:
    """Optional live enrichment runtime. It never changes scoring fields directly."""

    def __init__(self, store: EnrichmentStoreV23, *, lcw: LiveCoinWatchClient | None = None, cryptometer: CryptoMeterClient | None = None, lcw_interval_s: int = 60, cm_interval_s: int = 60) -> None:
        self.store = store
        self.lcw = lcw
        self.cryptometer = cryptometer
        self.lcw_interval_s = max(5, int(lcw_interval_s))
        self.cm_interval_s = max(5, int(cm_interval_s))
        self.latest: dict[str, dict[str, dict[str, Any]]] = {}
        self.running = False
        self.last_cycle_ms = 0
        self.tasks: list[asyncio.Task] = []

    async def _call(self, source: str, endpoint: str, fn: Callable[[], Any]) -> None:
        start = time.perf_counter()
        ts = int(time.time() * 1000)
        try:
            payload = await fn()
            self.last_cycle_ms = int(time.time() * 1000)
            latency = int((time.perf_counter() - start) * 1000)
            normalized = normalize_payload(source, endpoint, payload, ts)
            for row in normalized:
                key = f"{source}:{endpoint}"
                bucket = self.latest.setdefault(row["symbol"], {})
                bucket[key] = row
            self.store.record(SourceHealth(source, endpoint, ts, "OK", latency), payload, normalized)
        except Exception as exc:
            latency = int((time.perf_counter() - start) * 1000)
            self.store.record(SourceHealth(source, endpoint, ts, "ERROR", latency, str(exc)[:500]), {"error": str(exc)}, [])

    async def lcw_cycle(self) -> None:
        if not self.lcw:
            return
        await self._call(
            "livecoinwatch",
            "coins-list-btc-volume-band",
            lambda: self.lcw.btc_volume_band(min_btc=200.0, max_btc=500.0, max_pages=25),
        )

    async def cryptometer_cycle(self) -> None:
        if not self.cryptometer:
            return
        endpoints = (
            ("gainers-losers", lambda: self.cryptometer.gainers_losers("5m")),
            ("volume-flow", lambda: self.cryptometer.volume_flow("1h")),
            ("liquidity-lens", lambda: self.cryptometer.liquidity_lens("1h")),
            ("merged-orderbook", lambda: self.cryptometer.merged_orderbook()),
        )
        await asyncio.gather(*(self._call("cryptometer", name, fn) for name, fn in endpoints), return_exceptions=True)

    async def run_lcw(self) -> None:
        while self.running:
            await self.lcw_cycle()
            await asyncio.sleep(self.lcw_interval_s)

    async def run_cm(self) -> None:
        while self.running:
            await self.cryptometer_cycle()
            await asyncio.sleep(self.cm_interval_s)

    def feature_context(self, symbol: str) -> dict[str, Any]:
        """Return source context under a namespace; scoring is unchanged in v2.3."""
        rows = self.latest.get(symbol.upper(), {})
        if not rows:
            return {}
        return {
            "source_enrichment": {
                "endpoints": {key: {k: v for k, v in row.items() if k not in {"symbol"}} for key, row in rows.items()}
            }
        }

    def status(self) -> dict[str, Any]:
        health = self.store.latest_health()
        return {"schema_version": SCHEMA_VERSION, "running": self.running, "cached_symbols": len(self.latest), "last_cycle_ms": self.last_cycle_ms, "health": health}

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.last_cycle_ms = int(time.time() * 1000)
        if self.lcw:
            self.tasks.append(asyncio.create_task(self.run_lcw()))
        if self.cryptometer:
            self.tasks.append(asyncio.create_task(self.run_cm()))

    async def stop(self) -> None:
        self.running = False
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.tasks.clear()
