from __future__ import annotations

import json
import math
import sqlite3
import time
from dataclasses import dataclass, asdict
from typing import Any, Iterable, Sequence

EPS = 1e-12


def _finite(x: float) -> bool:
    return math.isfinite(float(x))


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def psi(expected: Sequence[float], actual: Sequence[float], bins: int = 10) -> float:
    """Population Stability Index using quantile bins from the baseline sample."""
    e = [float(x) for x in expected if _finite(x)]
    a = [float(x) for x in actual if _finite(x)]
    if len(e) < 2 or len(a) < 2:
        return float("nan")
    if min(e) == max(e):
        return 0.0 if min(a) == max(a) and min(a) == min(e) else float("inf")
    xs = sorted(e)
    edges = [xs[int((len(xs) - 1) * i / bins)] for i in range(bins + 1)]
    edges[0] = -math.inf
    edges[-1] = math.inf
    # Ensure monotonic unique edges so search does not create zero-width bins.
    unique = [edges[0]]
    for x in edges[1:-1]:
        if x > unique[-1]:
            unique.append(x)
    unique.append(edges[-1])
    k = len(unique) - 1

    def counts(vals: Sequence[float]) -> list[int]:
        out = [0] * k
        for v in vals:
            for i in range(k):
                if unique[i] <= v < unique[i + 1] or (i == k - 1 and v == unique[i + 1]):
                    out[i] += 1
                    break
        return out

    ec, ac = counts(e), counts(a)
    en, an = len(e), len(a)
    val = 0.0
    for x, y in zip(ec, ac):
        ep = max(x / en, EPS)
        ap = max(y / an, EPS)
        val += (ap - ep) * math.log(ap / ep)
    return float(val)


def ks_statistic(expected: Sequence[float], actual: Sequence[float]) -> float:
    """Two-sample KS statistic, dependency-free."""
    e = sorted(float(x) for x in expected if _finite(x))
    a = sorted(float(x) for x in actual if _finite(x))
    if not e or not a:
        return float("nan")
    points = sorted(set(e + a))
    i = j = 0
    d = 0.0
    ne, na = len(e), len(a)
    for x in points:
        while i < ne and e[i] <= x:
            i += 1
        while j < na and a[j] <= x:
            j += 1
        d = max(d, abs(i / ne - j / na))
    return float(d)


def calibration_metrics(probabilities: Sequence[float], outcomes: Sequence[int], bins: int = 10) -> dict[str, float]:
    pairs = [(clamp01(p), 1 if int(y) else 0) for p, y in zip(probabilities, outcomes)]
    if not pairs:
        return {"brier": float("nan"), "logloss": float("nan"), "ece": float("nan"), "count": 0}
    brier = sum((p - y) ** 2 for p, y in pairs) / len(pairs)
    ll = 0.0
    for p, y in pairs:
        q = min(1.0 - EPS, max(EPS, p))
        ll += -(y * math.log(q) + (1 - y) * math.log(1 - q))
    ece = 0.0
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        bucket = [(p, y) for p, y in pairs if (lo <= p < hi) or (i == bins - 1 and p <= hi)]
        if bucket:
            ece += len(bucket) / len(pairs) * abs(sum(p for p, _ in bucket) / len(bucket) - sum(y for _, y in bucket) / len(bucket))
    return {"brier": brier, "logloss": ll / len(pairs), "ece": ece, "count": len(pairs)}


def performance_metrics(returns_r: Sequence[float]) -> dict[str, float]:
    xs = [float(x) for x in returns_r if _finite(x)]
    if not xs:
        return {"count": 0, "win_rate": float("nan"), "mean_r": float("nan"), "expectancy_r": float("nan"), "max_drawdown_r": float("nan")}
    eq = 0.0
    peak = 0.0
    dd = 0.0
    for x in xs:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    wins = sum(1 for x in xs if x > 0)
    return {"count": len(xs), "win_rate": wins / len(xs), "mean_r": sum(xs) / len(xs), "expectancy_r": sum(xs) / len(xs), "max_drawdown_r": dd}


