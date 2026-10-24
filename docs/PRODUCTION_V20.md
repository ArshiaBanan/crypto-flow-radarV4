# Crypto Flow Radar v2.0 — Production Foundation

v2.0 unifies the previously separated research/runtime components into one auditable live pipeline:

`Binance 1m -> Features -> Anomaly -> Rule Score -> Fusion -> Risk Gate -> Alert Ledger -> Telegram (optional) -> Paper Trading -> Monitoring/Drift`

## Safety model

- No real-order execution endpoint exists in v2.0.
- Monitoring quarantine blocks new paper submissions through the existing v1.9 safety gate.
- The v2.0 pause state is a separate kill switch for the alert pipeline.
- Management endpoints accept `X-Admin-Key` when `ADMIN_API_KEY` is configured.
- Telegram delivery is opt-in and failure is recorded in the alert ledger.

## Durable audit

SQLite remains the default development/portable backend. v2.0 creates:

- `alert_events_v20`
- `system_state_v20`
- `model_registry_v20`
- `runtime_heartbeats_v20`

The Docker Compose file includes Redis and PostgreSQL for the next persistence migration, but the default application path intentionally remains SQLite until a full adapter is validated.

## Alert identity

Alert IDs are deterministic SHA-256-derived identifiers from version, symbol, direction and event timestamp. Duplicate inserts are ignored by the primary key.

## Correlation control

The live pipeline can compare the current symbol's recent 1m return series against active alert symbols. A configurable absolute Pearson-correlation threshold is used to estimate correlated exposure; insufficient samples result in no inferred correlation rather than a fabricated number.

## Model registry

Models can be registered with name, version, path, SHA-256 and metadata. v2.0 does not automatically promote a newly registered model to production inference; model promotion remains a controlled research/validation decision.

## Environment

See `.env.example` for v2.0 controls, including:

- risk/cooldown parameters;
- correlation thresholds;
- admin API key;
- Telegram credentials;
- paper-auto-submit behavior.

## Operational endpoints

- `GET /health`
- `GET /v20/status`
- `GET /v20/alerts`
- `GET /v20/alerts/{alert_id}`
- `GET /v20/models`
- `POST /v20/admin/pause`
- `POST /v20/models/register`

The frontend is served from `/` and polls system status/alerts without requiring a separate web application in development.
