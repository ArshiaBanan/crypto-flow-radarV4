from __future__ import annotations

import math


def clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


def safe_pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old in (None, 0):
        return None
    return (new / old - 1.0) * 100.0


def imbalance(buy: float | None, sell: float | None) -> float | None:
    if buy is None or sell is None:
        return None
    denom = buy + sell
    if denom == 0:
        return 0.0
    return (buy - sell) / denom


def minmax(value: float | None, low: float, high: float) -> float:
    if value is None or math.isnan(value):
        return 50.0
    if high <= low:
        return 50.0
    return clamp((value - low) / (high - low) * 100.0)