@dataclass(frozen=True, slots=True)
class DriftPolicy:
    feature_psi_warn: float = 0.10
    feature_psi_block: float = 0.25
    feature_ks_warn: float = 0.15
    feature_ks_block: float = 0.30
    prediction_psi_warn: float = 0.10
    prediction_psi_block: float = 0.25
    calibration_ece_warn: float = 0.08
    calibration_ece_block: float = 0.15
    performance_expectancy_floor_r: float = -0.10
    performance_dd_warn_r: float = 3.0
    performance_dd_block_r: float = 6.0
    freshness_warn_sec: int = 90
    freshness_block_sec: int = 300
    min_samples: int = 30


@dataclass(frozen=True, slots=True)
class DriftFinding:
    domain: str
    metric: str
    value: float | None
    threshold_warn: float | None
    threshold_block: float | None
    severity: str  # OK/WARN/BLOCK/INSUFFICIENT_DATA
    message: str


@dataclass(frozen=True, slots=True)
class MonitoringReport:
    ts_ms: int
    status: str  # HEALTHY/WARN/QUARANTINED/INSUFFICIENT_DATA
    quarantine: bool
    findings: list[DriftFinding]
    feature_metrics: dict[str, dict[str, float]]
    prediction_metrics: dict[str, float]
    calibration: dict[str, float]
    performance: dict[str, float]
    freshness: dict[str, float]


class MonitoringEngine:
    """v1.9 monitoring engine for drift, freshness, calibration and live performance."""

    def __init__(self, policy: DriftPolicy | None = None) -> None:
        self.policy = policy or DriftPolicy()

    def evaluate(
        self,
        *,
        baseline_features: dict[str, Sequence[float]],
        current_features: dict[str, Sequence[float]],
        baseline_predictions: Sequence[float] = (),
        current_predictions: Sequence[float] = (),
        current_probabilities: Sequence[float] = (),
        current_outcomes: Sequence[int] = (),
        current_returns_r: Sequence[float] = (),
        latest_data_ts_ms: int | None = None,
        now_ms: int | None = None,
    ) -> MonitoringReport:
        now = int(now_ms if now_ms is not None else time.time() * 1000)
        findings: list[DriftFinding] = []
        feature_metrics: dict[str, dict[str, float]] = {}

        for name in sorted(set(baseline_features) | set(current_features)):
            b, c = baseline_features.get(name, ()), current_features.get(name, ())
            if len(b) < self.policy.min_samples or len(c) < self.policy.min_samples:
                findings.append(DriftFinding("feature", name, None, self.policy.feature_psi_warn, self.policy.feature_psi_block, "INSUFFICIENT_DATA", "not enough baseline/current samples"))
                continue
            p = psi(b, c)
            ks = ks_statistic(b, c)
            feature_metrics[name] = {"psi": p, "ks": ks, "baseline_count": len(b), "current_count": len(c)}
            severity = "OK"
            if p >= self.policy.feature_psi_block or ks >= self.policy.feature_ks_block:
                severity = "BLOCK"
            elif p >= self.policy.feature_psi_warn or ks >= self.policy.feature_ks_warn:
                severity = "WARN"
            findings.append(DriftFinding("feature", name, max(p, ks), self.policy.feature_psi_warn, self.policy.feature_psi_block, severity, f"feature drift PSI={p:.4f}, KS={ks:.4f}"))

        prediction_metrics: dict[str, float] = {}
        if len(baseline_predictions) >= self.policy.min_samples and len(current_predictions) >= self.policy.min_samples:
            pp = psi(baseline_predictions, current_predictions)
            pk = ks_statistic(baseline_predictions, current_predictions)
            prediction_metrics = {"psi": pp, "ks": pk, "baseline_count": len(baseline_predictions), "current_count": len(current_predictions)}
            severity = "OK"
            if pp >= self.policy.prediction_psi_block:
                severity = "BLOCK"
            elif pp >= self.policy.prediction_psi_warn:
                severity = "WARN"
            findings.append(DriftFinding("prediction", "probability_distribution", pp, self.policy.prediction_psi_warn, self.policy.prediction_psi_block, severity, f"prediction PSI={pp:.4f}"))

        cal = calibration_metrics(current_probabilities, current_outcomes)
        if cal["count"] >= self.policy.min_samples:
            sev = "BLOCK" if cal["ece"] >= self.policy.calibration_ece_block else ("WARN" if cal["ece"] >= self.policy.calibration_ece_warn else "OK")
            findings.append(DriftFinding("calibration", "ece", cal["ece"], self.policy.calibration_ece_warn, self.policy.calibration_ece_block, sev, f"ECE={cal['ece']:.4f}, Brier={cal['brier']:.4f}"))
        else:
            findings.append(DriftFinding("calibration", "ece", None, self.policy.calibration_ece_warn, self.policy.calibration_ece_block, "INSUFFICIENT_DATA", "not enough labeled outcomes"))

        perf = performance_metrics(current_returns_r)
        if perf["count"] >= self.policy.min_samples:
            sev = "OK"
            if perf["expectancy_r"] <= self.policy.performance_expectancy_floor_r or perf["max_drawdown_r"] >= self.policy.performance_dd_block_r:
                sev = "BLOCK"
            elif perf["max_drawdown_r"] >= self.policy.performance_dd_warn_r:
                sev = "WARN"
            findings.append(DriftFinding("performance", "expectancy_or_drawdown", perf["expectancy_r"], self.policy.performance_expectancy_floor_r, 0.0, sev, f"expectancy={perf['expectancy_r']:.4f}R, maxDD={perf['max_drawdown_r']:.4f}R"))
        else:
            findings.append(DriftFinding("performance", "live_outcomes", None, self.policy.performance_expectancy_floor_r, self.policy.performance_dd_block_r, "INSUFFICIENT_DATA", "not enough resolved paper/live outcomes"))

        freshness: dict[str, float] = {}
        if latest_data_ts_ms is not None:
            age = max(0.0, (now - int(latest_data_ts_ms)) / 1000.0)
            freshness["age_sec"] = age
            sev = "BLOCK" if age >= self.policy.freshness_block_sec else ("WARN" if age >= self.policy.freshness_warn_sec else "OK")
            findings.append(DriftFinding("freshness", "market_data_age_sec", age, self.policy.freshness_warn_sec, self.policy.freshness_block_sec, sev, f"latest market data age={age:.1f}s"))
        else:
            findings.append(DriftFinding("freshness", "market_data_age_sec", None, self.policy.freshness_warn_sec, self.policy.freshness_block_sec, "INSUFFICIENT_DATA", "no latest data timestamp provided"))

        quarantine = any(f.severity == "BLOCK" for f in findings)
        warn = any(f.severity == "WARN" for f in findings)
        insufficient = all(f.severity == "INSUFFICIENT_DATA" for f in findings)
        status = "QUARANTINED" if quarantine else ("WARN" if warn else ("INSUFFICIENT_DATA" if insufficient else "HEALTHY"))
        return MonitoringReport(now, status, quarantine, findings, feature_metrics, prediction_metrics, cal, perf, freshness)


