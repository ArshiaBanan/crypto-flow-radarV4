from __future__ import annotations

import asyncio

from app.intelligence_v24 import (
    ConsensusEngineV24,
    ExternalIntelligenceStoreV24,
    ExternalIntelligenceRuntimeV24,
    SourceSnapshotV24,
    normalize_external,
)
from app.collectors.coinglass_v24 import CoinGlassClientV24
from app.collectors.geckoterminal_v24 import GeckoTerminalClientV24


def test_v24_normalize_coinglass_payload():
    payload = {"code": "0", "data": [{"symbol": "SOL", "current_price": 100, "open_interest_change_24h": 4.2, "avg_funding_rate_by_oi": 0.001}]}
    rows = normalize_external("coinglass", "derivatives", "futures-coins-markets", payload, 1000)
    assert rows and rows[0]["symbol"] == "SOL" and rows[0]["open_interest_change_pct"] == 4.2


def test_v24_consensus_requires_independent_families():
    engine = ConsensusEngineV24(min_families=2)
    one = engine.evaluate("SOL", [{"symbol":"SOL","family":"derivatives","source":"coinglass","endpoint":"x","ts_ms":1000,"price_change_pct":3,"open_interest_change_pct":2}], now_ms=1000)
    assert one["direction"] == "LONG"
    assert one["family_count"] == 1
    assert any("insufficient_independent_families" in x for x in one["warnings"])
    assert one["eligible_for_scoring"] is False


def test_v24_consensus_agreement_and_disagreement():
    engine = ConsensusEngineV24(min_families=2)
    rows = [
        {"symbol":"SOL","family":"derivatives","source":"coinglass","ts_ms":1000,"price_change_pct":3,"open_interest_change_pct":2},
        {"symbol":"SOL","family":"onchain","source":"geckoterminal","ts_ms":1000,"buy_volume_usd":700,"sell_volume_usd":300},
    ]
    r = engine.evaluate("SOL", rows, now_ms=1000)
    assert r["direction"] == "LONG" and r["agreement_score"] > 70

    rows2 = [
        {"symbol":"SOL","family":"derivatives","source":"coinglass","ts_ms":1000,"price_change_pct":3},
        {"symbol":"SOL","family":"onchain","source":"geckoterminal","ts_ms":1000,"buy_volume_usd":300,"sell_volume_usd":700},
    ]
    r2 = engine.evaluate("SOL", rows2, now_ms=1000)
    assert r2["direction"] == "NEUTRAL"
    assert any("cross_source_disagreement" in x for x in r2["warnings"])


def test_v24_security_blocks_consensus():
    engine = ConsensusEngineV24()
    rows = [
        {"symbol":"SCAM","family":"derivatives","source":"coinglass","ts_ms":1000,"price_change_pct":10},
        {"symbol":"SCAM","family":"onchain","source":"geckoterminal","ts_ms":1000,"buy_volume_usd":800,"sell_volume_usd":200},
        {"symbol":"SCAM","family":"security","source":"goplus","ts_ms":1000,"malicious":1},
    ]
    r = engine.evaluate("SCAM", rows, now_ms=1000)
    assert r["direction"] == "LONG"
    assert r["blockers"]
    assert r["eligible_for_scoring"] is False


def test_v24_store_snapshot_and_consensus(tmp_path):
    store = ExternalIntelligenceStoreV24(str(tmp_path / "v24.db"))
    rows = [{"symbol":"AAA","source":"coinglass","family":"derivatives","endpoint":"x","ts_ms":1000,"price_change_pct":2}]
    sid = store.record(SourceSnapshotV24("coinglass","derivatives","x",1000,"OK",12), {"data":rows}, rows)
    assert sid
    c = ConsensusEngineV24().evaluate("AAA", rows, now_ms=1000)
    cid = store.record_consensus(c)
    assert store.recent_consensus(1)[0]["consensus_id"] == cid
    assert store.status()["snapshots"] == 1
    store.close()


def test_v24_runtime_records_and_consensus(tmp_path):
    class DummyCG:
        async def coins_markets(self):
            return {"code":"0","data":[{"symbol":"AAA","current_price":100,"open_interest_change_24h":3,"price_change_percent":4}]}
        async def liquidation_orders(self):
            return {"code":"0","data":[]}
    class DummyGT:
        async def trending_pools(self, network):
            return {"data":[{"attributes":{"base_token_symbol":"AAA","price_change_percentage":{"h1":"2"},"reserve_in_usd":"100000"}}]}
    store = ExternalIntelligenceStoreV24(str(tmp_path / "rt.db"))
    rt = ExternalIntelligenceRuntimeV24(store, coinglass=DummyCG(), gecko=DummyGT(), gecko_networks=("solana",), poll_seconds=60)
    results = asyncio.run(rt.cycle())
    assert results and results[0]["symbol"] == "AAA"
    assert store.status()["consensus"] >= 1
    store.close()


def test_v24_clients_headers_and_paths(monkeypatch):
    # Contract-level checks without network access.
    cg = CoinGlassClientV24("k")
    assert cg.base_url.endswith("coinglass.com")
    gt = GeckoTerminalClientV24()
    assert gt.base_url.endswith("/api/v2")


def test_v24_gecko_attributes_are_normalized():
    payload = {"data": [{"attributes": {"base_token_symbol": "AAA", "price_change_percentage": {"h1": "3"}, "reserve_in_usd": "120000"}}]}
    rows = normalize_external("geckoterminal", "onchain", "solana-trending-pools", payload, 1000)
    assert rows and rows[0]["symbol"] == "AAA"
    assert rows[0]["liquidity_usd"] == 120000.0


def test_v24_api_surface():
    from fastapi.testclient import TestClient
    from app.main import app, settings
    settings.data_engine_enabled = False
    settings.binance_enabled = False
    settings.v24_intelligence_enabled = False
    with TestClient(app) as client:
        assert client.get("/v24/intelligence/status").status_code == 200
        assert client.get("/v24/consensus?limit=5").status_code == 200
        assert client.post("/v24/security/token", json={"chain_id":"1", "contract_addresses":"0x1234"}).status_code == 503
