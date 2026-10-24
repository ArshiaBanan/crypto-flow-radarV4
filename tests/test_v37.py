
from pathlib import Path

def test_v37_asset_registry_and_tv_mapping(tmp_path):
    from app.settings import Settings
    from app.market_data_v37 import MarketDataFederationV37
    st=Settings(sqlite_path=str(tmp_path/"m.db"), app_env="dev")
    fed=MarketDataFederationV37(str(tmp_path/"m.db"), st)
    assets=fed.assets()
    symbols={a["symbol"]:a for a in assets}
    assert {"BTCUSDT","ETHUSDT","EURUSD","GBPUSD","USDCHF","USDCAD","AUDUSD","NZDUSD","USDJPY","XAUUSD","XAGUSD","XCUUSD"} <= set(symbols)
    assert symbols["BTCUSDT"]["tradingview_symbol"] == "BINANCE:BTCUSDT"
    assert symbols["XAUUSD"]["tradingview_symbol"] == "OANDA:XAUUSD"
    assert symbols["XCUUSD"]["provider_symbols"]["fxcm"] == "Copper"


def test_v37_market_store_latest(tmp_path):
    from app.market_data_v37 import MarketDataStoreV37
    store=MarketDataStoreV37(str(tmp_path/"q.db"))
    store.upsert("crypto.btcusdt","binance","BTCUSDT",price=100000,change_24h_pct=2.5,volume_24h=1_000_000,ts_ms=1000,raw={"lastPrice":"100000"})
    rows=store.latest("CRYPTO")
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["provider"] == "binance"
