from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from app.collectors.coinglass_v24 import CoinGlassClientV24
from app.collectors.geckoterminal_v24 import GeckoTerminalClientV24
from app.collectors.goplus_v24 import GoPlusClientV24

SCHEMA_VERSION = "intelligence_v24"


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def _walk(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _symbol(row: dict[str, Any]) -> str | None:
    for key in ("symbol", "baseAsset", "base_asset", "base_token_symbol", "token_symbol", "coin", "market_pair"):
        v = row.get(key)
        if v is None:
            continue
        text = str(v).upper().replace("/", "").replace("-", "")
        if any(c.isalpha() for c in text):
            return text
    return None


def _float(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in row or row[key] in (None, ""):
            continue
        try:
            x = float(row[key])
            if math.isfinite(x):
                return x
        except (TypeError, ValueError):
            pass
    return None


def normalize_external(source: str, family: str, endpoint: str, payload: Any, ts_ms: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in _walk(payload):
        sym = _symbol(raw)
        if not sym:
            continue
        item: dict[str, Any] = {"symbol": sym, "source": source, "family": family, "endpoint": endpoint, "ts_ms": ts_ms}
        # GeckoTerminal exposes some useful nested attribute fields; flatten the common ones.
        attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else raw
        if isinstance(attrs, dict):
            for ak, av in attrs.items():
                if ak not in raw:
                    raw = {**raw, ak: av}
        aliases = {
            "price_change_pct": ("price_change_pct", "price_change_percent", "change_percent", "change_pct", "price_change_percentage_24h", "h1"),
            "open_interest_change_pct": ("open_interest_change_24h", "open_interest_change_percent", "oi_change_pct"),
            "open_interest_usd": ("open_interest_usd", "openInterestUsd", "oi_usd"),
            "funding_rate": ("avg_funding_rate_by_oi", "funding_rate", "fundingRate"),
            "liquidation_usd": ("liquidation_usd_24h", "liquidation_usd", "volUsd"),
            "netflow_usd": ("netflow_usd", "net_flow_usd", "flow_usd"),
            "volume_usd": ("volume_usd_24h", "volume_usd", "volume_24h", "h24_volume_usd"),
            "liquidity_usd": ("reserve_in_usd", "liquidity_usd", "reserve_usd"),
            "buy_volume_usd": ("buy_volume_usd", "buy_usd", "buys_usd"),
            "sell_volume_usd": ("sell_volume_usd", "sell_usd", "sells_usd"),
            "security_risk": ("security_risk", "risk_score", "risk"),
            "honeypot": ("is_honeypot", "honeypot"),
            "malicious": ("malicious_address", "malicious_token", "malicious_contract"),
        }
        for dst, keys in aliases.items():
            val = _float(raw, *keys)
            if val is not None:
                item[dst] = val
        for key in ("is_honeypot", "honeypot", "malicious_address", "malicious_token", "malicious_contract"):
            if key in raw and isinstance(raw[key], str) and raw[key] in {"0", "1"}:
                item[key] = float(raw[key])
        item["raw_keys"] = sorted(str(k) for k in raw.keys() if len(str(k)) < 80)
        fingerprint = f"{sym}|{source}|{family}|{endpoint}"
        if fingerprint not in seen:
            rows.append(item)
            seen.add(fingerprint)
    return rows


@dataclass(frozen=True, slots=True)
class SourceSnapshotV24:
    source: str
    family: str
    endpoint: str
    ts_ms: int
    status: str
    latency_ms: int
    error: str | None = None


class ExternalIntelligenceStoreV24:
    def __init__(self, path: str = "data/radar.db") -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._init()

    def _init(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS external_snapshots_v24 (
                    snapshot_id TEXT PRIMARY KEY,
                    ts_ms INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    family TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    normalized_count INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    latency_ms INTEGER NOT NULL,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_ext_snap_ts ON external_snapshots_v24(ts_ms DESC);
                CREATE INDEX IF NOT EXISTS ix_ext_snap_source ON external_snapshots_v24(source,ts_ms DESC);

                CREATE TABLE IF NOT EXISTS external_rows_v24 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    family TEXT NOT NULL,
                    endpoint TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    normalized TEXT NOT NULL,
                    payload_hash TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_ext_rows_symbol_ts ON external_rows_v24(symbol,ts_ms DESC);
                CREATE INDEX IF NOT EXISTS ix_ext_rows_family_ts ON external_rows_v24(family,ts_ms DESC);

                CREATE TABLE IF NOT EXISTS consensus_v24 (
                    consensus_id TEXT PRIMARY KEY,
                    ts_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    agreement_score REAL NOT NULL,
                    confidence_score REAL NOT NULL,
                    family_count INTEGER NOT NULL,
                    source_count INTEGER NOT NULL,
                    eligible_for_scoring INTEGER NOT NULL,
                    evidence TEXT NOT NULL,
                    blockers TEXT NOT NULL,
                    warnings TEXT NOT NULL,
                    policy_version TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_consensus_symbol_ts ON consensus_v24(symbol,ts_ms DESC);
                """
            )

    def record(self, snapshot: SourceSnapshotV24, payload: Any, rows: list[dict[str, Any]]) -> str:
        payload_hash = _hash(payload)
        sid = hashlib.sha256(f"{snapshot.source}|{snapshot.family}|{snapshot.endpoint}|{snapshot.ts_ms}|{payload_hash}".encode()).hexdigest()[:32]
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO external_snapshots_v24(snapshot_id,ts_ms,source,family,endpoint,payload_hash,payload,normalized_count,status,latency_ms,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (sid, snapshot.ts_ms, snapshot.source, snapshot.family, snapshot.endpoint, payload_hash, _canon(payload), len(rows), snapshot.status, snapshot.latency_ms, snapshot.error),
            )
            self.conn.executemany(
                "INSERT INTO external_rows_v24(ts_ms,source,family,endpoint,symbol,normalized,payload_hash) VALUES(?,?,?,?,?,?,?)",
                [(snapshot.ts_ms, r["source"], r["family"], r["endpoint"], r["symbol"], _canon(r), payload_hash) for r in rows],
            )
        return sid

    def recent(self, limit: int = 100, symbol: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        if symbol:
            rows = self.conn.execute(
                "SELECT ts_ms,source,family,endpoint,symbol,normalized,payload_hash FROM external_rows_v24 WHERE symbol=? ORDER BY ts_ms DESC LIMIT ?",
                (symbol.upper(), limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT ts_ms,source,family,endpoint,symbol,normalized,payload_hash FROM external_rows_v24 ORDER BY ts_ms DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(zip(("ts_ms","source","family","endpoint","symbol","normalized","payload_hash"), r))
            try:
                d["normalized"] = json.loads(d["normalized"])
            except json.JSONDecodeError:
                d["normalized"] = {}
            out.append(d)
        return out

    def record_consensus(self, result: dict[str, Any]) -> str:
        cid = hashlib.sha256(_canon({k: result[k] for k in result if k not in {"ts_ms"}}).encode()).hexdigest()[:32]
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO consensus_v24(consensus_id,ts_ms,symbol,direction,agreement_score,confidence_score,family_count,source_count,eligible_for_scoring,evidence,blockers,warnings,policy_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid, result["ts_ms"], result["symbol"], result["direction"], result["agreement_score"], result["confidence_score"], result["family_count"], result["source_count"], int(result["eligible_for_scoring"]), _canon(result["evidence"]), _canon(result["blockers"]), _canon(result["warnings"]), result["policy_version"]),
            )
        return cid

    def recent_consensus(self, limit: int = 100, symbol: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        if symbol:
            rows = self.conn.execute("SELECT * FROM consensus_v24 WHERE symbol=? ORDER BY ts_ms DESC LIMIT ?", (symbol.upper(), limit)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM consensus_v24 ORDER BY ts_ms DESC LIMIT ?", (limit,)).fetchall()
        cols = [x[1] for x in self.conn.execute("PRAGMA table_info(consensus_v24)").fetchall()]
        out = []
        for r in rows:
            d = dict(zip(cols, r))
            for k in ("evidence","blockers","warnings"):
                try: d[k] = json.loads(d[k] or "[]")
                except json.JSONDecodeError: d[k] = []
            d["eligible_for_scoring"] = bool(d["eligible_for_scoring"])
            out.append(d)
        return out

    def status(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "snapshots": int(self.conn.execute("SELECT COUNT(*) FROM external_snapshots_v24").fetchone()[0]),
            "rows": int(self.conn.execute("SELECT COUNT(*) FROM external_rows_v24").fetchone()[0]),
            "consensus": int(self.conn.execute("SELECT COUNT(*) FROM consensus_v24").fetchone()[0]),
        }

    def close(self) -> None:
        self.conn.close()


class ConsensusEngineV24:
    POLICY_VERSION = "v24.1"
    FAMILY_WEIGHTS = {"cex": 1.0, "derivatives": 1.2, "onchain": 1.1, "security": 0.5}

    def __init__(self, *, min_families: int = 2, min_agreement: float = 0.65, max_age_ms: int = 180_000):
        self.min_families = max(2, int(min_families))
        self.min_agreement = float(min_agreement)
        self.max_age_ms = max(5_000, int(max_age_ms))

    @staticmethod
    def _vote(row: dict[str, Any]) -> tuple[str, float, list[str], list[str]]:
        evidence: list[str] = []
        warnings: list[str] = []
        price = _float(row, "price_change_pct")
        oi = _float(row, "open_interest_change_pct")
        netflow = _float(row, "netflow_usd")
        buy = _float(row, "buy_volume_usd")
        sell = _float(row, "sell_volume_usd")
        liq = _float(row, "liquidation_usd")
        direction = "NEUTRAL"
        score = 0.0
        if buy is not None and sell is not None and buy + sell > 0:
            ratio = buy / (buy + sell)
            if ratio >= 0.60: direction, score = "LONG", 1.0; evidence.append(f"buy_ratio={ratio:.2f}")
            elif ratio <= 0.40: direction, score = "SHORT", 1.0; evidence.append(f"buy_ratio={ratio:.2f}")
        elif netflow is not None and abs(netflow) > 0:
            direction = "LONG" if netflow > 0 else "SHORT"; score = 0.9; evidence.append(f"netflow={netflow:.0f}")
        elif price is not None:
            direction = "LONG" if price > 0 else "SHORT" if price < 0 else "NEUTRAL"; score = min(1.0, abs(price) / 5.0); evidence.append(f"price_change={price:.2f}%")
            if oi is not None:
                if direction == "LONG" and oi > 0: evidence.append(f"price+oi_confirmation={oi:.2f}%"); score = min(1.0, score + 0.2)
                if direction == "SHORT" and oi > 0: evidence.append(f"price+oi_confirmation={oi:.2f}%"); score = min(1.0, score + 0.2)
        if liq is not None and liq > 0:
            warnings.append(f"liquidations={liq:.0f}")
        return direction, score, evidence, warnings

    def evaluate(self, symbol: str, rows: list[dict[str, Any]], *, now_ms: int | None = None) -> dict[str, Any]:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        latest_by_family: dict[str, dict[str, Any]] = {}
        for row in rows:
            age = max(0, now - int(row.get("ts_ms", now)))
            if age > self.max_age_ms:
                continue
            family = str(row.get("family", "unknown"))
            # one vote per independent source family; newest row wins within a family/source set
            prev = latest_by_family.get(family)
            if prev is None or int(row.get("ts_ms", 0)) > int(prev.get("ts_ms", 0)):
                latest_by_family[family] = row
        votes = []
        blockers: list[str] = []
        evidence: list[str] = []
        warnings: list[str] = []
        for family, row in sorted(latest_by_family.items()):
            if family == "security":
                malicious = _float(row, "malicious", "malicious_address", "malicious_token", "malicious_contract")
                honeypot = _float(row, "honeypot", "is_honeypot")
                risk = _float(row, "security_risk")
                if malicious == 1 or honeypot == 1:
                    blockers.append(f"security_red_flag:{family}")
                if risk is not None and risk >= 80:
                    blockers.append(f"security_risk={risk:.1f}")
                evidence.append(f"security_review:{family}")
                continue
            direction, strength, ev, warn = self._vote(row)
            votes.append((family, direction, strength))
            evidence.extend([f"{family}:{x}" for x in ev])
            warnings.extend([f"{family}:{x}" for x in warn])
        family_count = len(votes)
        long_w = sum(self.FAMILY_WEIGHTS.get(f, 1.0) * s for f, d, s in votes if d == "LONG")
        short_w = sum(self.FAMILY_WEIGHTS.get(f, 1.0) * s for f, d, s in votes if d == "SHORT")
        total_w = long_w + short_w
        gap = abs(long_w - short_w)
        if long_w > short_w and (short_w == 0 or gap >= 0.45):
            direction = "LONG"
        elif short_w > long_w and (long_w == 0 or gap >= 0.45):
            direction = "SHORT"
        else:
            direction = "NEUTRAL"
        agreement = max(long_w, short_w) / total_w if total_w else 0.0
        confidence = agreement * min(1.0, family_count / max(1, self.min_families)) * 100.0
        if family_count < self.min_families:
            warnings.append(f"insufficient_independent_families:{family_count}/{self.min_families}")
        if family_count >= self.min_families and (agreement < self.min_agreement or direction == "NEUTRAL"):
            warnings.append(f"cross_source_disagreement:{agreement:.2f}")
        eligible = False  # remains false until v24 consensus feature group passes independent OOS validation.
        return {
            "ts_ms": now,
            "symbol": symbol.upper(),
            "direction": direction,
            "agreement_score": round(agreement * 100, 2),
            "confidence_score": round(confidence, 2),
            "family_count": family_count,
            "source_count": len(rows),
            "eligible_for_scoring": eligible,
            "evidence": evidence,
            "blockers": blockers,
            "warnings": warnings,
            "policy_version": self.POLICY_VERSION,
        }


class ExternalIntelligenceRuntimeV24:
    def __init__(self, store: ExternalIntelligenceStoreV24, *, coinglass: CoinGlassClientV24 | None = None, gecko: GeckoTerminalClientV24 | None = None, poll_seconds: int = 30, gecko_networks: tuple[str, ...] = ("solana",), consensus: ConsensusEngineV24 | None = None):
        self.store = store
        self.coinglass = coinglass
        self.gecko = gecko
        self.poll_seconds = max(10, int(poll_seconds))
        self.gecko_networks = gecko_networks
        self.consensus = consensus or ConsensusEngineV24()
        self.running = False
        self.tasks: list[asyncio.Task] = []
        self.last_cycle_ms = 0
        self.health: dict[str, dict[str, Any]] = {}

    async def _call(self, source: str, family: str, endpoint: str, fn: Callable[[], Any]) -> list[dict[str, Any]]:
        started = time.perf_counter(); ts = int(time.time() * 1000)
        try:
            payload = await fn()
            latency = int((time.perf_counter() - started) * 1000)
            rows = normalize_external(source, family, endpoint, payload, ts)
            self.store.record(SourceSnapshotV24(source, family, endpoint, ts, "OK", latency), payload, rows)
            self.health[f"{source}:{endpoint}"] = {"ts_ms": ts, "status": "OK", "latency_ms": latency, "normalized": len(rows)}
            return rows
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            self.store.record(SourceSnapshotV24(source, family, endpoint, ts, "ERROR", latency, str(exc)[:500]), {"error": str(exc)}, [])
            self.health[f"{source}:{endpoint}"] = {"ts_ms": ts, "status": "ERROR", "latency_ms": latency, "error": str(exc)[:500]}
            return []

    async def cycle(self) -> list[dict[str, Any]]:
        all_rows: list[dict[str, Any]] = []
        if self.coinglass:
            all_rows += await self._call("coinglass", "derivatives", "futures-coins-markets", self.coinglass.coins_markets)
            all_rows += await self._call("coinglass", "derivatives", "liquidation-order", self.coinglass.liquidation_orders)
        if self.gecko:
            for network in self.gecko_networks:
                all_rows += await self._call("geckoterminal", "onchain", f"{network}-trending-pools", lambda n=network: self.gecko.trending_pools(n))
        symbols = sorted({r["symbol"] for r in all_rows})
        results = []
        for sym in symbols:
            result = self.consensus.evaluate(sym, [r for r in all_rows if r["symbol"] == sym])
            self.store.record_consensus(result)
            results.append(result)
        self.last_cycle_ms = int(time.time() * 1000)
        return results

    async def run(self) -> None:
        while self.running:
            await self.cycle()
            await asyncio.sleep(self.poll_seconds)

    async def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.last_cycle_ms = int(time.time() * 1000)
        if self.coinglass or self.gecko:
            self.tasks.append(asyncio.create_task(self.run()))

    async def stop(self) -> None:
        self.running = False
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            try: await task
            except asyncio.CancelledError: pass
        self.tasks.clear()

    def status(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "running": self.running, "last_cycle_ms": self.last_cycle_ms, "health": self.health, "store": self.store.status()}


async def fetch_token_security(client: GoPlusClientV24, *, chain_id: str, contract_addresses: str, store: ExternalIntelligenceStoreV24) -> list[dict[str, Any]]:
    started = time.perf_counter(); ts = int(time.time() * 1000)
    endpoint = f"token-security/{chain_id}"
    try:
        payload = await client.token_security(chain_id, contract_addresses)
        latency = int((time.perf_counter() - started) * 1000)
        rows = normalize_external("goplus", "security", endpoint, payload, ts)
        store.record(SourceSnapshotV24("goplus", "security", endpoint, ts, "OK", latency), payload, rows)
        return rows
    except Exception as exc:
        latency = int((time.perf_counter() - started) * 1000)
        store.record(SourceSnapshotV24("goplus", "security", endpoint, ts, "ERROR", latency, str(exc)[:500]), {"error": str(exc)}, [])
        return []
