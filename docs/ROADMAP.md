# Roadmap

## v0.1 — foundation
- Multi-source client interfaces
- LCW BTC volume-band universe and mover ranking
- CryptoMeter connector surface
- Binance websocket foundation
- Transparent scoring function
- FastAPI skeleton
- Unit tests

## v0.2 — live feature engine
- Persist ticks/trades/order book deltas
- 1m/5m/15m/1h feature windows
- RVOL and volume z-score
- taker imbalance
- OI change
- funding normalization
- liquidation imbalance
- order book imbalance
- signal event table

## v0.3 — database + backtest
- PostgreSQL + TimescaleDB schema
- Redis cache
- event provenance and raw payload archive
- walk-forward backtest
- MAE/MFE and hit-rate metrics
- score calibration

## v0.4 — data quality + historical acquisition
- Top Pump
- Top Dump
- Early candidates
- Squeeze radar
- Coin detail page
- signal timeline

## v0.5 — point-in-time replay + event engine
- Nansen smart-money/on-chain
- CoinGlass cross-exchange OI/liquidation/funding
- GoPlus token security
- DEX liquidity and pool data

## v0.6 — regime + robustness research
- XGBoost/LightGBM baseline
- probability of target-before-stop labels
- out-of-sample evaluation
- probability calibration


## Current status

v0.4 and v0.5 are implemented in the working tree. The project is research-first: no score is treated as a validated trading edge until it passes out-of-sample and robustness tests.

### v0.4 — data quality + historical acquisition
- Binance Futures historical 1m canonical data.
- Historical taker-flow, OI and funding inputs.
- Point-in-time dependency lookup.
- Coverage, gap and provenance audits.

### v0.5 — point-in-time replay + event engine
- Historical feature materialization.
- Signal episode de-duplication and configurable cooldown.
- Fee/slippage-aware backtesting.
- Funding-aware net-R accounting.
- Multi-horizon / multi-target research matrix.
- Conservative same-bar TP/SL rule.

### v0.6 — regime + robustness research
- BTC/risk-on/risk-off regime features.
- Walk-forward train/test windows.
- Bootstrap confidence intervals and stability analysis.
- Feature ablation and cross-symbol/cross-regime robustness.

## Completed in v0.2

- Binance live event ingestion: aggTrade, depth@100ms, markPrice@1s, forceOrder.
- Durable raw event store and 1-minute OHLCV/flow bars.
- OI time-series persistence and 5-minute ΔOI feature.
- Rolling volume median, RVOL, volume Z-score, taker imbalance, order-book imbalance and short/long liquidation flow.
- Live Pump/Dump snapshot endpoint and Top scanner endpoint.

## Next: v0.3

- 5m/15m/1h feature materialization.
- Historical replay runner.
- Event-level backtest labels (MFE/MAE, TP-before-SL).
- Data quality monitor: gaps, stale feeds, clock drift, duplicate events.
- Redis stream/cache adapter and PostgreSQL/Timescale production adapter.

## v0.4 — data quality + historical acquisition (in progress)
- Historical Binance Futures 1m/5m/15m/1h import.
- Historical taker-flow reconstruction from Kline taker-buy quote volume.
- Historical OI and funding-rate ingestion.
- Point-in-time feature dependencies for replay.
- Coverage/gap/duplicate audits.
- Next: historical feature materialization + signal de-duplication/cooldown + realistic costs/slippage.

## v0.5 — point-in-time replay + event engine (completed/in progress)
- Historical feature materialization from 1m canonical bars.
- Point-in-time OI/funding dependencies.
- Signal episode de-duplication and cooldown.
- Fees/slippage-aware backtest.
- Multi-horizon / multi-target research matrix.
- Conservative same-bar TP/SL policy.

## v0.6 — regime + robustness research
- BTC market-regime features.
- Walk-forward train/test windows.
- Bootstrap confidence intervals and stability analysis.
- Feature ablation: volume vs taker vs OI vs funding vs liquidation vs depth.
- Cross-symbol and cross-regime robustness.


## v1.7 — Advanced Risk Gate + Alert Ranking
- risk-adjusted reward/risk after fees, slippage and funding;
- expected-value gate;
- liquidity and data-quality hard gates;
- anomaly-risk gate;
- signal-age confidence decay;
- correlated exposure and cluster-aware ranking;
- deterministic ALERT/WATCH/BLOCK + A/B/C quality tiers.

