# v4.0 — Pump/Dump Detector integration and honest comparison

## What changed
- The existing Crypto Flow Radar frontend is preserved.
- The uploaded `pump_dump_detector` project is included as the primary detector for the Radar data view.
- The detector runs in an isolated subprocess so its upstream modules and SQLite schema do not collide with the SaaS backend.
- New API routes: `GET /v40/detector/status`, `GET /v40/detector/results`, `POST /v40/detector/run`.
- When detector results exist, `GET /scanner/snapshot` supplies normalized detector rows to the existing Radar UI. Otherwise it continues to show native engine rows.
- Optional environment-based API keys are supported. Unconfigured sources are skipped rather than fabricated.
- The uploaded detector's original README and Quickstart are preserved as `README_UPSTREAM.md` and `QUICKSTART_UPSTREAM.md`.

## Enable on Railway
Set `V40_ENABLED=true`. Optional settings:
- `V40_SCAN_INTERVAL_SECONDS=900` (minimum 300 seconds)
- `V40_SCAN_TIMEOUT_SECONDS=900`
- `V40_SCAN_INTERVAL_MINUTES=15`

Then redeploy. The first scan starts when the app starts. A scan can also be requested with `POST /v40/detector/run`. Results are exposed at `GET /v40/detector/results` and the status at `GET /v40/detector/status`.

Optional provider variables: `LCW_API_KEY`, `CRYPTOMETER_API_KEY`, `ETHERSCAN_API_KEY`, `SOLSCAN_API_KEY`, `COINGECKO_API_KEY`, `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_MONITORED_CHANNELS`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. Leave unconfigured keys blank. Telegram user-session monitoring may require an interactive first login and should not be enabled on a headless deployment until its session has been initialized securely.

## Which detector is better?
This is a code/architecture comparison, **not proof of higher signal accuracy**. Neither project's test suite establishes live profitability.

### Uploaded detector strengths
- Uses independent external, social and on-chain families in addition to Binance market data.
- Explicit `CONFIRMED / WATCH / LOW_CONFIDENCE` confluence tier.
- Has a wash-trading suspicion guard and cross-exchange confirmation.
- Includes trade-count abnormality (`RUSH_Z`) and taker-buy ratio, potentially useful for early activity detection.
- Has a local outcome/backtest utility and a feature-lift research command.

### Uploaded detector limitations found in code review
- Its score is a heuristic weighted average with missing-signal weights renormalized. This is reasonable for availability, but can make a score from one available signal look comparable to a score confirmed by many sources unless the confluence tier is respected.
- `direction_label` is a small vote heuristic, not a calibrated directional probability.
- Default candle timeframe is `1h`, while the scan interval is `15m`; repeatedly evaluating the currently forming hourly candle can distort volume/trade-count z-scores. Treat `RUSH_Z` as an activity anomaly, not a proven pump predictor.
- The detector's default watch-wallet sample is narrow; exchange-wallet inflow/outflow does not by itself establish buy/sell intent.
- Social ticker mentions and funding are noisy proxies. A high composite score is not a probability of success.
- The scanner's per-symbol sequential API calls may be slow/rate-limited for a large universe.

### Previous Crypto Flow Radar strengths
- Stream-oriented Binance Futures ingestion and 1-minute aggregation.
- Anomaly, liquidity, risk gate, paper-trading, observation ledger, drift monitoring, walk-forward research and manual model-promotion controls.
- Production-oriented health/readiness and storage integration.

### Previous Crypto Flow Radar limitations
- The native score/model is also not proven profitable merely because tests pass. External enrichment was not originally a fully validated substitute for the detector's independent social/on-chain families.
- Live exchange connectivity, PostgreSQL/Redis integration and provider credentials must be verified in the actual deployment; local unit tests cannot establish that.

## Recommendation
Use the uploaded detector as the **candidate-discovery/radar engine**, while retaining the existing paper-trading, audit, risk and validation infrastructure as safety layers. Do not merge the two scores into a new blended score until a point-in-time, chronological out-of-sample comparison has been run on the same symbols, timestamps, entry/exit rules, fees, slippage and funding. Track net expectancy, precision at top-K, false-alert rate, drawdown and stability by market regime. Promote only after enough resolved live-paper observations.

## Important integration boundary
The Radar snapshot/UI uses the uploaded detector's results when available. The legacy live-stream engine and its existing production alert pipeline remain available for paper/risk infrastructure; this release does not claim that the two scoring engines have been proven equivalent or that every alert subsystem now emits only the uploaded detector's calls. Validate in paper/observation mode before enabling paid alerts.
