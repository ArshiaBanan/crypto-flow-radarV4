def test_imports():
    from app.main import app  # noqa: F401
    from app.collectors.binance_futures import BinanceFuturesClient  # noqa: F401
    from app.collectors.cryptometer import CryptoMeterClient  # noqa: F401
    from app.collectors.livecoinwatch import LiveCoinWatchClient  # noqa: F401
