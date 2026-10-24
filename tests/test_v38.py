from app.settings import Settings
from app.market_data_v38 import MarketDataFederationV38, CORE_ASSETS_V38, ASSET_ALIAS_INDEX_V38


def test_v38_registry_contains_requested_assets(tmp_path):
    st = Settings(sqlite_path=str(tmp_path / 'v38.db'), app_env='dev')
    fed = MarketDataFederationV38(str(tmp_path / 'v38.db'), st)
    symbols = {x['symbol']: x for x in fed.assets()}
    assert {'US30', 'SPX500', 'US100', 'USOIL', 'UKOIL', 'DXY', 'BTC.D', 'USDT.D'} <= set(symbols)
    assert symbols['US30']['tradingview_symbol'] == 'FXCM:US30'
    assert symbols['SPX500']['tradingview_symbol'] == 'FXCM:SPX500'
    assert symbols['US100']['provider_symbols']['fxcm'] == 'NAS100'
    assert symbols['USOIL']['tradingview_symbol'] == 'TVC:USOIL'
    assert symbols['UKOIL']['tradingview_symbol'] == 'TVC:UKOIL'
    assert symbols['DXY']['tradingview_symbol'] == 'TVC:DXY'
    assert symbols['BTC.D']['tradingview_symbol'] == 'CRYPTOCAP:BTC.D'
    assert symbols['USDT.D']['tradingview_symbol'] == 'CRYPTOCAP:USDT.D'


def test_v38_alias_resolution(tmp_path):
    st = Settings(sqlite_path=str(tmp_path / 'alias.db'), app_env='dev')
    fed = MarketDataFederationV38(str(tmp_path / 'alias.db'), st)
    assert fed.resolve('NASDAQ')['symbol'] == 'US100'
    assert fed.resolve('WTI')['symbol'] == 'USOIL'
    assert fed.resolve('BRENT')['symbol'] == 'UKOIL'
    assert fed.resolve('USDX')['symbol'] == 'DXY'
    assert fed.resolve('USDT_DOMINANCE')['symbol'] == 'USDT.D'
    assert len(CORE_ASSETS_V38) >= 20
    assert ASSET_ALIAS_INDEX_V38['DOW'].symbol == 'US30'


def test_v38_status_counts(tmp_path):
    st = Settings(sqlite_path=str(tmp_path / 'status.db'), app_env='dev')
    fed = MarketDataFederationV38(str(tmp_path / 'status.db'), st)
    status = fed.status()
    assert status['version'] == '3.8.0'
    assert status['asset_classes']['INDEX'] == 3
    assert status['asset_classes']['ENERGY'] == 2
    assert status['asset_classes']['MACRO'] == 1
    assert status['asset_classes']['DOMINANCE'] == 2
