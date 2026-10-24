from __future__ import annotations

from typing import Any
from app.resilience_v25 import ProviderHealthRegistryV25, ResilientHTTPTransportV25, ResilienceConfigV25, GLOBAL_PROVIDER_HEALTH_V25


class CoinGlassClientV24:
    def __init__(self, api_key: str, base_url: str = "https://open-api-v4.coinglass.com", timeout: float = 20.0, transport: ResilientHTTPTransportV25 | None = None):
        if not api_key:
            raise ValueError("CoinGlass API key is required")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        if transport is None:
            cfg = ResilienceConfigV25(timeout_s=timeout)
            transport = ResilientHTTPTransportV25(GLOBAL_PROVIDER_HEALTH_V25, cfg)
        self.transport = transport

    async def get(self, endpoint: str, **params: Any) -> Any:
        query = {k: v for k, v in params.items() if v is not None}
        headers = {"accept": "application/json", "CG-API-KEY": self.api_key}
        data = await self.transport.request_json("coinglass", "GET", f"{self.base_url}/{endpoint.strip('/')}", headers=headers, params=query)
        if isinstance(data, dict) and str(data.get("code")) not in {"0", "None", ""}:
            raise RuntimeError(f"Unexpected CoinGlass response: {data!r}")
        return data

    async def coins_markets(self) -> Any:
        return await self.get("/api/futures/coins-markets")

    async def liquidation_orders(self) -> Any:
        return await self.get("/api/futures/liquidation/order")

    async def spot_netflow(self, symbol: str, exchange_list: str = "Binance,Bybit,OKX,Bitget,Gate") -> Any:
        return await self.get("/api/spot/coin-netflow", symbol=symbol.upper(), exchange_list=exchange_list)

    async def open_interest_history(self, symbol: str, interval: str = "1h") -> Any:
        return await self.get("/api/futures/open-interest/history", symbol=symbol.upper(), interval=interval)
