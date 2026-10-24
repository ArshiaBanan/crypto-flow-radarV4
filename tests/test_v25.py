from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from app.resilience_v25 import (
    GLOBAL_PROVIDER_HEALTH_V25,
    ProviderHealthRegistryV25,
    ProviderUnavailable,
    ResilienceConfigV25,
    ResilientHTTPTransportV25,
    SchemaValidationError,
)


@dataclass
class FakeResponse:
    status_code: int
    payload: object
    headers: dict[str, str] | None = None
    content: bytes = b"{}"

    def __post_init__(self):
        self.headers = self.headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("error", request=httpx.Request("GET", "https://example.test"), response=self)

    def json(self):
        return self.payload


class FakeAsyncClient:
    responses: list[FakeResponse] = []
    calls = 0

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def request(self, *args, **kwargs):
        cls = type(self)
        idx = cls.calls
        cls.calls += 1
        return cls.responses[min(idx, len(cls.responses) - 1)]


@pytest.fixture(autouse=True)
def reset_global():
    GLOBAL_PROVIDER_HEALTH_V25.reset()
    yield
    GLOBAL_PROVIDER_HEALTH_V25.reset()


def make_transport(registry=None, **kwargs):
    cfg = ResilienceConfigV25(max_attempts=3, base_backoff_s=0, max_backoff_s=0, **kwargs).normalized()
    registry = registry or ProviderHealthRegistryV25(cfg)
    return ResilientHTTPTransportV25(registry, cfg, sleep_fn=lambda _: asyncio.sleep(0))


def test_v25_retries_transient_then_succeeds(monkeypatch):
    FakeAsyncClient.calls = 0
    FakeAsyncClient.responses = [FakeResponse(503, {"error": "busy"}), FakeResponse(200, {"ok": True})]
    monkeypatch.setattr("app.resilience_v25.httpx.AsyncClient", FakeAsyncClient)
    reg = ProviderHealthRegistryV25(ResilienceConfigV25(max_attempts=3, base_backoff_s=0, max_backoff_s=0))
    tr = make_transport(reg)
    out = asyncio.run(tr.request_json("demo", "GET", "https://example.test"))
    assert out == {"ok": True}
    assert FakeAsyncClient.calls == 2
    st = reg.get("demo")
    assert st["state"] == "CLOSED" and st["retry_count"] == 1 and st["successes"] == 1


def test_v25_non_retryable_4xx_fails_without_retry(monkeypatch):
    FakeAsyncClient.calls = 0
    FakeAsyncClient.responses = [FakeResponse(401, {"error": "unauthorized"}), FakeResponse(200, {"ok": True})]
    monkeypatch.setattr("app.resilience_v25.httpx.AsyncClient", FakeAsyncClient)
    tr = make_transport()
    with pytest.raises(Exception):
        asyncio.run(tr.request_json("demo", "GET", "https://example.test"))
    assert FakeAsyncClient.calls == 1


def test_v25_circuit_opens_after_consecutive_failures(monkeypatch):
    FakeAsyncClient.calls = 0
    FakeAsyncClient.responses = [FakeResponse(500, {"e": 1})]
    monkeypatch.setattr("app.resilience_v25.httpx.AsyncClient", FakeAsyncClient)
    cfg = ResilienceConfigV25(max_attempts=1, circuit_failure_threshold=2, circuit_open_seconds=60)
    reg = ProviderHealthRegistryV25(cfg)
    tr = ResilientHTTPTransportV25(reg, cfg, sleep_fn=lambda _: asyncio.sleep(0))
    for _ in range(2):
        with pytest.raises(Exception):
            asyncio.run(tr.request_json("demo", "GET", "https://example.test"))
    assert reg.get("demo")["state"] == "OPEN"
    with pytest.raises(ProviderUnavailable):
        asyncio.run(tr.request_json("demo", "GET", "https://example.test"))
    assert FakeAsyncClient.calls == 2


