# Production Integration & Live Paper Validation — v3.3

v3.3 is the integration boundary between the production runtime and the long-running live-paper evidence loop.

## Production integration matrix

The runtime exposes a component matrix for:

- market data
- external enrichment
- external intelligence / consensus
- fusion + risk
- paper trading
- monitoring
- PostgreSQL
- Redis
- v3.2 dual-write

`/v33/status` reports readiness using the existing production rules. In production mode, enabled PostgreSQL/Redis/dual-write components are critical dependencies.

## Live paper validation harness

`LivePaperValidationHarnessV33` reads the existing `paper_trades` ledger and creates an immutable dataset manifest identified by a deterministic digest. Each validation run receives a unique run id.

The harness performs:

1. Cohort filtering by symbol, direction and quality tier.
2. Metadata promotion for ML/fusion fields stored inside the paper ledger metadata.
3. Temporal-integrity checks so a resolution timestamp cannot precede signal time.
4. Duplicate signal-id detection.
5. Deterministic dataset digesting.
6. Validation through the v2.1 policy engine.
7. Explicit decision states:
   - `READY_FOR_MANUAL_REVIEW`
   - `VALIDATION_FAILED`
   - `INSUFFICIENT_DATA`
   - `INVALID_DATASET`

There is no automatic model promotion.

## API

`GET /v33/status`

Returns the production integration matrix and validation-store status.

`POST /v33/validation/run`

Runs a validation cohort. Optional filters are symbol, quality tier and direction; validation thresholds can be supplied for research experiments.

`GET /v33/validation/recent`

Returns the most recent v3.3 validation runs.

## CLI

```bash
python -m app.run_validation_v33 --db data/radar.db --cohort all
```

For a narrower research cohort:

```bash
python -m app.run_validation_v33 \
  --db data/radar.db \
  --cohort btc-a \
  --symbol BTCUSDT \
  --quality-tier A \
  --direction long \
  --min-resolved 100 \
  --bootstrap-iters 2000
```

The command exits with code `0` for `READY_FOR_MANUAL_REVIEW` or `INSUFFICIENT_DATA`, and `2` for a failed or invalid dataset.

## Important limitation

v3.3 provides the repeatable evidence-collection and validation machinery. It does not create market evidence by itself and does not claim that an edge exists. Actual edge validation still requires sustained live-paper observations and resolved outcomes from the real market.
