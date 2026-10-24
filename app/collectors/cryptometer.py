from __future__ import annotations

from typing import Any
from app.resilience_v25 import ProviderHealthRegistryV25, ResilientHTTPTransportV25, ResilienceConfigV25, GLOBAL_PROVIDER_HEALTH_V25


class CryptoMeterClient:
    def __init__(self, api_key: str, base_url: str = "https://api.cryptometer.io", timeout: float = 20.0, transport: ResilientHTTPTransportV25 | None = None):
        if not api_key:
            raise ValueError("CryptoMeter API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        cfg = ResilienceConfigV25(timeout_s=timeout)
        self.transport = transport or ResilientHTTPTransportV25(GLOBAL_PROVIDER_HEALTH_V25, cfg)

    async def get(self, endpoint: str, **params: Any) -> dict[str, Any]:
        query = {k: v for k, v in params.items() if v is not None}; query["api_key"] = self.api_key
        data = await self.transport.request_json("cryptometer", "GET", f"{self.base_url}/{endpoint.strip('/')}/", params=query, expected_types=(dict,))
        if str(data.get("success")).lower() != "true":
            raise RuntimeError(f"Unexpected CryptoMeter response for {endpoint}: {data!r}")
        return data

    async def gainers_losers(self, timeframe: str = "5m", exchange: str | None = None): return await self.get("gainers-losers", timeframe=timeframe, e=exchange)
    async def rapid_movements(self, *, type_: str | None = None, exchange: str | None = None, symbol: str | None = None): return await self.get("rapid-movements-v2", type=type_, e=exchange, symbol=symbol)
    async def funding_rates(self, symbol: str | None = None, exchange: str | None = None): return await self.get("funding-rates", symbol=symbol, e=exchange)
    async def open_interest(self, exchange: str, market_pair: str): return await self.get("open-interest", e=exchange, market_pair=market_pair)
    async def live_trades(self, exchange: str, pair: str): return await self.get("live-trades", e=exchange, pair=pair)
    async def liquidation_data(self, symbol: str): return await self.get("liquidation-data-v2", symbol=symbol)
    async def volume_flow(self, timeframe: str = "1h"): return await self.get("volume-flow", timeframe=timeframe)
    async def liquidity_lens(self, timeframe: str = "1h"): return await self.get("liquidity-lens", timeframe=timeframe)
    async def merged_orderbook(self): return await self.get("merged-orderbook")
    async def whale_trades(self, symbol: str | None = None, exchange: str | None = None): return await self.get("xtrades", symbol=symbol, e=exchange)
