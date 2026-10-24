# Data Quality Policy — v0.4

The research dataset treats Binance USDⓈ-M Futures 1-minute candles as the canonical price/flow series.

## Canonical rules

- `bars_1m` contains only 1-minute candles.
- Historical Klines populate OHLC, quote volume, trade count, and taker-buy quote volume; taker-sell quote is `quote_volume - taker_buy_quote_volume`.
- 5m/15m/1h windows are derived only from complete 1m windows. A missing 1m candle invalidates that derived candle instead of silently filling it.
- OI and funding values are resolved with `timestamp <= signal_timestamp` so historical replay does not use future observations.
- Every raw payload gets a deterministic SHA-256 hash for duplicate-event auditing.
- Coverage reports are persisted in `data_quality_runs`.

## Status interpretation

- `PASS`: complete bar coverage and no duplicate raw-event groups detected in the audited window.
- `WARN`: minor coverage degradation (at least 99% coverage) or another non-fatal quality issue.
- `FAIL`: materially incomplete coverage.

A `PASS` data-quality status does not imply that the data is economically complete. Historical order-book state and liquidation flow are separate research dependencies and are not reconstructed from OHLC alone.
