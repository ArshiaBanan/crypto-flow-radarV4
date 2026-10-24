# v3.0 — Production Deployment & Observability

v3.0 operationalizes the radar without claiming that the trading edge is proven.

## Added

- Dependency-free JSON structured logging.
- Request correlation via `X-Request-ID` and stable HTTP metrics.
- Prometheus-compatible `/v30/metrics` endpoint.
- Runtime lifecycle state and kill switch.
- Readiness endpoint that distinguishes development readiness from production readiness.
- SQLite integrity probe.
- Optional PostgreSQL connectivity probe using `POSTGRES_URL`.
- Optional Redis connectivity probe and distributed runtime lease.
- Production gate requiring an admin key when `APP_ENV=prod`/`production`.
- Docker image runs as a non-root user and uses one Uvicorn worker because the current data-engine process is stateful.
- Compose services for PostgreSQL and Redis with health checks.

## Production safety contract

The current core stores still use SQLite. Therefore `V30_REQUIRE_POSTGRES=true` intentionally makes `/v30/readiness` return not-ready until the core storage backend is migrated to PostgreSQL. This is deliberate; a connectivity probe is not equivalent to a production storage migration.

Redis can be used as a distributed runtime lease to prevent two application instances from running the stateful market engine at the same time. The lease is optional in development.

## Endpoints

- `GET /v30/runtime`
- `GET /v30/readiness`
- `GET /v30/storage`
- `GET /v30/metrics`
- `POST /v30/kill-switch`

Admin mutation requires `X-Admin-Key` and fails closed when no key is configured.
