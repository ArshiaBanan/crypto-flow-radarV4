# v2.4 — External Intelligence + Cross-Source Consensus

## Scope

v2.4 adds external intelligence connectors and an independent-family consensus layer. CoinGlass provides aggregated derivatives/market data, GeckoTerminal provides on-chain DEX market data, and GoPlus provides token security checks.

## Important model-safety boundary

Consensus is **context-only** in v2.4. `eligible_for_scoring` is always false until this feature group passes its own point-in-time historical research and out-of-sample validation. External sources cannot silently modify the existing Rule/ML score.

## Independent source families

- `derivatives`: CoinGlass futures market / liquidation data.
- `onchain`: GeckoTerminal DEX market data.
- `security`: GoPlus token security.

The consensus engine counts at most one vote per family to reduce double-counting multiple endpoints from the same provider. Security is a blocker/risk layer rather than a directional vote.

## Current endpoints

- `GET /v24/intelligence/status`
- `GET /v24/intelligence/rows`
- `GET /v24/consensus`
- `POST /v24/security/token`

## Research promotion rule

The consensus layer must be backtested as a standalone feature family with point-in-time joins, cost-aware outcomes, purged walk-forward evaluation, calibration checks and robustness across symbols/regimes before it may influence production scoring.