## v1.8 — Live Paper Trading Engine (completed)
- stateful 1m paper execution;
- latency-aware entry;
- entry/exit slippage, fees and funding impact;
- TP/SL/horizon exits with conservative ambiguous-bar handling;
- MFE/MAE, Net-R and equity/drawdown accounting;
- durable `paper_trades` audit ledger;
- no real order execution.

## v1.9 — Live Monitoring + Drift Detection (completed)
- market-data freshness guard;
- feature drift with PSI and two-sample KS;
- prediction-distribution drift;
- calibration drift with Brier, log loss and ECE;
- live/paper performance monitoring with expectancy and drawdown;
- persistent monitoring audit trail;
- automatic quarantine on BLOCK-level monitoring findings;
- manual quarantine override endpoint;
- Paper Submit safety gate while quarantined.

## v2.0 — Production Infrastructure
- replace SQLite with PostgreSQL/TimescaleDB and Redis where justified;
- full signal-to-alert integration with Fusion + Risk Gate;
- Telegram alert delivery with idempotent alert IDs;
- production dashboard and alert history;
- Prometheus/Grafana observability;
- model registry, dataset/config/schema versioning;
- feature/prediction drift dashboards;
- controlled retraining pipeline with out-of-sample promotion gates;
- deployment hardening, secrets, health checks and kill switch.


## v2.1 — Live Validation & Promotion Gate (completed)
- resolved paper-trade cohort validation;
- bootstrap confidence intervals for net expectancy;
- drawdown/data-quality/calibration gates;
- symbol/tier-scoped validation;
- candidate/promotion/rejection ledger;
- manual authenticated model promotion;
- no automatic promotion and no synthetic performance claims.

## v2.2 — Real-Market Observation & Immutable Research Ledger (completed)
- point-in-time live observation snapshots;
- append-only alert/paper outcome events;
- payload hash + per-symbol hash chain;
- session/config/model context capture;
- chain verification endpoint;
- no future labels inside the original market snapshot.

## v2.3 — Multi-Source Live Enrichment (in progress)
- scheduled LiveCoinWatch BTC-volume-band enrichment;
- scheduled CryptoMeter gainers/losers, volume-flow, liquidity-lens and merged-orderbook snapshots;
- source health/latency/error ledger;
- normalized source rows with raw payload provenance;
- symbol-scoped enrichment cache;
- source context kept namespaced and **not injected into scoring until independently validated**;
- future: CoinGlass/Nansen/DEX/on-chain integrations and source-consensus research gates.

## v2.4 — External Intelligence + Cross-Source Consensus
- CoinGlass V4 derivatives / liquidation ingestion;
- GeckoTerminal on-chain DEX market ingestion;
- GoPlus token-security on-demand checks;
- source-family de-duplication;
- cross-source agreement/conflict detection;
- security blockers;
- consensus persistence and audit;
- consensus kept out of scoring until independent OOS validation.

## v3.1 — Production Core Storage Migration (started)
- PostgreSQL implementation of the canonical market-data `DurableStore` contract.
- Controlled SQLite → PostgreSQL migration utility with row-count + SHA-256 verification.
- Fail-closed backend factory: production selection of PostgreSQL never silently downgrades to SQLite.
- PostgreSQL schema version marker and deterministic migration audit inputs.
- Core cutover remains explicit; specialized ledgers (paper/monitoring/alert/observation/enrichment/intelligence/validation) retain their existing stores until their own migration is independently tested.
- Next: migrate specialized ledgers and implement dual-write/reconciliation before final production cutover.

## v3.2 — Full Ledger Dual-Write + Reconciliation (completed)
- transactional SQLite outbox triggers for specialized ledgers;
- at-least-once PostgreSQL ledger replication;
- append-only event ledger + current-state materialization;
- deterministic count/digest reconciliation;
- backlog/lag readiness gates;
- shadow/required/off replication modes.

## v3.3 — Production Integration + Live Paper Validation (completed as engineering layer)
- production component readiness matrix;
- immutable validation dataset manifests;
- deterministic dataset digests and run IDs;
- duplicate signal-id and temporal-integrity checks;
- cohort validation by symbol, direction and quality tier;
- reuse of v2.1 statistical validation without auto-promotion;
- `READY_FOR_MANUAL_REVIEW` as the highest validation decision;
- API + CLI for repeatable validation runs.
- Remaining evidence is intentionally operational: sustained real-market paper observations and resolved outcomes are required before any edge claim or model promotion.
