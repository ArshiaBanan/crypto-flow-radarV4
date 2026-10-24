from __future__ import annotations

from dataclasses import dataclass
from statistics import median, pstdev
from typing import Any

from app.features.math import imbalance


@dataclass(slots=True)
class Bar:
    bucket_ms: int
    open: float
    high: float
    low: float
    close: float
    volume_quote: float
    buy_quote: float
    sell_quote: float
    trades: int


class OneMinuteAggregator:
    def __init__(self) -> None:
        self.current: dict[str, Bar] = {}

    def trade(self, symbol: str, ts_ms: int, price: float, quote_qty: float, buyer_is_maker: bool) -> Bar | None:
        bucket = (ts_ms // 60_000) * 60_000
        b = self.current.get(symbol)
        if b is None or b.bucket_ms != bucket:
            finished = b
            self.current[symbol] = Bar(bucket, price, price, price, price, quote_qty, 0.0 if buyer_is_maker else quote_qty, quote_qty if buyer_is_maker else 0.0, 1)
            return finished
        b.high = max(b.high, price)
        b.low = min(b.low, price)
        b.close = price
        b.volume_quote += quote_qty
        if buyer_is_maker:
            b.sell_quote += quote_qty
        else:
            b.buy_quote += quote_qty
        b.trades += 1
        return None

    def flush(self) -> list[tuple[str, Bar]]:
        out = list(self.current.items())
        self.current.clear()
        return out


def zscore(value: float | None, history: list[float]) -> float | None:
    if value is None or len(history) < 10:
        return None
    sd = pstdev(history)
    if sd == 0:
        return 0.0
    return (value - median(history)) / sd


def build_features(bars: list[dict[str, Any]], oi_pct: float | None = None, funding: float | None = None,
                   bid_value: float | None = None, ask_value: float | None = None,
                   long_liq: float | None = None, short_liq: float | None = None) -> dict[str, Any]:
    if not bars:
        return {}
    b = bars[-1]
    prev = bars[-2] if len(bars) >= 2 else None
    vols = [float(x["volume_quote"]) for x in bars[:-1]]
    closes = [float(x["close"]) for x in bars]
    buy = float(b["buy_quote"])
    sell = float(b["sell_quote"])
    total = buy + sell
    f: dict[str, Any] = {
        "close": float(b["close"]),
        "bar_volume_quote": float(b["volume_quote"]),
        "buy_quote": buy,
        "sell_quote": sell,
        "taker_imbalance": imbalance(buy, sell),
        "rvol_1m_vs_history": (float(b["volume_quote"]) / median(vols)) if vols and median(vols) > 0 else None,
        "volume_z_median": zscore(float(b["volume_quote"]), vols),
        "return_1m_pct": ((float(b["close"]) / float(prev["close"]) - 1) * 100) if prev and float(prev["close"]) else None,
        "return_5m_pct": ((float(b["close"]) / float(bars[-6]["close"]) - 1) * 100) if len(bars) >= 6 and float(bars[-6]["close"]) else None,
        "return_15m_pct": ((float(b["close"]) / float(bars[-16]["close"]) - 1) * 100) if len(bars) >= 16 and float(bars[-16]["close"]) else None,
        "oi_change_pct": oi_pct,
        "funding_rate": funding,
        "orderbook_imbalance": imbalance(bid_value, ask_value),
        "long_liquidation_usd": long_liq,
        "short_liquidation_usd": short_liq,
    }
    if total:
        f["buy_share"] = buy / total
    return f
