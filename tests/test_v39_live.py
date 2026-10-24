from fastapi.testclient import TestClient

from app.storage.in_memory import InMemoryStore


def test_live_quote_cache_roundtrip():
    s = InMemoryStore()
    s.upsert_live_quote("BTCUSDT", {"symbol": "btcusdt", "price": 123.45, "ts_ms": 100})
    rows = s.live_quote_snapshot()
    assert rows == [{"symbol": "btcusdt", "price": 123.45, "ts_ms": 100}]


def test_live_http_quote_endpoint():
    from app.main import app, settings, store
    settings.data_engine_enabled = False
    settings.binance_enabled = False
    store.live_quotes.clear()
    store.upsert_live_quote("BTCUSDT", {"symbol": "btcusdt", "price": 123.45, "ts_ms": 100, "source": "test"})
    with TestClient(app) as client:
        r = client.get("/api/live/quotes")
        assert r.status_code == 200
        assert r.json()["rows"][0]["price"] == 123.45


def test_live_websocket_surface():
    from app.main import app, settings, store
    settings.data_engine_enabled = False
    settings.binance_enabled = False
    store.live_quotes.clear()
    store.upsert_live_quote("BTCUSDT", {"symbol": "btcusdt", "price": 123.45, "ts_ms": 100, "source": "test"})
    with TestClient(app) as client:
        with client.websocket_connect("/ws/live") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "market_tick"
            assert msg["source"] == "binance_futures_ws"
            assert msg["quotes"][0]["price"] == 123.45
