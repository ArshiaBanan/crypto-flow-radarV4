from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class MarketObservation:
    symbol: str
    source: str
    observed_at: datetime = field(default_factory=utcnow)
    price: float | None = None
    volume_24h: float | None = None
    volume_currency: str | None = None
    change_1h_pct: float | None = None
    change_24h_pct: float | None = None
    change_7d_pct: float | None = None
    oi_usd: float | None = None
    funding_rate: float | None = None
    taker_buy_qty: float | None = None
    taker_sell_qty: float | None = None
    bids_value: float | None = None
    asks_value: float | None = None
    long_liquidation_usd: float | None = None
    short_liquidation_usd: float | None = None
    smart_money_netflow_usd: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