def test_v25_half_open_recovers(monkeypatch):
    FakeAsyncClient.calls = 0
    FakeAsyncClient.responses = [FakeResponse(500, {"e": 1}), FakeResponse(200, {"ok": True})]
    monkeypatch.setattr("app.resilience_v25.httpx.AsyncClient", FakeAsyncClient)
    now = [1000]
    cfg = ResilienceConfigV25(max_attempts=1, circuit_failure_threshold=1, circuit_open_seconds=1)
    reg = ProviderHealthRegistryV25(cfg, clock=lambda: now[0])
    tr = ResilientHTTPTransportV25(reg, cfg, sleep_fn=lambda _: asyncio.sleep(0))
    with pytest.raises(Exception):
        asyncio.run(tr.request_json("demo", "GET", "https://example.test"))
    assert reg.get("demo")["state"] == "OPEN"
    now[0] += 1001
    out = asyncio.run(tr.request_json("demo", "GET", "https://example.test"))
    assert out == {"ok": True}
    assert reg.get("demo")["state"] == "CLOSED"


def test_v25_schema_validation_rejects_wrong_type(monkeypatch):
    FakeAsyncClient.calls = 0
    FakeAsyncClient.responses = [FakeResponse(200, "not-json-object")]
    monkeypatch.setattr("app.resilience_v25.httpx.AsyncClient", FakeAsyncClient)
    tr = make_transport()
    with pytest.raises(SchemaValidationError):
        asyncio.run(tr.request_json("demo", "GET", "https://example.test", expected_types=(dict,)))
    assert tr.registry.get("demo")["state"] == "CLOSED" or tr.registry.get("demo")["state"] == "OPEN"


def test_v25_response_size_is_blocked(monkeypatch):
    FakeAsyncClient.calls = 0
    FakeAsyncClient.responses = [FakeResponse(200, {"ok": True}, content=b"x" * 2000)]
    monkeypatch.setattr("app.resilience_v25.httpx.AsyncClient", FakeAsyncClient)
    tr = make_transport(max_response_bytes=1024)
    with pytest.raises(SchemaValidationError):
        asyncio.run(tr.request_json("demo", "GET", "https://example.test"))


def test_v25_global_registry_is_shared_by_default(monkeypatch):
    FakeAsyncClient.calls = 0
    FakeAsyncClient.responses = [FakeResponse(200, []), FakeResponse(200, {})]
    monkeypatch.setattr("app.resilience_v25.httpx.AsyncClient", FakeAsyncClient)
    from app.collectors.geckoterminal_v24 import GeckoTerminalClientV24
    from app.collectors.coinglass_v24 import CoinGlassClientV24
    a = GeckoTerminalClientV24()
    b = CoinGlassClientV24("k")
    assert a.transport.registry is b.transport.registry is GLOBAL_PROVIDER_HEALTH_V25


def test_v25_api_surface(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app, settings
    settings.data_engine_enabled = False
    settings.binance_enabled = False
    settings.v24_intelligence_enabled = False
    with TestClient(app) as client:
        r = client.get("/v25/resilience/status")
        assert r.status_code == 200
        data = r.json()
        assert data["version"] == "2.5.0"
        assert "providers" in data
        r2 = client.get("/health")
        assert r2.status_code == 200
        assert "resilience" in r2.json()


def test_v25_admin_mutation_fails_closed_when_key_unconfigured():
    from app.api_v20 import require_admin_key
    with pytest.raises(Exception) as exc:
        require_admin_key(None, None)
    assert getattr(exc.value, "status_code", None) == 503


def test_v25_transport_adds_stable_request_fingerprint(monkeypatch):
    FakeAsyncClient.calls = 0
    FakeAsyncClient.responses = [FakeResponse(200, {"ok": True})]
    seen = {}

    async def request(self, *args, **kwargs):
        seen["headers"] = kwargs.get("headers")
        return FakeResponse(200, {"ok": True})

    monkeypatch.setattr("app.resilience_v25.httpx.AsyncClient", lambda *a, **k: _ClientCtx(request))
    tr = make_transport()
    asyncio.run(tr.request_json("demo", "GET", "https://example.test", params={"a": 1}))
    first = seen["headers"]["X-CFR-Request-ID"]
    asyncio.run(tr.request_json("demo", "GET", "https://example.test", params={"a": 1}))
    second = seen["headers"]["X-CFR-Request-ID"]
    assert first == second and len(first) == 24


class _ClientCtx:
    def __init__(self, fn):
        self.fn = fn
    async def __aenter__(self):
        self.request = self.fn.__get__(self, type(self)) if hasattr(self.fn, '__get__') else self.fn
        return self
    async def __aexit__(self, exc_type, exc, tb):
        return False
