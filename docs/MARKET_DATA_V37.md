# Crypto Flow Radar v3.7 — Multi-Asset Market & On-Chain Data Hub

v3.7 expands the terminal from crypto-only intelligence into a normalized multi-asset market layer.

## Asset classes

- Crypto: Binance Spot/Futures ecosystem, CoinMarketCap aggregate context, Bybit, OKX, Coinbase and Kraken reference fan-out for core assets.
- Forex: EURUSD, GBPUSD, USDCHF, USDCAD, AUDUSD, NZDUSD, USDJPY.
- Metals: XAUUSD, XAGUSD, XCUUSD.

## TradingView normalization

Every registry row has a `tradingview_symbol` such as `BINANCE:BTCUSDT`, `OANDA:EURUSD`, `OANDA:XAUUSD`, `OANDA:XAGUSD`, and `OANDA:XCUUSD`.

TradingView is used as the chart/reference layer. The backend does not scrape TradingView for quotes. The official TradingView Datafeed API is designed to connect your own or third-party market-data feed to the chart runtime.

## Crypto coverage

The Binance snapshot endpoint is used to populate a broad USDT crypto universe. Core assets receive cross-exchange fan-out. CoinMarketCap is kept as an independent market-cap/volume context layer.

## On-chain coverage

Optional adapters are wired for:

- CoinGlass derivatives / liquidations / open interest
- Arkham entity-labelled transfers and address-intelligence updates
- Etherscan API V2 for Ethereum and BNB Smart Chain (`chainid=1` and `chainid=56`)
- GoPlus token security
- GeckoTerminal DEX / pool liquidity context

External intelligence remains source-aware and is not silently promoted into the main model score without out-of-sample validation.

## OANDA / FXCM

OANDA v20 pricing is wired for authenticated account pricing and uses OANDA-native instrument identifiers (`EUR_USD`, `XAU_USD`, `XAG_USD`, `XCU_USD`).

FXCM is represented with official broker symbol normalization and a dedicated FCLite/ForexConnect gateway status. The adapter deliberately does not invent undocumented quote REST paths; live FXCM pricing requires the broker's supported API/session credentials and integration method.
