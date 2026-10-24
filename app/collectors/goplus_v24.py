from __future__ import annotations

from typing import Any
from app.resilience_v25 import ProviderHealthRegistryV25, ResilientHTTPTransportV25, ResilienceConfigV25, GLOBAL_PROVIDER_HEALTH_V25


class GoPlusClientV24:
    def __init__(self, api_key: str | None = None, base_url: str = "https://api.gopluslabs.io", timeout: float = 20.0, transport: ResilientHTTPTransportV25 | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        if transport is None:
            cfg = ResilienceConfigV25(timeout_s=timeout)
            transport = ResilientHTTPTransportV25(GLOBAL_PROVIDER_HEALTH_V25, cfg)
        self.transport = transport

    async def token_security(self, chain_id: str, contract_addresses: str) -> Any:
        headers = {"accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = await self.transport.request_json("goplus", "GET", f"{self.base_url}/api/v1/token_security/{chain_id}", headers=headers, params={"contract_addresses": contract_addresses})
        if isinstance(data, dict) and str(data.get("code")) not in {"1", "0", "None", ""}:
            raise RuntimeError(f"Unexpected GoPlus response: {data!r}")
        return data
