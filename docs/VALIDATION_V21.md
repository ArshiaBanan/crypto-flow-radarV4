# Crypto Flow Radar v2.1 — Live Validation & Promotion Gate

v2.1 adds a controlled research-to-production validation layer on top of the v2.0 alert and paper-trading pipeline. It never claims that paper performance is live profitability, and it never auto-promotes a model.

## What is validated

Validation operates on resolved `paper_trades` outcomes and can be scoped by symbol and quality tier. Metrics include:

- resolved sample count and data sufficiency;
- net expectancy in R;
- deterministic bootstrap confidence interval for expectancy;
- hit rate;
- sequence max drawdown;
- ambiguous-bar rate;
- no-data rate;
- ML calibration Brier score when probabilities are present.

## Promotion policy

A model can enter `CANDIDATE` only when the supplied validation report has status `PASS`. Promotion to `PROMOTED` is a separate, authenticated manual action. This separates statistical eligibility from operational approval.

The system deliberately supports `INSUFFICIENT_DATA` rather than filling missing outcomes or manufacturing confidence intervals.

## Endpoints

- `POST /v21/validation/paper` — build and persist a validation report.
- `GET /v21/validation/recent` — retrieve recent reports.
- `GET /v21/promotions` — retrieve model promotion decisions.
- `POST /v21/models/{model_id}/request-promotion` — create a candidate after a PASS report.
- `POST /v21/models/{model_id}/promote` — manual controlled promotion.
- `POST /v21/models/{model_id}/reject` — manual rejection.

## Important limitation

Unit/integration tests use controlled synthetic observations. They verify implementation correctness, not trading performance. Real market validation still requires accumulating sufficient live-paper observations across symbols, regimes, liquidity states, and time periods before any production model decision.
