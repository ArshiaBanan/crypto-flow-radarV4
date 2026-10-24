from __future__ import annotations

import asyncio

from app.enrichment_v23 import EnrichmentStoreV23, MultiSourceRuntimeV23, normalize_payload


def test_v23_normalization_tolerates_nested_payload():
    payload = {"data": [{"symbol": "AAAUSDT", "change_pct": "4.5", "volume": "1234", "open_interest": "99"}]}
    rows = normalize_payload("cryptometer", "gainers-losers", payload, 1000)
    assert rows == [
        {"symbol": "AAAUSDT", "source": "cryptometer", "endpoint": "gainers-losers", "ts_ms": 1000,
         "change_pct": 4.5, "volume": 1234.0, "open_interest": 99.0}
    ]


def test_v23_store_and_health(tmp_path):
    store = EnrichmentStoreV23(str(tmp_path / "e.db"))
    from app.enrichment_v23 import SourceHealth
    health = SourceHealth("test", "ep", 1000, "OK", 12)
    sid = store.record(health, {"data": [{"symbol": "AAA", "price": 10}]}, normalize_payload("test", "ep", {"data": [{"symbol": "AAA", "price": 10}]}, 1000))
    assert sid
    assert store.latest_health()[0]["status"] == "OK"
    assert store.recent_rows(5)[0]["symbol"] == "AAA"
    store.close()


def test_v23_runtime_keeps_enrichment_out_of_scoring_namespace():
    store = EnrichmentStoreV23(":memory:")
    runtime = MultiSourceRuntimeV23(store)
    runtime.latest["AAAUSDT"] = {"test:ep": {"symbol": "AAAUSDT", "price": 10.0, "change_pct": 3.0}}
    ctx = runtime.feature_context("AAAUSDT")
    assert ctx["source_enrichment"]["endpoints"]["test:ep"]["change_pct"] == 3.0
    assert "taker_imbalance" not in ctx["source_enrichment"]["endpoints"]["test:ep"]
    store.close()


class _DummyLCW:
    async def btc_volume_band(self, **kwargs):
        return [{"code": "AAA", "volume": 250, "delta": {"day": 1.10}}]

class _DummyCM:
    async def gainers_losers(self, timeframe):
        return {"success": True, "data": [{"symbol": "AAAUSDT", "change_pct": 5}]}
    async def volume_flow(self, timeframe):
        return {"success": True, "data": [{"symbol": "AAAUSDT", "flow": 1000}]}
    async def liquidity_lens(self, timeframe):
        return {"success": True, "data": [{"symbol": "AAAUSDT", "liquidity": 200000}]}
    async def merged_orderbook(self):
        return {"success": True, "data": [{"symbol": "AAAUSDT", "depth": 500000}]}


def test_v23_runtime_records_real_client_results(tmp_path):
    store = EnrichmentStoreV23(str(tmp_path / "live.db"))
    runtime = MultiSourceRuntimeV23(store, lcw=_DummyLCW(), cryptometer=_DummyCM())
    asyncio.run(runtime.lcw_cycle())
    asyncio.run(runtime.cryptometer_cycle())
    assert any(x["source"] == "livecoinwatch" and x["status"] == "OK" for x in store.latest_health())
    assert any(x["source"] == "cryptometer" and x["status"] == "OK" for x in store.latest_health())
    assert runtime.feature_context("AAAUSDT")["source_enrichment"]["endpoints"]["cryptometer:liquidity-lens"]["liquidity"] == 200000.0
    store.close()
