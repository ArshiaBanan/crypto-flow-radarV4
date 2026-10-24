from __future__ import annotations

import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class QualityReport:
    symbol: str
    start_ms: int
    end_ms: int
    expected_bars: int
    actual_bars: int
    missing_bars: int
    duplicate_raw_events: int
    max_gap_ms: int
    coverage_pct: float
    status: str


def audit_database(db_path: str, symbol: str, start_ms: int, end_ms: int, interval_ms: int = 60_000) -> QualityReport:
    conn = sqlite3.connect(db_path)
    expected = ((end_ms - start_ms) // interval_ms) + 1
    stamps = [r[0] for r in conn.execute(
        "SELECT bucket_ms FROM bars_1m WHERE symbol=? AND bucket_ms BETWEEN ? AND ? ORDER BY bucket_ms",
        (symbol, start_ms, end_ms),
    )]
    duplicate_raw = conn.execute(
        "SELECT COUNT(*) FROM (SELECT payload_hash,ts_ms,symbol,event_type,COUNT(*) c FROM raw_events WHERE symbol=? AND ts_ms BETWEEN ? AND ? AND payload_hash IS NOT NULL GROUP BY payload_hash,ts_ms,symbol,event_type HAVING c>1)",
        (symbol, start_ms, end_ms),
    ).fetchone()[0]
    gaps = [b - a for a, b in zip(stamps, stamps[1:]) if b - a != interval_ms]
    max_gap = max(gaps) if gaps else interval_ms
    actual = len(stamps)
    missing = max(0, expected - actual)
    coverage = actual / expected * 100 if expected else 100.0
    status = "PASS" if missing == 0 and duplicate_raw == 0 else ("WARN" if coverage >= 99 else "FAIL")
    conn.close()
    return QualityReport(symbol, start_ms, end_ms, expected, actual, missing, duplicate_raw, max_gap, coverage, status)
