from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "observation_v22"


def _canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def public_config_hash(config: dict[str, Any]) -> str:
    """Hash only public/non-secret runtime configuration."""
    secret_words = ("key", "token", "secret", "password")
    safe = {k: v for k, v in config.items() if not any(w in k.lower() for w in secret_words)}
    return _hash_text(_canon(safe))


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    observation_id: str
    session_id: str
    ts_ms: int
    symbol: str
    source: str
    price: float
    feature_schema_version: str
    config_hash: str
    model_context: dict[str, Any]
    features: dict[str, Any]
    pump_score: float | None
    dump_score: float | None
    fusion_score: float | None
    action: str | None
    quality_tier: str | None
    risk_score: float | None
    ml_probability: float | None
    anomaly_score: float | None
    data_quality_score: float | None
    regime_fit: float | None
    payload_hash: str
    prev_chain_hash: str
    chain_hash: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ObservationLedgerV22:
    """Append-oriented, tamper-evident observation store for live market research."""

    def __init__(self, path: str = "data/radar.db") -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._init()

    def _init(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS observation_sessions_v22 (
                    session_id TEXT PRIMARY KEY,
                    started_at_ms INTEGER NOT NULL,
                    ended_at_ms INTEGER,
                    environment TEXT NOT NULL,
                    code_version TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'RUNNING'
                );
                CREATE INDEX IF NOT EXISTS ix_obs_sessions_start ON observation_sessions_v22(started_at_ms DESC);

                CREATE TABLE IF NOT EXISTS market_observations_v22 (
                    observation_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    source TEXT NOT NULL,
                    price REAL NOT NULL,
                    feature_schema_version TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    model_context TEXT NOT NULL DEFAULT '{}',
                    features TEXT NOT NULL,
                    pump_score REAL,
                    dump_score REAL,
                    fusion_score REAL,
                    action TEXT,
                    quality_tier TEXT,
                    risk_score REAL,
                    ml_probability REAL,
                    anomaly_score REAL,
                    data_quality_score REAL,
                    regime_fit REAL,
                    payload_hash TEXT NOT NULL,
                    prev_chain_hash TEXT NOT NULL DEFAULT '',
                    chain_hash TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES observation_sessions_v22(session_id)
                );
                CREATE INDEX IF NOT EXISTS ix_obs_symbol_ts ON market_observations_v22(symbol,ts_ms);
                CREATE INDEX IF NOT EXISTS ix_obs_session_ts ON market_observations_v22(session_id,ts_ms);
                CREATE INDEX IF NOT EXISTS ix_obs_action_ts ON market_observations_v22(action,ts_ms);
                CREATE INDEX IF NOT EXISTS ix_obs_chain ON market_observations_v22(symbol,session_id,chain_hash);

                CREATE TABLE IF NOT EXISTS observation_events_v22 (
                    event_id TEXT PRIMARY KEY,
                    observation_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    ts_ms INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    reference_id TEXT,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    FOREIGN KEY(observation_id) REFERENCES market_observations_v22(observation_id)
                );
                CREATE INDEX IF NOT EXISTS ix_obs_events_obs ON observation_events_v22(observation_id,ts_ms);
                CREATE INDEX IF NOT EXISTS ix_obs_events_type ON observation_events_v22(event_type,ts_ms);
                """
            )

    def close(self) -> None:
        self.conn.close()

    def start_session(
        self,
        *,
        environment: str = "paper",
        code_version: str = "2.2.0",
        config_hash: str = "",
        metadata: dict[str, Any] | None = None,
        ts_ms: int | None = None,
    ) -> str:
        now = int(ts_ms or time.time() * 1000)
        sid = uuid.uuid4().hex
        with self.conn:
            self.conn.execute(
                "INSERT INTO observation_sessions_v22(session_id,started_at_ms,environment,code_version,config_hash,metadata,status) VALUES(?,?,?,?,?,?,?)",
                (sid, now, environment, code_version, config_hash, _canon(metadata or {}), "RUNNING"),
            )
        return sid

    def stop_session(self, session_id: str, *, status: str = "STOPPED", ts_ms: int | None = None) -> None:
        now = int(ts_ms or time.time() * 1000)
        with self.conn:
            self.conn.execute(
                "UPDATE observation_sessions_v22 SET ended_at_ms=?,status=? WHERE session_id=?",
                (now, status, session_id),
            )

    def latest_session(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM observation_sessions_v22 ORDER BY started_at_ms DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        cols = [x[1] for x in self.conn.execute("PRAGMA table_info(observation_sessions_v22)").fetchall()]
        d = dict(zip(cols, row))
        d["metadata"] = json.loads(d.get("metadata") or "{}")
        return d

    def _previous_chain(self, session_id: str, symbol: str) -> str:
        row = self.conn.execute(
            "SELECT chain_hash FROM market_observations_v22 WHERE session_id=? AND symbol=? ORDER BY ts_ms DESC,created_at_ms DESC LIMIT 1",
            (session_id, symbol.lower()),
        ).fetchone()
        return str(row[0]) if row else ""

    def record_snapshot(
        self,
        *,
        session_id: str,
        ts_ms: int,
        symbol: str,
        source: str,
        price: float,
        features: dict[str, Any],
        pump_score: float | None = None,
        dump_score: float | None = None,
        fusion_score: float | None = None,
        action: str | None = None,
        quality_tier: str | None = None,
        risk_score: float | None = None,
        ml_probability: float | None = None,
        anomaly_score: float | None = None,
        data_quality_score: float | None = None,
        regime_fit: float | None = None,
        model_context: dict[str, Any] | None = None,
        config_hash: str = "",
        feature_schema_version: str = SCHEMA_VERSION,
    ) -> str:
        symbol = symbol.lower()
        oid_seed = f"{session_id}|{symbol}|{int(ts_ms)}|{feature_schema_version}|{config_hash}"
        observation_id = _hash_text(oid_seed)[:24]
        payload = {
            "session_id": session_id,
            "ts_ms": int(ts_ms),
            "symbol": symbol,
            "source": source,
            "price": float(price),
            "feature_schema_version": feature_schema_version,
            "config_hash": config_hash,
            "model_context": model_context or {},
            "features": features,
            "pump_score": pump_score,
            "dump_score": dump_score,
            "fusion_score": fusion_score,
            "action": action,
            "quality_tier": quality_tier,
            "risk_score": risk_score,
            "ml_probability": ml_probability,
            "anomaly_score": anomaly_score,
            "data_quality_score": data_quality_score,
            "regime_fit": regime_fit,
        }
        payload_hash = _hash_text(_canon(payload))
        created = int(time.time() * 1000)
        with self.conn:
            existing = self.conn.execute(
                "SELECT observation_id FROM market_observations_v22 WHERE observation_id=?", (observation_id,)
            ).fetchone()
            if existing:
                return observation_id
            prev = self._previous_chain(session_id, symbol)
            chain_hash = _hash_text(f"{prev}|{payload_hash}")
            self.conn.execute(
                """INSERT INTO market_observations_v22(
                    observation_id,session_id,ts_ms,symbol,source,price,feature_schema_version,
                    config_hash,model_context,features,pump_score,dump_score,fusion_score,action,
                    quality_tier,risk_score,ml_probability,anomaly_score,data_quality_score,regime_fit,
                    payload_hash,prev_chain_hash,chain_hash,created_at_ms
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    observation_id, session_id, int(ts_ms), symbol, source, float(price), feature_schema_version,
                    config_hash, _canon(model_context or {}), _canon(features), pump_score, dump_score, fusion_score,
                    action, quality_tier, risk_score, ml_probability, anomaly_score, data_quality_score, regime_fit,
                    payload_hash, prev, chain_hash, created,
                ),
            )
        return observation_id

    def append_event(
        self,
        observation_id: str,
        event_type: str,
        *,
        reference_id: str | None = None,
        payload: dict[str, Any] | None = None,
        ts_ms: int | None = None,
    ) -> str:
        row = self.conn.execute(
            "SELECT session_id FROM market_observations_v22 WHERE observation_id=?", (observation_id,)
        ).fetchone()
        if not row:
            raise KeyError(f"unknown observation_id: {observation_id}")
        sid = str(row[0])
        eid = uuid.uuid4().hex
        body = payload or {}
        body_text = _canon(body)
        with self.conn:
            self.conn.execute(
                "INSERT INTO observation_events_v22(event_id,observation_id,session_id,ts_ms,event_type,reference_id,payload,payload_hash,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?)",
                (eid, observation_id, sid, int(ts_ms or time.time() * 1000), event_type, reference_id, body_text, _hash_text(body_text), int(time.time() * 1000)),
            )
        return eid

    def recent(self, limit: int = 100, symbol: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        if symbol:
            rows = self.conn.execute(
                "SELECT * FROM market_observations_v22 WHERE symbol=? ORDER BY ts_ms DESC LIMIT ?",
                (symbol.lower(), limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM market_observations_v22 ORDER BY ts_ms DESC LIMIT ?", (limit,)
            ).fetchall()
        cols = [x[1] for x in self.conn.execute("PRAGMA table_info(market_observations_v22)").fetchall()]
        out = []
        for row in rows:
            d = dict(zip(cols, row))
            for k in ("model_context", "features"):
                try:
                    d[k] = json.loads(d.get(k) or "{}")
                except json.JSONDecodeError:
                    d[k] = {}
            out.append(d)
        return out

    def get(self, observation_id: str) -> dict[str, Any] | None:
        rows = self.recent(1)
        row = self.conn.execute("SELECT * FROM market_observations_v22 WHERE observation_id=?", (observation_id,)).fetchone()
        if not row:
            return None
        cols = [x[1] for x in self.conn.execute("PRAGMA table_info(market_observations_v22)").fetchall()]
        d = dict(zip(cols, row))
        for k in ("model_context", "features"):
            try:
                d[k] = json.loads(d.get(k) or "{}")
            except json.JSONDecodeError:
                d[k] = {}
        events = self.conn.execute(
            "SELECT event_id,ts_ms,event_type,reference_id,payload,payload_hash,created_at_ms FROM observation_events_v22 WHERE observation_id=? ORDER BY ts_ms",
            (observation_id,),
        ).fetchall()
        d["events"] = []
        for ev in events:
            item = dict(zip(("event_id","ts_ms","event_type","reference_id","payload","payload_hash","created_at_ms"), ev))
            try:
                item["payload"] = json.loads(item["payload"] or "{}")
            except json.JSONDecodeError:
                item["payload"] = {}
            d["events"].append(item)
        return d

    def verify_chain(self, session_id: str, symbol: str | None = None) -> dict[str, Any]:
        q = "SELECT * FROM market_observations_v22 WHERE session_id=?"
        args: list[Any] = [session_id]
        if symbol:
            q += " AND symbol=?"
            args.append(symbol.lower())
        q += " ORDER BY symbol,ts_ms,created_at_ms"
        rows = self.conn.execute(q, args).fetchall()
        cols = [x[1] for x in self.conn.execute("PRAGMA table_info(market_observations_v22)").fetchall()]
        prev_by_symbol: dict[str, str] = {}
        checked = 0
        broken: list[dict[str, Any]] = []
        for row in rows:
            d = dict(zip(cols, row))
            symbol_key = str(d["symbol"])
            features = json.loads(d["features"] or "{}")
            model_context = json.loads(d["model_context"] or "{}")
            payload = {
                "session_id": d["session_id"], "ts_ms": d["ts_ms"], "symbol": symbol_key,
                "source": d["source"], "price": d["price"], "feature_schema_version": d["feature_schema_version"],
                "config_hash": d["config_hash"], "model_context": model_context, "features": features,
                "pump_score": d["pump_score"], "dump_score": d["dump_score"], "fusion_score": d["fusion_score"],
                "action": d["action"], "quality_tier": d["quality_tier"], "risk_score": d["risk_score"],
                "ml_probability": d["ml_probability"], "anomaly_score": d["anomaly_score"],
                "data_quality_score": d["data_quality_score"], "regime_fit": d["regime_fit"],
            }
            payload_hash = _hash_text(_canon(payload))
            expected_prev = prev_by_symbol.get(symbol_key, "")
            expected_chain = _hash_text(f"{expected_prev}|{payload_hash}")
            checked += 1
            if payload_hash != d["payload_hash"] or expected_prev != d["prev_chain_hash"] or expected_chain != d["chain_hash"]:
                broken.append({"observation_id": d["observation_id"], "symbol": symbol_key, "reason": "hash_chain_mismatch"})
            prev_by_symbol[symbol_key] = d["chain_hash"]
        return {"session_id": session_id, "symbol": symbol.lower() if symbol else None, "checked": checked, "valid": not broken, "broken": broken}

    def status(self) -> dict[str, Any]:
        session = self.latest_session()
        total = int(self.conn.execute("SELECT COUNT(*) FROM market_observations_v22").fetchone()[0])
        events = int(self.conn.execute("SELECT COUNT(*) FROM observation_events_v22").fetchone()[0])
        return {"schema_version": SCHEMA_VERSION, "observations": total, "events": events, "latest_session": session}
