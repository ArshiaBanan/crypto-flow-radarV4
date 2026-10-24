from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

import numpy as np


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    min_resolved: int = 100
    min_expectancy_r: float = 0.10
    expectancy_ci: float = 0.95
    bootstrap_iters: int = 2000
    max_drawdown_pct: float = 20.0
    max_ambiguous_rate: float = 0.10
    max_no_data_rate: float = 0.05
    max_calibration_brier: float | None = 0.25
    min_positive_rate: float = 0.50


@dataclass(frozen=True, slots=True)
class ValidationReport:
    cohort: str
    generated_at_ms: int
    status: str  # PASS / FAIL / INSUFFICIENT_DATA
    sample_size: int
    resolved: int
    expectancy_r: float | None
    expectancy_ci_low: float | None
    expectancy_ci_high: float | None
    hit_rate: float | None
    max_drawdown_pct: float | None
    ambiguous_rate: float | None
    no_data_rate: float | None
    calibration_brier: float | None
    checks: tuple[dict[str, Any], ...]
    metadata: dict[str, Any]


class ValidationStore:
    def __init__(self, path: str = "data/radar.db") -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._init()

    def _init(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS validation_reports_v21 (
                    report_id TEXT PRIMARY KEY,
                    generated_at_ms INTEGER NOT NULL,
                    cohort TEXT NOT NULL,
                    status TEXT NOT NULL,
                    sample_size INTEGER NOT NULL,
                    resolved INTEGER NOT NULL,
                    expectancy_r REAL,
                    expectancy_ci_low REAL,
                    expectancy_ci_high REAL,
                    hit_rate REAL,
                    max_drawdown_pct REAL,
                    ambiguous_rate REAL,
                    no_data_rate REAL,
                    calibration_brier REAL,
                    checks TEXT NOT NULL DEFAULT '[]',
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS ix_validation_v21_cohort_ts ON validation_reports_v21(cohort,generated_at_ms);
                CREATE TABLE IF NOT EXISTS model_promotion_v21 (
                    model_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at_ms INTEGER NOT NULL,
                    decided_at_ms INTEGER,
                    reason TEXT NOT NULL DEFAULT '',
                    validation_report_id TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                """
            )

    def record(self, report: ValidationReport) -> str:
        raw = f"{report.cohort}|{report.generated_at_ms}|{report.status}|{report.sample_size}".encode()
        report_id = hashlib.sha256(raw).hexdigest()[:24]
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO validation_reports_v21(
                    report_id,generated_at_ms,cohort,status,sample_size,resolved,expectancy_r,
                    expectancy_ci_low,expectancy_ci_high,hit_rate,max_drawdown_pct,ambiguous_rate,
                    no_data_rate,calibration_brier,checks,metadata
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    report_id, report.generated_at_ms, report.cohort, report.status, report.sample_size,
                    report.resolved, report.expectancy_r, report.expectancy_ci_low,
                    report.expectancy_ci_high, report.hit_rate, report.max_drawdown_pct,
                    report.ambiguous_rate, report.no_data_rate, report.calibration_brier,
                    json.dumps(report.checks, separators=(",", ":"), sort_keys=True),
                    json.dumps(report.metadata, separators=(",", ":"), sort_keys=True),
                ),
            )
        return report_id

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM validation_reports_v21 ORDER BY generated_at_ms DESC LIMIT ?", (max(1, min(int(limit), 500)),)
        ).fetchall()
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(validation_reports_v21)").fetchall()]
        out = []
        for row in rows:
            d = dict(zip(cols, row))
            for k in ("checks", "metadata"):
                try:
                    d[k] = json.loads(d[k] or ("[]" if k == "checks" else "{}"))
                except json.JSONDecodeError:
                    d[k] = [] if k == "checks" else {}
            out.append(d)
        return out

    def request_promotion(self, model_id: str, name: str, version: str, report_id: str, *, status: str = "CANDIDATE", reason: str = "", metadata: dict[str, Any] | None = None) -> None:
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO model_promotion_v21(
                    model_id,name,version,status,requested_at_ms,decided_at_ms,reason,validation_report_id,metadata
                ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (model_id, name, version, status, int(time.time() * 1000), None, reason, report_id,
                 json.dumps(metadata or {}, separators=(",", ":"), sort_keys=True)),
            )

    def set_promotion(self, model_id: str, status: str, reason: str = "") -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE model_promotion_v21 SET status=?, decided_at_ms=?, reason=? WHERE model_id=?",
                (status, int(time.time() * 1000), reason, model_id),
            )

    def promotions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM model_promotion_v21 ORDER BY requested_at_ms DESC LIMIT ?", (max(1, min(int(limit), 500)),)
        ).fetchall()
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(model_promotion_v21)").fetchall()]
        out = []
        for row in rows:
            d = dict(zip(cols, row))
            try:
                d["metadata"] = json.loads(d["metadata"] or "{}")
            except json.JSONDecodeError:
                d["metadata"] = {}
            out.append(d)
        return out

    def close(self) -> None:
        self.conn.close()


def _bootstrap_mean_ci(values: list[float], confidence: float, iters: int, seed_key: str) -> tuple[float, float]:
    if len(values) < 2:
        x = values[0] if values else float("nan")
        return x, x
    arr = np.asarray(values, dtype=float)
    seed = int(hashlib.sha256(seed_key.encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    samples = rng.integers(0, len(arr), size=(max(200, int(iters)), len(arr)))
    means = arr[samples].mean(axis=1)
    alpha = max(0.0, min(0.999, 1.0 - confidence)) / 2.0
    return float(np.quantile(means, alpha)), float(np.quantile(means, 1.0 - alpha))


def _max_drawdown_from_r(net_r: Iterable[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for r in net_r:
        equity *= max(1e-12, 1.0 + float(r) * 0.01)
        peak = max(peak, equity)
        max_dd = min(max_dd, (equity / peak - 1.0) * 100.0)
    return max_dd


def _brier(rows: list[dict[str, Any]]) -> float | None:
    vals = []
    for row in rows:
        p = row.get("ml_probability")
        r = row.get("net_r")
        if p is None or r is None:
            continue
        p = float(p)
        if not math.isfinite(p) or not 0 <= p <= 1:
            continue
        y = 1.0 if float(r) > 0 else 0.0
        vals.append((p - y) ** 2)
    return sum(vals) / len(vals) if vals else None


def validate_paper_rows(rows: Iterable[dict[str, Any]], cohort: str = "all", policy: ValidationPolicy | None = None, *, generated_at_ms: int | None = None) -> ValidationReport:
    policy = policy or ValidationPolicy()
    generated_at_ms = int(generated_at_ms or time.time() * 1000)
    all_rows = [dict(x) for x in rows]
    resolved = [x for x in all_rows if x.get("status") == "RESOLVED" and x.get("net_r") is not None]
    ambiguous = sum(bool(x.get("ambiguous_bar")) for x in all_rows)
    no_data = sum(x.get("status") == "NO_DATA" for x in all_rows)
    if len(resolved) < policy.min_resolved:
        return ValidationReport(cohort, generated_at_ms, "INSUFFICIENT_DATA", len(all_rows), len(resolved), None, None, None, None, None,
                                ambiguous / len(all_rows) if all_rows else None, no_data / len(all_rows) if all_rows else None,
                                _brier(resolved), ({"name": "min_resolved", "pass": False, "value": len(resolved), "threshold": policy.min_resolved},),
                                {"reason": "not_enough_resolved_observations"})

    net_r = [float(x["net_r"]) for x in resolved if math.isfinite(float(x["net_r"]))]
    exp = sum(net_r) / len(net_r)
    ci_low, ci_high = _bootstrap_mean_ci(net_r, policy.expectancy_ci, policy.bootstrap_iters, f"{cohort}|{len(net_r)}")
    hit = sum(r > 0 for r in net_r) / len(net_r)
    dd = _max_drawdown_from_r(net_r)
    amb_rate = ambiguous / len(all_rows) if all_rows else 0.0
    no_data_rate = no_data / len(all_rows) if all_rows else 0.0
    brier = _brier(resolved)
    checks = []
    checks.append({"name": "min_resolved", "pass": len(resolved) >= policy.min_resolved, "value": len(resolved), "threshold": policy.min_resolved})
    checks.append({"name": "expectancy_ci_low", "pass": ci_low >= policy.min_expectancy_r, "value": ci_low, "threshold": policy.min_expectancy_r})
    checks.append({"name": "max_drawdown_pct", "pass": abs(min(dd, 0.0)) <= policy.max_drawdown_pct, "value": dd, "threshold": policy.max_drawdown_pct})
    checks.append({"name": "ambiguous_rate", "pass": amb_rate <= policy.max_ambiguous_rate, "value": amb_rate, "threshold": policy.max_ambiguous_rate})
    checks.append({"name": "no_data_rate", "pass": no_data_rate <= policy.max_no_data_rate, "value": no_data_rate, "threshold": policy.max_no_data_rate})
    checks.append({"name": "positive_rate", "pass": hit >= policy.min_positive_rate, "value": hit, "threshold": policy.min_positive_rate})
    if policy.max_calibration_brier is not None:
        checks.append({"name": "calibration_brier", "pass": brier is not None and brier <= policy.max_calibration_brier, "value": brier, "threshold": policy.max_calibration_brier})
    status = "PASS" if all(bool(x["pass"]) for x in checks) else "FAIL"
    return ValidationReport(cohort, generated_at_ms, status, len(all_rows), len(resolved), exp, ci_low, ci_high, hit, dd, amb_rate, no_data_rate, brier,
                            tuple(checks), {"policy": asdict(policy)})


def load_paper_rows(path: str = "data/radar.db", *, cohort: str = "all", symbol: str | None = None, quality_tier: str | None = None) -> list[dict[str, Any]]:
    conn = sqlite3.connect(path)
    try:
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_trades'").fetchone()
        if not exists:
            return []
        query = "SELECT * FROM paper_trades"
        clauses = []
        args: list[Any] = []
        if symbol:
            clauses.append("symbol=?"); args.append(symbol.lower())
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY signal_ts_ms ASC"
        rows = conn.execute(query, args).fetchall()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(paper_trades)").fetchall()]
        out = []
        for row in rows:
            d = dict(zip(cols, row))
            try:
                md = json.loads(d.get("metadata") or "{}")
            except json.JSONDecodeError:
                md = {}
            d["metadata"] = md
            if quality_tier and str(md.get("quality_tier", "")) != quality_tier:
                continue
            out.append(d)
        return out
    finally:
        conn.close()
