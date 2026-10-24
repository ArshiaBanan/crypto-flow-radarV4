# Crypto Flow Radar v3.9 — Online Live Terminal

- Binance USDⓈ-M Futures WebSocket is the real-time scanner feed.
- Every `aggTrade` updates the live quote cache immediately.
- Completed 1m bars execute the existing feature → scoring → fusion → risk → alert pipeline.
- Frontend uses `/ws/live` for 1-second quote updates and slower REST polling for non-streaming panels.
- TradingView is the chart/reference layer. Its official docs state that Advanced Charts does not provide market data; a Datafeed must be connected to an own/third-party source.

## Production
Set `APP_ENV=production`, PostgreSQL/Redis URLs, `V30_REQUIRE_POSTGRES=true`, `V30_REQUIRE_REDIS=true`, `V32_ENABLED=true`, and `V32_DUALWRITE_MODE=required`.

## Railway
Railway can build the root `Dockerfile`; the service listens on `$PORT`. Configure secrets as Railway Variables and generate a public domain.

## Alert start
With `DATA_ENGINE_ENABLED=true` and `BINANCE_ENABLED=true`, the Binance live engine starts during application lifespan. `ALERT` still requires the existing Fusion/Risk Gate; the first scored live signal is available after a completed 1m bar and adequate feature context.
