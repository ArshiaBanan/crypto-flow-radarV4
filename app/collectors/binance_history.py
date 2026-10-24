from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from app.resilience_v25 import GLOBAL_PROVIDER_HEALTH_V25, ResilientHTTPTransportV25, ResilienceConfigV25


KLINE_INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


@dataclass(slots=True)
class ImportStats:
    symbol: str
    interval: str
    start_ms: int
    end_ms: int
    rows: int = 0
    pages: int = 0


class BinanceHistoricalClient:
    """Public Binance USDⓈ-M Futures historical market-data client."""

    def __init__(self, rest_url: str = "https://fapi.binance.com", request_timeout: float = 20.0, transport: ResilientHTTPTransportV25 | None = None):
        self.rest_url = rest_url.rstrip("/")
        cfg = ResilienceConfigV25(timeout_s=request_timeout)
        self.transport = transport or ResilientHTTPTransportV25(GLOBAL_PROVIDER_HEALTH_V25, cfg)

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        return await self.transport.request_json("binance_historical", "GET", f"{self.rest_url}{path}", params=params)

    async def klines(
        self,
        symbol: str,
        interval: str,
        start_ms: int,
        end_ms: int,
        limit: int = 1500,
        sleep_seconds: float = 0.0,
    ) -> list[list[Any]]:
        if interval not in KLINE_INTERVAL_MS:
            raise ValueError(f"Unsupported interval: {interval}")
        if start_ms > end_ms:
            return []
        limit = max(1, min(int(limit), 1500))
        step = KLINE_INTERVAL_MS[interval]
        cursor = int(start_ms)
        out: list[list[Any]] = []
        while cursor <= end_ms:
            rows = await self._get(
                "/fapi/v1/klines",
                {
                    "symbol": symbol.upper(),
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": limit,
                },
            )
            if not rows:
                break
            out.extend(rows)
            last_open = int(rows[-1][0])
            next_cursor = last_open + step
            if next_cursor <= cursor:
                raise RuntimeError("Binance kline pagination stalled")
            cursor = next_cursor
            if len(rows) < limit:
                break
            if sleep_seconds:
                await asyncio.sleep(sleep_seconds)
        return [row for row in out if start_ms <= int(row[0]) <= end_ms]

    async def open_interest_history(
        self,
        symbol: str,
        period: str,
        start_ms: int,
        end_ms: int,
        limit: int = 500,
        sleep_seconds: float = 0.0,
    ) -> list[dict[str, Any]]:
        if period not in {"5m", "15m", "30m", "1h", "2h", "4h", "6h", "12h", "1d"}:
            raise ValueError(f"Unsupported OI period: {period}")
        limit = max(1, min(int(limit), 500))
        cursor = int(start_ms)
        out: list[dict[str, Any]] = []
        while cursor <= end_ms:
            rows = await self._get(
                "/futures/data/openInterestHist",
                {
                    "symbol": symbol.upper(),
                    "period": period,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": limit,
                },
            )
            if not rows:
                break
            out.extend(rows)
            last_ts = int(rows[-1]["timestamp"])
            next_cursor = last_ts + 1
            if next_cursor <= cursor:
                raise RuntimeError("Binance OI pagination stalled")
            cursor = next_cursor
            if len(rows) < limit:
                break
            if sleep_seconds:
                await asyncio.sleep(sleep_seconds)
        return [r for r in out if start_ms <= int(r["timestamp"]) <= end_ms]

    async def funding_history(
        self,
        symbol: str,
        start_ms: int,
        end_ms: int,
        limit: int = 1000,
        sleep_seconds: float = 0.0,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        cursor = int(start_ms)
        out: list[dict[str, Any]] = []
        while cursor <= end_ms:
            rows = await self._get(
                "/fapi/v1/fundingRate",
                {
                    "symbol": symbol.upper(),
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": limit,
                },
            )
            if not rows:
                break
            out.extend(rows)
            last_ts = int(rows[-1]["fundingTime"])
            next_cursor = last_ts + 1
            if next_cursor <= cursor:
                raise RuntimeError("Binance funding pagination stalled")
            cursor = next_cursor
            if len(rows) < limit:
                break
            if sleep_seconds:
                await asyncio.sleep(sleep_seconds)
        return [r for r in out if start_ms <= int(r["fundingTime"]) <= end_ms]


def kline_to_bar(row: list[Any], source: str = "binance_historical") -> dict[str, Any]:
    if len(row) < 12:
        raise ValueError("Malformed Binance kline row")
    volume_quote = float(row[7])
    buy_quote = float(row[10])
    sell_quote = max(0.0, volume_quote - buy_quote)
    return {
        "symbol": "",  # filled by importer
        "bucket_ms": int(row[0]),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume_quote": volume_quote,
        "buy_quote": buy_quote,
        "sell_quote": sell_quote,
        "trades": int(row[8]),
        "source": source,
    }


def funding_to_sample(row: dict[str, Any]) -> tuple[int, float, float | None, str]:
    return (
        int(row["fundingTime"]),
        float(row["fundingRate"]),
        float(row["markPrice"]) if row.get("markPrice") not in (None, "") else None,
        row.get("rateType") or "Regular",
    )
