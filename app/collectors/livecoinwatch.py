from __future__ import annotations

import httpx
from typing import Any
from app.resilience_v25 import ProviderHealthRegistryV25, ResilientHTTPTransportV25, ResilienceConfigV25, GLOBAL_PROVIDER_HEALTH_V25

LCW_URL = "https://api.livecoinwatch.com/coins/list"


class LiveCoinWatchClient:
    def __init__(self, api_key: str, timeout: float = 20.0, transport: ResilientHTTPTransportV25 | None = None):
        if not api_key:
            raise ValueError("LiveCoinWatch API key is required")
        self.api_key = api_key
        cfg = ResilienceConfigV25(timeout_s=timeout)
        self.transport = transport or ResilientHTTPTransportV25(GLOBAL_PROVIDER_HEALTH_V25, cfg)

    async def coins_page(self, *, currency: str = "BTC", sort: str = "volume", order: str = "descending", offset: int = 0, limit: int = 100, meta: bool = False) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError("LiveCoinWatch limit must be between 1 and 100")
        payload = {"currency": currency, "sort": sort, "order": order, "offset": offset, "limit": limit, "meta": meta}
        data = await self.transport.request_json("livecoinwatch", "POST", LCW_URL, headers={"content-type": "application/json", "x-api-key": self.api_key}, json=payload, idempotent=True, expected_types=(list,))
        return data

    async def btc_volume_band(self, *, min_btc: float, max_btc: float, max_pages: int = 25) -> list[dict[str, Any]]:
        if min_btc > max_btc:
            raise ValueError("min_btc must be <= max_btc")
        out: list[dict[str, Any]] = []
        offset = 0
        for _ in range(max_pages):
            rows = await self.coins_page(currency="BTC", sort="volume", order="descending", offset=offset, limit=100, meta=False)
            if not rows:
                break
            out.extend(r for r in rows if min_btc <= float(r.get("volume", 0)) <= max_btc)
            last_volume = float(rows[-1].get("volume", 0))
            if last_volume < min_btc:
                break
            offset += len(rows)
        return out

    @staticmethod
    def rank_movers(rows: list[dict[str, Any]], field: str = "day") -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        def pct(row: dict[str, Any]) -> float:
            raw = float(row.get("delta", {}).get(field, 1.0))
            return (raw - 1.0) * 100.0
        enriched = []
        for row in rows:
            x = dict(row); x["change_pct"] = pct(row); enriched.append(x)
        gainers = sorted((r for r in enriched if r["change_pct"] > 0), key=lambda r: r["change_pct"], reverse=True)
        losers = sorted((r for r in enriched if r["change_pct"] < 0), key=lambda r: r["change_pct"])
        return gainers, losers