class MonitoringStore:
    """Persistent audit log and model/runtime quarantine state."""

    def __init__(self, path: str = "data/radar.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._init()

    def _init(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS monitoring_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    quarantine INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_monitoring_runs_ts ON monitoring_runs(ts_ms);
                CREATE TABLE IF NOT EXISTS monitoring_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_ts_ms INTEGER NOT NULL
                );
                """
            )

    def record(self, report: MonitoringReport) -> None:
        payload = asdict(report)
        with self.conn:
            self.conn.execute(
                "INSERT INTO monitoring_runs(ts_ms,status,quarantine,payload) VALUES(?,?,?,?)",
                (report.ts_ms, report.status, int(report.quarantine), json.dumps(payload, separators=(",", ":"), sort_keys=True)),
            )
            self.conn.execute(
                "INSERT INTO monitoring_state(key,value,updated_ts_ms) VALUES('quarantine',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts_ms=excluded.updated_ts_ms",
                (json.dumps(bool(report.quarantine)), report.ts_ms),
            )

    def set_quarantine(self, value: bool, reason: str = "manual", ts_ms: int | None = None) -> None:
        ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
        with self.conn:
            self.conn.execute(
                "INSERT INTO monitoring_state(key,value,updated_ts_ms) VALUES('quarantine',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_ts_ms=excluded.updated_ts_ms",
                (json.dumps({"quarantine": bool(value), "reason": reason}), ts),
            )

    def is_quarantined(self) -> bool:
        row = self.conn.execute("SELECT value FROM monitoring_state WHERE key='quarantine'").fetchone()
        if not row:
            return False
        try:
            parsed = json.loads(row[0])
        except json.JSONDecodeError:
            return str(row[0]).lower() == "true"
        if isinstance(parsed, dict):
            return bool(parsed.get("quarantine"))
        return bool(parsed)

    def state(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT key,value,updated_ts_ms FROM monitoring_state").fetchall()
        out = {}
        for key, value, ts in rows:
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                parsed = value
            out[key] = {"value": parsed, "updated_ts_ms": ts}
        return out

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT ts_ms,status,quarantine,payload FROM monitoring_runs ORDER BY ts_ms DESC LIMIT ?", (max(1, int(limit)),)).fetchall()
        out = []
        for ts_ms, status, quarantine, payload in rows:
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                pass
            out.append({"ts_ms": ts_ms, "status": status, "quarantine": bool(quarantine), "payload": payload})
        return out

    def close(self) -> None:
        self.conn.close()


class LiveMonitoringRuntime:
    """Rolling-window runtime monitor fed by closed 1m bars/features, isolated per symbol."""

    def __init__(self, engine: MonitoringEngine, store: MonitoringStore, *, baseline_size: int = 120, current_size: int = 30, eval_interval_ms: int = 5 * 60_000) -> None:
        if baseline_size <= current_size or current_size < 2:
            raise ValueError("baseline_size must be greater than current_size")
        self.engine = engine
        self.store = store
        self.baseline_size = int(baseline_size)
        self.current_size = int(current_size)
        self.eval_interval_ms = int(eval_interval_ms)
        self.feature_buffers: dict[str, dict[str, list[tuple[int, float]]]] = {}
        self.latest_ts_ms_by_symbol: dict[str, int] = {}
        self.last_eval_ts_ms_by_symbol: dict[str, int] = {}
        self.last_report_by_symbol: dict[str, MonitoringReport] = {}

    def observe_features(self, symbol: str, ts_ms: int, features: dict[str, Any]) -> MonitoringReport | None:
        sym = str(symbol).lower()
        ts = int(ts_ms)
        self.latest_ts_ms_by_symbol[sym] = ts
        buffers = self.feature_buffers.setdefault(sym, {})
        for name, value in features.items():
            if isinstance(value, bool):
                continue
            try:
                x = float(value)
            except (TypeError, ValueError):
                continue
            if not _finite(x):
                continue
            buf = buffers.setdefault(name, [])
            buf.append((ts, x))
            max_keep = self.baseline_size + self.current_size + 5
            if len(buf) > max_keep:
                del buf[:-max_keep]

        last_eval = self.last_eval_ts_ms_by_symbol.get(sym)
        if last_eval is not None and ts - last_eval < self.eval_interval_ms:
            return None
        eligible = [v for v in buffers.values() if len(v) >= self.baseline_size + self.current_size]
        if not eligible:
            return None
        baseline_features: dict[str, list[float]] = {}
        current_features: dict[str, list[float]] = {}
        for name, buf in buffers.items():
            if len(buf) < self.baseline_size + self.current_size:
                continue
            vals = [v for _, v in buf[-(self.baseline_size + self.current_size):]]
            baseline_features[name] = vals[:self.baseline_size]
            current_features[name] = vals[self.baseline_size:]
        if not baseline_features:
            return None
        report = self.engine.evaluate(
            baseline_features=baseline_features,
            current_features=current_features,
            latest_data_ts_ms=ts,
            now_ms=ts,
        )
        self.store.record(report)
        self.last_eval_ts_ms_by_symbol[sym] = ts
        self.last_report_by_symbol[sym] = report
        return report

    @property
    def last_report(self) -> MonitoringReport | None:
        return next(reversed(self.last_report_by_symbol.values()), None) if self.last_report_by_symbol else None

    def status(self) -> dict[str, Any]:
        return {
            "symbols": len(self.feature_buffers),
            "latest_ts_ms_by_symbol": dict(self.latest_ts_ms_by_symbol),
            "last_eval_ts_ms_by_symbol": dict(self.last_eval_ts_ms_by_symbol),
            "buffered_features_by_symbol": {s: len(v) for s, v in self.feature_buffers.items()},
            "quarantine": self.store.is_quarantined(),
            "last_reports": {s: asdict(r) for s, r in self.last_report_by_symbol.items()},
        }

