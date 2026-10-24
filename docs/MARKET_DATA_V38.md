# Market Data v3.8

v3.8 extends the existing market federation with:

- US30 / Dow Jones
- SPX500 / S&P 500
- US100 / NASDAQ-100 (native FXCM symbol: NAS100)
- USOIL / WTI
- UKOIL / Brent
- DXY
- BTC.D / Bitcoin Dominance
- USDT.D / Tether Dominance

## Provider policy

The registry separates:

1. canonical product symbol used by Crypto Flow Radar,
2. exact TradingView reference symbol,
3. broker-native/provider-native symbol,
4. the live-data source actually used by the backend.

TradingView is a chart/reference layer; the backend does not scrape TradingView. OANDA is the primary live broker adapter for the extended index/energy set when credentials are configured. FXCM symbols are retained as a verified provider mapping and can be activated through the existing ForexConnect/FCLite gateway.

DXY uses Trading Economics as the configurable live-data provider. BTC.D is read from CoinMarketCap global metrics. USDT.D is derived as USDT market cap / total crypto market cap from the same CMC snapshot and is explicitly marked as a derived metric.

## Current TradingView references

- US30: `FXCM:US30`
- SPX500: `FXCM:SPX500`
- US100: `FXCM:NAS100`
- USOIL: `TVC:USOIL`
- UKOIL: `TVC:UKOIL`
- DXY: `TVC:DXY`
- BTC.D: `CRYPTOCAP:BTC.D`
- USDT.D: `CRYPTOCAP:USDT.D`

## OANDA mapping

OANDA's regional/platform identifiers vary by division. v3.8 therefore makes the API identifiers configurable through `.env` and keeps provider-native display aliases separately. The shipped defaults are:

- `US30_USD`
- `SPX500_USD`
- `NAS100_USD`
- `WTICO_USD`
- `BCO_USD`

Change these variables when the account division uses a different server-side instrument code.
