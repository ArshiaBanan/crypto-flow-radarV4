from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Awaitable

import httpx
import websockets

from app.resilience_v25 import GLOBAL_PROVIDER_HEALTH_V25, ResilientHTTPTransportV25, ResilienceConfigV25

logger = logging.getLogger(__name__)


class BinanceFuturesClient:
    def __init__(self, ws_url: str, rest_url: str, max_symbols: int = 80, depth_levels: int = 20, transport: ResilientHTTPTransportV25 | None = None):
        self.ws_url = ws_url
        self.rest_url = rest_url.rstrip("/")
        self.max_symbols = max_symbols
        self.depth_levels = depth_levels
        cfg = ResilienceConfigV25(timeout_s=15.0)
        self.transport = transport or ResilientHTTPTransportV25(GLOBAL_PROVIDER_HEALTH_V25, cfg)

    async def exchange_info(self) -> dict:
        data = await self.transport.request_json("binance_rest", "GET", f"{self.rest_url}/fapi/v1/exchangeInfo", expected_types=(dict,))
        return data

    async def top_usdt_symbols(self, limit: int | None = None) -> list[str]:
        limit = limit or self.max_symbols
        info = await self.exchange_info()
        eligible = {
            s["symbol"] for s in info.get("symbols", [])
            if s.get("status") == "TRADING" and s.get("quoteAsset") == "USDT" and s.get("contractType") == "PERPETUAL"
        }
        tickers = await self.transport.request_json("binance_rest", "GET", f"{self.rest_url}/fapi/v1/ticker/24hr", expected_types=(list,))
        ranked = sorted((t for t in tickers if t.get("symbol") in eligible), key=lambda t: float(t.get("quoteVolume", 0.0)), reverse=True)
        return [t["symbol"].lower() for t in ranked[: min(limit, self.max_symbols)]]

    async def open_interest(self, symbol: str) -> float | None:
        data = await self.transport.request_json("binance_rest", "GET", f"{self.rest_url}/fapi/v1/openInterest", params={"symbol": symbol.upper()}, expected_types=(dict,))
        return float(data["openInterest"])

    async def premium_index(self, symbol: str) -> dict:
        data = await self.transport.request_json("binance_rest", "GET", f"{self.rest_url}/fapi/v1/premiumIndex", params={"symbol": symbol.upper()}, expected_types=(dict,))
        return data

    async def stream(self, symbols: list[str], on_event: Callable[[dict], Awaitable[None]]) -> None:
        streams = []
        for symbol in symbols:
            streams.extend([f"{symbol}@aggTrade", f"{symbol}@depth@100ms", f"{symbol}@markPrice@1s", f"{symbol}@forceOrder"])
        url = self.ws_url + "?streams=" + "/".join(streams)
        backoff = 1
        while True:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=8_000_000) as ws:
                    backoff = 1
                    st = GLOBAL_PROVIDER_HEALTH_V25._state("binance_ws")
                    st.state = "CLOSED"
                    async for message in ws:
                        payload = json.loads(message)
                        await on_event(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                GLOBAL_PROVIDER_HEALTH_V25.failure("binance_ws", str(exc), 0)
                logger.warning("Binance WS disconnected: %s; reconnecting in %ss", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
