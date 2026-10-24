# Crypto Flow Radar v1.9 — Live Monitoring & Drift Detection

v1.9 adds a model/runtime safety layer above v1.8 paper trading. It does not place real orders and does not auto-retrain a model.

## Monitored domains

- **Market-data freshness:** age of the latest observed market-data timestamp.
- **Feature drift:** PSI and two-sample KS statistic per feature.
- **Prediction drift:** PSI on model-output probabilities.
- **Calibration drift:** Brier score, log loss and ECE on labeled outcomes.
- **Live paper performance:** win rate, mean/expected R and cumulative max drawdown in R.
- **Quarantine:** a BLOCK finding switches the monitoring report to `QUARANTINED`.

## Severity policy

`OK` means the metric is inside the normal operating band.
`WARN` means the system should be watched and can be downgraded by the production policy layer.
`BLOCK` means new alerts should be quarantined until the condition is cleared or explicitly overridden.
`INSUFFICIENT_DATA` is not treated as a failure: it means the monitor does not have enough evidence to make a drift decision.

Thresholds live in `DriftPolicy` and are intentionally configurable. They are guardrails, not claims of predictive accuracy.

## Safety boundary

v1.9 only observes and gates the radar. Retraining remains a separate, audited process. A quarantine is an operational state, not proof that the underlying strategy has stopped working permanently.

## Live runtime

`LiveMonitoringRuntime` can be attached to the 1-minute Binance data engine. It keeps separate rolling windows **per symbol**, avoiding cross-asset contamination. After the configured window is full and the evaluation interval elapses, it automatically evaluates feature drift and freshness and persists the result. The runtime exposes its latest status through `/monitoring/status`.

The current runtime intentionally does not synthesize labeled outcomes or model predictions when those data are unavailable. Those domains remain `INSUFFICIENT_DATA` until real labeled/prediction streams are connected.
