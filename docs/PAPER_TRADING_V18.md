# v1.8 — Live Paper Trading Engine

v1.8 adds a deterministic, event-driven paper execution layer. It does **not** submit real orders.

## Execution model

1. A validated `ALERT` is converted into `PaperSignal`.
2. The signal timestamp plus configured latency determines the first eligible 1-minute bar.
3. Entry slippage is applied in the adverse direction.
4. TP/SL are evaluated against subsequent bars. When TP and SL are both touched in one bar, the engine conservatively treats SL as first and flags `ambiguous_bar`.
5. Exit is TP, SL, or horizon close.
6. Exit slippage, round-trip fees, and supplied funding impact are subtracted from gross return.
7. The result records MFE, MAE, execution latency, observed bars, and model metadata.
8. Results can be persisted into `paper_trades` for later live/replay reconciliation.

## Important research rule

Paper trading must be kept separate from historical label generation. Historical data may be replayed, but the paper ledger is an execution simulation, not a source of training labels.

## Safety boundary

There is intentionally no broker/order API in this module. Transition to live trading requires a separate, explicit execution service with credentials, permissions, kill-switches, idempotency and independent risk controls.

## Live ingestion hook

The Binance 1-minute data engine now accepts an optional paper engine and feeds each completed 1m bar into it. The application exposes:

- `GET /paper/status` — paper-only account/session state.
- `GET /paper/recent` — durable paper-trade ledger.
- `POST /paper/submit` — queues a paper signal; it never sends a real exchange order.

The submit endpoint is intentionally an execution test surface. Production alert generation should call the v1.7 Fusion + Risk Gate first and only pass `ALERT` decisions into this engine.
