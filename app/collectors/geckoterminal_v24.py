from __future__ import annotations

from typing import Any
from app.resilience_v25 import ProviderHealthRegistryV25, ResilientHTTPTransportV25, ResilienceConfigV25, GLOBAL_PROVIDER_HEALTH_V25


class GeckoTerminalClientV24:
    def __init__(self, base_url: str = "https://api.geckoterminal.com/api/v2", timeout: float = 20.0, transport: ResilientHTTPTransportV25 | None = None):
        self.base_url = base_url.rstrip("/")
        if transport is None:
            cfg = ResilienceConfigV25(timeout_s=timeout)
            transport = ResilientHTTPTransportV25(GLOBAL_PROVIDER_HEALTH_V25, cfg)
        self.transport = transport

    async def get(self, endpoint: str, **params: Any) -> Any:
        query = {k: v for k, v in params.items() if v is not None}
        headers = {"accept": "application/json"}
        return await self.transport.request_json("geckoterminal", "GET", f"{self.base_url}/{endpoint.strip('/')}", headers=headers, params=query)

    async def trending_pools(self, network: str) -> Any:
        return await self.get(f"/networks/{network}/trending_pools")

    async def new_pools(self, network: str) -> Any:
        return await self.get(f"/networks/{network}/new_pools")

    async def token_pools(self, network: str, token_address: str) -> Any:
        return await self.get(f"/networks/{network}/tokens/{token_address}/pools")
