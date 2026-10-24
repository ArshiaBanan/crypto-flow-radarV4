# Crypto Flow Radar v3.8 — Professional Multi-Asset Intelligence Terminal

v3.7 extends the project into a normalized multi-asset market-data layer while keeping the existing signal, risk, paper-validation, monitoring and SaaS stacks.

## What's new

- Free / Pro / Elite only; Legendary removed and legacy Legendary records normalize to Elite.
- Purple / blue / red trading-room UI with a To The Moon rocket banner.
- TradingView chart/reference layer with exact provider-style symbol mapping.
- Crypto market hub: Binance, CoinMarketCap, Bybit, OKX, Coinbase, Kraken.
- Forex: EURUSD, GBPUSD, USDCHF, USDCAD, AUDUSD, NZDUSD, USDJPY.
- Metals: XAUUSD, XAGUSD, XCUUSD through OANDA / FXCM symbol mappings.
- On-chain hub: CoinGlass, Arkham, Etherscan/BscScan V2-compatible access, GoPlus, GeckoTerminal.
- Broad Binance USDT crypto-universe cache plus core cross-exchange fan-out.
- Source-aware provider status and persistence in `market_*_v37` tables.

## Important data-source design

TradingView is not scraped for backend prices. TradingView's official Datafeed API is a chart integration layer; CFR uses provider-native market feeds and maps them to TradingView symbols for charts/reference.

FXCM exposes live/historical market data through supported FIX, Java and ForexConnect APIs. CFR does not fabricate an undocumented REST quote endpoint; the broker session/gateway must be configured with real credentials.

## v3.7 API

- `GET /v37/assets`
- `GET /v37/market/overview?asset_class=ALL&refresh=true`
- `POST /v37/market/refresh?asset_class=ALL`
- `GET /v37/market/status`
- `GET /v37/crypto/universe?limit=200`
- `GET /v37/tradingview/symbol?symbol=BTCUSDT`
- `GET /v37/onchain/overview`
- `GET /v37/onchain/live`

## Local run

```powershell
cd "C:\path\cfr_v37_release"
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
pytest -q
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open:

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/docs`
- `http://127.0.0.1:8000/v37/market/status`

## Optional credentials

```env
V37_CMC_API_KEY=
V37_OANDA_ENABLED=false
V37_OANDA_API_TOKEN=
V37_OANDA_ACCOUNT_ID=
V37_FXCM_ENABLED=false
V37_FXCM_USERNAME=
V37_FXCM_PASSWORD=
V37_FXCM_TRADING_SESSION_ID=FXCM
V37_FXCM_TRADING_SESSION_SUB_ID=
V37_ARKHAM_API_KEY=
V37_ETHERSCAN_API_KEY=
```

Public Binance spot market-data requests can run without private trading credentials. OANDA pricing requires an authenticated v20 trading account. CoinGlass, Arkham and Etherscan require their respective API access/keys. FXCM's supported API/session mechanism must be provisioned separately.

## Validation status

The release is software-validated with the full test suite and offline boot checks. Live third-party account/API authentication is not claimed unless run with real credentials in the target deployment environment.


## v3.8 Extended Global Market Context

v3.8 adds US30, SPX500, US100/NAS100, WTI/USOIL, Brent/UKOIL, DXY, BTC.D and USDT.D to the Market Data Federation.

The registry keeps canonical symbols, provider-native symbols, and TradingView reference symbols separate. DXY uses an optional Trading Economics feed. BTC.D comes from CoinMarketCap global metrics; USDT.D is explicitly derived from CoinMarketCap total market cap and USDT market cap.

For OANDA, extended index and oil API identifiers are configurable because instrument codes differ across divisions. FXCM mappings are verified provider symbols and the existing ForexConnect/FCLite integration remains the execution/data gateway rather than an invented REST interface.

## v3.9 — Online Live Terminal
The frontend is now wired to a same-origin `/ws/live` stream. The backend pushes every Binance futures `aggTrade` into an in-memory live quote cache immediately; completed 1-minute bars continue through the existing feature/scoring/fusion/risk/alert pipeline. Non-streaming panels use slower REST refreshes so the UI does not overload provider APIs.

TradingView is embedded as the chart/reference surface. TradingView's official documentation states that its chart libraries do not contain market data; a datafeed must be connected to an own/third-party provider. CFR therefore treats Binance/OANDA/FXCM/etc. as the raw-data layer and TradingView as the chart/reference layer.

### Online deployment
Use `.env.online.example` as a production starting point. The included `Dockerfile` listens on `$PORT`, exposes `/health`, and is ready for Railway-style deployment. A public domain must be generated in the hosting provider's networking layer. Do not place provider API secrets in frontend code.


## v4.0 detector integration
The web terminal now includes the uploaded multi-source detector under `pump_dump_detector/`. See `docs/V40_INTEGRATION_AND_COMPARISON.md` for the new `/v40/detector/*` API, Railway environment settings, limitations, and an honest comparison of both scoring approaches. Set `V40_ENABLED=true` to run the detector. It is disabled by default to avoid unexpectedly starting a high-volume scan before configuration.
