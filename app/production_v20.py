from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from app.risk_v17 import RiskInput, RiskDecision, evaluate_risk
from app.signals.fusion_v17 import FusionResult, fuse_snapshot


@dataclass(frozen=True, slots=True)
class AlertEvent:
    alert_id: str
    ts_ms: int
    symbol: str
    direction: str
    price: float
    action: str
    quality_tier: str
    risk_score: float
    fusion_score: float
    rule_score: float
    ml_probability: float | None
    expected_value_r: float
    net_rr: float
    expected_move_pct: float
    stop_pct: float
    target_pct: float
    liquidity_regime: str
    anomaly_score: float
    data_quality_score: float
    regime_fit: float
    positive_evidence: tuple[str, ...]
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    risk_reasons: tuple[str, ...]
    source: str = "live_pipeline_v20"
    signal_kind: str = "STANDARD"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class AlertLedger:
    """Durable v2.0 alert audit ledger. SQLite is the default; schema is backend-neutral."""

    def __init__(self, path: str = "data/radar.db") -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._init()

    def _init(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS alert_events_v20 (
                    alert_id TEXT PRIMARY KEY,
                    ts_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    price REAL NOT NULL,
                    action TEXT NOT NULL,
                    quality_tier TEXT NOT NULL,
                    risk_score REAL NOT NULL,
                    fusion_score REAL NOT NULL,
                    rule_score REAL NOT NULL,
                    ml_probability REAL,
                    expected_value_r REAL NOT NULL,
                    net_rr REAL NOT NULL,
                    expected_move_pct REAL NOT NULL,
                    stop_pct REAL NOT NULL,
                    target_pct REAL NOT NULL,
                    liquidity_regime TEXT NOT NULL,
                    anomaly_score REAL NOT NULL,
                    data_quality_score REAL NOT NULL,
                    regime_fit REAL NOT NULL,
                    payload TEXT NOT NULL,
                    delivery_status TEXT NOT NULL DEFAULT 'PENDING',
                    delivery_error TEXT,
                    paper_signal_id TEXT,
                    created_at_ms INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_alerts_v20_ts ON alert_events_v20(ts_ms DESC);
                CREATE INDEX IF NOT EXISTS ix_alerts_v20_symbol_ts ON alert_events_v20(symbol, ts_ms DESC);
                CREATE INDEX IF NOT EXISTS ix_alerts_v20_action ON alert_events_v20(action);
                CREATE TABLE IF NOT EXISTS system_state_v20 (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_registry_v20 (
                    model_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    path TEXT,
                    sha256 TEXT,
                    status TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at_ms INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_heartbeats_v20 (
                    source TEXT PRIMARY KEY,
                    ts_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    details TEXT NOT NULL DEFAULT '{}'
                );
                """
            )

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _alert_id(symbol: str, direction: str, ts_ms: int, version: str = "v20") -> str:
        raw = f"{version}|{symbol.lower()}|{direction}|{ts_ms}".encode()
        return hashlib.sha256(raw).hexdigest()[:24]

    def insert(self, event: AlertEvent) -> bool:
        payload = json.dumps(event.as_dict(), separators=(",", ":"), sort_keys=True)
        with self.conn:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO alert_events_v20(
                    alert_id,ts_ms,symbol,direction,price,action,quality_tier,risk_score,
                    fusion_score,rule_score,ml_probability,expected_value_r,net_rr,
                    expected_move_pct,stop_pct,target_pct,liquidity_regime,anomaly_score,
                    data_quality_score,regime_fit,payload,created_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event.alert_id, event.ts_ms, event.symbol, event.direction, event.price,
                    event.action, event.quality_tier, event.risk_score, event.fusion_score,
                    event.rule_score, event.ml_probability, event.expected_value_r, event.net_rr,
                    event.expected_move_pct, event.stop_pct, event.target_pct,
                    event.liquidity_regime, event.anomaly_score, event.data_quality_score,
                    event.regime_fit, payload, int(time.time() * 1000),
                ),
            )
        return cur.rowcount == 1

    def mark_delivery(self, alert_id: str, status: str, error: str | None = None, paper_signal_id: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE alert_events_v20 SET delivery_status=?,delivery_error=?,paper_signal_id=? WHERE alert_id=?",
                (status, error, paper_signal_id, alert_id),
            )

    def recent(self, limit: int = 100, action: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        if action:
            cur = self.conn.execute(
                "SELECT * FROM alert_events_v20 WHERE action=? ORDER BY ts_ms DESC LIMIT ?",
                (action, limit),
            )
        else:
            cur = self.conn.execute("SELECT * FROM alert_events_v20 ORDER BY ts_ms DESC LIMIT ?", (limit,))
        cols = [d[0] for d in cur.description]
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            d["payload"] = json.loads(d.get("payload") or "{}")
            out.append(d)
        return out

    def get(self, alert_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute("SELECT * FROM alert_events_v20 WHERE alert_id=?", (alert_id,))
        row = cur.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cur.description]
        d = dict(zip(cols, row))
        d["payload"] = json.loads(d.get("payload") or "{}")
        return d

    def has_recent(self, symbol: str, direction: str, now_ms: int, cooldown_ms: int) -> bool:
        cur = self.conn.execute(
            "SELECT 1 FROM alert_events_v20 WHERE symbol=? AND direction=? AND action='ALERT' AND ts_ms>=? LIMIT 1",
            (symbol.lower(), direction, int(now_ms - cooldown_ms)),
        )
        return cur.fetchone() is not None

    def active_alerts(self, now_ms: int, lookback_ms: int = 3600_000) -> list[dict[str, Any]]:
        return self.recent(1000, action="ALERT") if lookback_ms <= 0 else [
            x for x in self.recent(1000, action="ALERT") if int(x["ts_ms"]) >= now_ms - lookback_ms
        ]

    def set_state(self, key: str, value: Any) -> None:
        payload = json.dumps(value, separators=(",", ":"), sort_keys=True)
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO system_state_v20(key,value,updated_at_ms) VALUES(?,?,?)",
                (key, payload, int(time.time() * 1000)),
            )

    def get_state(self, key: str, default: Any = None) -> Any:
        cur = self.conn.execute("SELECT value FROM system_state_v20 WHERE key=?", (key,))
        row = cur.fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return default

    def heartbeat(self, source: str, ts_ms: int | None = None, status: str = "OK", details: dict[str, Any] | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO runtime_heartbeats_v20(source,ts_ms,status,details) VALUES(?,?,?,?)",
                (source, int(ts_ms or time.time() * 1000), status, json.dumps(details or {}, separators=(",", ":"))),
            )

    def heartbeats(self) -> dict[str, Any]:
        cur = self.conn.execute("SELECT source,ts_ms,status,details FROM runtime_heartbeats_v20")
        out: dict[str, Any] = {}
        for source, ts, status, details in cur.fetchall():
            try:
                details = json.loads(details)
            except json.JSONDecodeError:
                details = {}
            out[source] = {"ts_ms": ts, "status": status, "details": details}
        return out

    def register_model(self, name: str, version: str, path: str | None, status: str = "ACTIVE", metadata: dict[str, Any] | None = None) -> str:
        model_id = hashlib.sha256(f"{name}|{version}|{path or ''}".encode()).hexdigest()[:24]
        digest = None
        if path and Path(path).exists() and Path(path).is_file():
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            digest = h.hexdigest()
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO model_registry_v20(model_id,name,version,path,sha256,status,metadata,created_at_ms) VALUES(?,?,?,?,?,?,?,?)",
                (model_id, name, version, path, digest, status, json.dumps(metadata or {}, separators=(",", ":")), int(time.time() * 1000)),
            )
        return model_id

    def models(self) -> list[dict[str, Any]]:
        cur = self.conn.execute("SELECT * FROM model_registry_v20 ORDER BY created_at_ms DESC")
        cols = [d[0] for d in cur.description]
        out = []
        for row in cur.fetchall():
            d = dict(zip(cols, row))
            d["metadata"] = json.loads(d["metadata"] or "{}")
            out.append(d)
        return out


class TelegramNotifier:
    def __init__(self, bot_token: str | None = None, chat_id: str | None = None, enabled: bool = False) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = bool(enabled and bot_token and chat_id)

    @staticmethod
    def _format(event: AlertEvent) -> str:
        evidence = ", ".join(event.positive_evidence[:4]) or "n/a"
        warnings = ", ".join(event.warnings[:3]) or "none"
        return (
            f"🚨 CFR v2.0 ALERT\n"
            f"{event.symbol.upper()} | {event.direction.upper()} | Tier {event.quality_tier}\n"
            f"Risk {event.risk_score:.1f} | Fusion {event.fusion_score:.1f}\n"
            f"EV(R) {event.expected_value_r:.2f} | Net RR {event.net_rr:.2f}\n"
            f"Entry≈ {event.price:.8g}\n"
            f"Evidence: {evidence}\n"
            f"Warnings: {warnings}"
        )

    async def send(self, event: AlertEvent) -> tuple[bool, str | None]:
        if not self.enabled:
            return False, "telegram_disabled_or_not_configured"
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(url, json={"chat_id": self.chat_id, "text": self._format(event)})
                r.raise_for_status()
            return True, None
        except Exception as exc:
            return False, str(exc)


class CorrelationExposure:
    def __init__(self, bars_provider: Callable[[str, int], list[dict[str, Any]]], per_alert_risk_pct: float = 1.0, corr_threshold: float = 0.75) -> None:
        self.bars_provider = bars_provider
        self.per_alert_risk_pct = max(0.01, float(per_alert_risk_pct))
        self.corr_threshold = float(corr_threshold)

    @staticmethod
    def _returns(rows: list[dict[str, Any]], n: int = 30) -> list[float]:
        closes = [float(x["close"]) for x in rows if float(x.get("close", 0)) > 0]
        closes = closes[-(n + 1):]
        return [closes[i] / closes[i - 1] - 1.0 for i in range(1, len(closes))]

    @staticmethod
    def _corr(a: list[float], b: list[float]) -> float | None:
        n = min(len(a), len(b))
        if n < 8:
            return None
        a, b = a[-n:], b[-n:]
        ma = sum(a) / n
        mb = sum(b) / n
        va = sum((x - ma) ** 2 for x in a)
        vb = sum((x - mb) ** 2 for x in b)
        if va <= 0 or vb <= 0:
            return None
        cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
        return cov / math.sqrt(va * vb)

    def exposure(self, symbol: str, active_alerts: Iterable[dict[str, Any]]) -> tuple[float, list[dict[str, Any]]]:
        cur = self._returns(self.bars_provider(symbol, 40))
        correlated: list[dict[str, Any]] = []
        for alert in active_alerts:
            other = str(alert.get("symbol", "")).lower()
            if not other or other == symbol.lower():
                continue
            corr = self._corr(cur, self._returns(self.bars_provider(other, 40)))
            if corr is not None and abs(corr) >= self.corr_threshold:
                correlated.append({"symbol": other, "corr": round(corr, 4)})
        return len(correlated) * self.per_alert_risk_pct, correlated


class ProductionRuntimeV20:
    def __init__(
        self,
        ledger: AlertLedger,
        *,
        cooldown_ms: int = 900_000,
        stop_pct: float = 1.5,
        target_pct: float = 4.0,
        horizon_min: int = 60,
        per_alert_risk_pct: float = 1.0,
        max_correlated_exposure_pct: float = 3.0,
        telegram: TelegramNotifier | None = None,
        auto_paper_submit: bool = False,
        quarantine_provider: Callable[[], bool] | None = None,
        paper_submitter: Callable[[dict[str, Any]], str] | None = None,
        premium_alert_router: Any | None = None,
    ) -> None:
        self.ledger = ledger
        self.cooldown_ms = int(cooldown_ms)
        self.stop_pct = float(stop_pct)
        self.target_pct = float(target_pct)
        self.horizon_min = int(horizon_min)
        self.per_alert_risk_pct = float(per_alert_risk_pct)
        self.max_correlated_exposure_pct = float(max_correlated_exposure_pct)
        self.telegram = telegram or TelegramNotifier()
        self.auto_paper_submit = bool(auto_paper_submit)
        self.quarantine_provider = quarantine_provider or (lambda: False)
        self.paper_submitter = paper_submitter
        self.premium_alert_router = premium_alert_router
        self.paused = bool(self.ledger.get_state("v20_paused", False))

    def set_paused(self, enabled: bool, reason: str = "manual") -> None:
        self.paused = bool(enabled)
        self.ledger.set_state("v20_paused", {"value": self.paused, "reason": reason, "ts_ms": int(time.time() * 1000)})

    def status(self) -> dict[str, Any]:
        st = self.ledger.get_state("v20_paused", {"value": False})
        return {"paused": bool(st.get("value", self.paused)), "pause_state": st, "heartbeats": self.ledger.heartbeats()}

    def _event(self, features: dict[str, Any], fusion: FusionResult, risk: RiskDecision, ts_ms: int) -> AlertEvent:
        symbol = str(features.get("symbol") or "unknown").lower()
        price = float(features.get("close") or 0.0)
        expected_move = abs(float(features.get("expected_move_pct") or 0.0))
        return AlertEvent(
            alert_id=self.ledger._alert_id(symbol, fusion.direction, ts_ms),
            ts_ms=ts_ms, symbol=symbol, direction=fusion.direction, price=price,
            action=risk.action, quality_tier=risk.quality_tier, risk_score=risk.risk_score,
            fusion_score=fusion.fusion_score, rule_score=fusion.rule_score,
            ml_probability=fusion.ml_probability, expected_value_r=risk.expected_value_r,
            net_rr=risk.net_rr, expected_move_pct=expected_move, stop_pct=self.stop_pct,
            target_pct=self.target_pct, liquidity_regime=str(features.get("liquidity_regime", "unknown")),
            anomaly_score=fusion.anomaly_score, data_quality_score=fusion.data_quality_score,
            regime_fit=fusion.regime_fit, positive_evidence=fusion.positive_evidence,
            warnings=tuple(dict.fromkeys((*fusion.warnings, *risk.warnings))),
            blockers=risk.blockers, risk_reasons=risk.reasons,
            signal_kind=str(features.get("signal_kind") or "STANDARD").upper(),
        )

    async def consider(self, features: dict[str, Any], pump_score: float, dump_score: float, *, ml_probability: float | None = None, correlated_exposure_pct: float = 0.0, correlation_details: list[dict[str, Any]] | None = None) -> tuple[AlertEvent, RiskDecision] | None:
        if self.paused or self.quarantine_provider():
            return None
        symbol = str(features.get("symbol") or "").lower()
        ts_ms = int(features.get("ts_ms") or time.time() * 1000)
        if not symbol or not float(features.get("close") or 0) > 0:
            return None
        fusion = fuse_snapshot(features, pump_score=pump_score, dump_score=dump_score, ml_probability=ml_probability)
        expected_move = max(
            abs(float(features.get("return_5m_pct") or 0.0)),
            abs(float(features.get("return_15m_pct") or 0.0)),
            abs(float(features.get("range_pct") or 0.0)) * 100.0,
        )
        enriched = dict(features)
        enriched["expected_move_pct"] = expected_move
        if self.ledger.has_recent(symbol, fusion.direction, ts_ms, self.cooldown_ms):
            return None
        risk = evaluate_risk(RiskInput(
            symbol=symbol, direction=fusion.direction, ts_ms=ts_ms,
            fusion_score=fusion.fusion_score, rule_score=fusion.rule_score,
            ml_probability=fusion.ml_probability, expected_move_pct=expected_move,
            stop_pct=self.stop_pct, target_pct=self.target_pct,
            liquidity_regime=str(features.get("liquidity_regime", "unknown")),
            slippage_bps=float(features.get("slippage_bps") or 2.0),
            fee_bps_per_side=float(features.get("fee_bps_per_side") or 4.0),
            funding_cost_pct=float(features.get("funding_cost_pct") or 0.0),
            anomaly_score=fusion.anomaly_score, data_quality_score=fusion.data_quality_score,
            regime_fit=fusion.regime_fit, organic_score=fusion.organic_score,
            correlated_exposure_pct=correlated_exposure_pct,
            max_correlated_exposure_pct=self.max_correlated_exposure_pct,
        ))
        event = self._event(enriched, fusion, risk, ts_ms)
        event = AlertEvent(
            **{**event.as_dict(), "warnings": tuple(dict.fromkeys((*event.warnings, *([f"correlated_with:{x['symbol']}" for x in (correlation_details or [])]))))}
        )
        inserted = self.ledger.insert(event)
        if not inserted:
            return None
        if risk.action != "ALERT":
            return event, risk
        delivered, error = await self.telegram.send(event)
        if self.premium_alert_router is not None:
            try:
                premium_result = await self.premium_alert_router.route_alert(event)
                if isinstance(premium_result, dict):
                    event_warnings = tuple(dict.fromkeys((*event.warnings, f"premium_delivery:{sum(int(v) for k,v in premium_result.items() if k != "eligible" and isinstance(v,int))}")))
                    event = AlertEvent(**{**event.as_dict(), "warnings": event_warnings})
            except Exception as exc:
                error = f"premium_router:{exc}" if not error else f"{error};premium_router:{exc}"
        paper_id = None
        if self.auto_paper_submit and self.paper_submitter is not None:
            try:
                paper_id = self.paper_submitter({
                    "ts_ms": ts_ms, "symbol": symbol, "direction": fusion.direction,
                    "entry_price": float(features["close"]), "stop_pct": self.stop_pct,
                    "target_pct": self.target_pct, "horizon_min": self.horizon_min,
                    "risk_score": risk.risk_score, "fusion_score": fusion.fusion_score,
                    "ml_probability": fusion.ml_probability, "quality_tier": risk.quality_tier,
                    "expected_value_r": risk.expected_value_r,
                    "slippage_bps": float(features.get("slippage_bps") or 2.0),
                    "fee_bps_per_side": float(features.get("fee_bps_per_side") or 4.0),
                    "funding_cost_pct": float(features.get("funding_cost_pct") or 0.0),
                })
            except Exception as exc:
                error = f"paper_submit:{exc}"
        self.ledger.mark_delivery(event.alert_id, "SENT" if delivered else ("PAPER_ONLY" if paper_id else "PENDING"), error, paper_id)
        return event, risk
