# v3.1 — Production Core Storage

The canonical market-data store can now run on PostgreSQL through `app.storage.postgres_v31.PostgreSQLStoreV31`.

## Controlled migration

```bash
python -m app.migrate_v31 \
  --sqlite data/radar.db \
  --postgres "$POSTGRES_URL"
```

The migration is intentionally destructive on the *target* schema only: it truncates the seven core market-data tables before loading the frozen SQLite snapshot. It verifies row counts and deterministic SHA-256 row hashes before reporting `VERIFIED`.

Freeze writers during the migration window. Do not treat a successful copy as proof that the source and target remained in sync after the snapshot was taken.

## Cutover policy

Set:

```env
STORAGE_BACKEND=postgres
POSTGRES_URL=postgresql://...
V30_REQUIRE_POSTGRES=true
```

The backend factory fails closed when PostgreSQL is unavailable or its driver is missing. It never silently falls back to SQLite.

## Scope

v3.1 migrates the canonical market-data store used by the live Binance data engine:

- `raw_events`
- `oi_samples`
- `bars_1m`
- `funding_samples`
- `data_quality_runs`
- `feature_snapshots`
- `signal_episodes`

The specialized audit ledgers remain on their existing implementations for now. They are not claimed to be PostgreSQL-migrated until their individual schemas, transactions and reconciliation have been validated.
