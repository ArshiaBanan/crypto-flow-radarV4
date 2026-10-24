from __future__ import annotations

from collections import defaultdict
from typing import Iterable


def resample_1m_bars(bars: Iterable[dict], interval_minutes: int) -> list[dict]:
    """Aggregate canonical 1m bars into a higher timeframe without storing duplicate candles."""
    if interval_minutes <= 0 or interval_minutes not in {3, 5, 15, 30, 60, 120, 240, 360, 720, 1440}:
        raise ValueError("Unsupported aggregation interval")
    width = interval_minutes * 60_000
    groups: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for bar in bars:
        ts = int(bar["bucket_ms"])
        bucket = (ts // width) * width
        groups[(bar["symbol"], bucket)].append(bar)
    out: list[dict] = []
    for (symbol, bucket), rows in sorted(groups.items(), key=lambda x: (x[0][0], x[0][1])):
        rows.sort(key=lambda r: r["bucket_ms"])
        expected = interval_minutes
        # Do not create synthetic candles across missing 1m data.
        if len(rows) != expected:
            continue
        out.append({
            "symbol": symbol,
            "bucket_ms": bucket,
            "open": float(rows[0]["open"]),
            "high": max(float(r["high"]) for r in rows),
            "low": min(float(r["low"]) for r in rows),
            "close": float(rows[-1]["close"]),
            "volume_quote": sum(float(r["volume_quote"]) for r in rows),
            "buy_quote": sum(float(r["buy_quote"]) for r in rows),
            "sell_quote": sum(float(r["sell_quote"]) for r in rows),
            "trades": sum(int(r["trades"]) for r in rows),
            "source": "derived_from_1m",
        })
    return out
