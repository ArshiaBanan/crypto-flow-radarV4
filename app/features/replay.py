from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.features.engine import build_features
from app.anomaly import anomaly_features
from app.regime import classify_regime
from app.signals.scorer import score_features
from app.storage.sqlite import SQLiteStore


@dataclass(slots=True)
class MaterializeStats:
    symbol: str
    start_ms: int
    end_ms: int
    candidates: int = 0
    written: int = 0
    skipped_insufficient_history: int = 0
    skipped_gaps: int = 0


def _is_contiguous_1m(rows: list[dict[str, Any]]) -> bool:
    if len(rows) < 2:
        return True
    stamps = [int(r["bucket_ms"]) for r in rows]
    return all(b - a == 60_000 for a, b in zip(stamps, stamps[1:]))


def materialize_symbol(
    db: SQLiteStore,
    symbol: str,
    start_ms: int,
    end_ms: int,
    history_bars: int = 120,
    timeframe: str = "1m",
) -> MaterializeStats:
    """Replay historical 1m bars using only dependencies available at each closed bar."""
    if timeframe != "1m":
        raise ValueError("v0.5 materialization writes canonical 1m features; higher TF can be added after event labels are stable")
    symbol = symbol.lower()
    all_rows = db.bars_between(symbol, start_ms - history_bars * 60_000, end_ms)
    by_ts = {int(r["bucket_ms"]): r for r in all_rows}
    candidates = sorted(ts for ts in by_ts if start_ms <= ts <= end_ms)
    stats = MaterializeStats(symbol, start_ms, end_ms, candidates=len(candidates))

    for ts in candidates:
        window_start = ts - (history_bars - 1) * 60_000
        window = [by_ts.get(t) for t in range(window_start, ts + 60_000, 60_000)]
        if len(window) < history_bars or any(x is None for x in window):
            stats.skipped_insufficient_history += 1
            continue
        bars = [x for x in window if x is not None]
        if not _is_contiguous_1m(bars):
            stats.skipped_gaps += 1
            continue

        oi_pct = db.oi_change_pct_at(symbol, ts + 59_999, lookback_ms=5 * 60_000)
        funding = db.funding_at(symbol, ts + 59_999)
        features = build_features(bars, oi_pct=oi_pct, funding=funding)
        if not features:
            continue

        # Persist anomaly context with the point-in-time feature row.
        features.update(anomaly_features(features))

        # BTC context is optional: when unavailable, the regime engine returns an explicit
        # zero-confidence range/unknown context rather than inventing a regime.
        btc_rows = db.bars_between("btcusdt", ts - 60 * 60_000, ts)
        if btc_rows and len(btc_rows) >= 61:
            btc_closes = [float(r["close"]) for r in sorted(btc_rows, key=lambda x: x["bucket_ms"])[-61:]]
            regime = classify_regime(btc_closes)
            features.update({
                "regime_label": regime.label,
                "regime_confidence": regime.confidence,
                "btc_return_15m": regime.btc_return_15m,
                "btc_return_1h": regime.btc_return_1h,
                "btc_volatility_1h": regime.btc_volatility_1h,
                "breadth": regime.breadth,
            })

        score = score_features(features)
        anomaly = features
        db.write_features(
            ts + 59_999, symbol, timeframe, features, score["pump_score"], score["dump_score"], score["stage"],
            anomaly_score=anomaly.get("anomaly_score"),
            anomaly_classification=anomaly.get("anomaly_classification"),
            anomaly_flags=anomaly.get("anomaly_flags"),
        )
        stats.written += 1
    return stats


def materialize_range(db_path: str, symbols: list[str], start_ms: int, end_ms: int, history_bars: int = 120) -> list[dict[str, Any]]:
    db = SQLiteStore(db_path)
    try:
        return [asdict(materialize_symbol(db, symbol, start_ms, end_ms, history_bars=history_bars)) for symbol in symbols]
    finally:
        db.close()
