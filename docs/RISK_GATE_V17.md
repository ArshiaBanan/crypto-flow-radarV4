# Risk Gate & Alert Ranking — v1.7

v1.7 is the execution-quality and portfolio-context layer for Crypto Flow Radar.

## Design principles

1. A high signal score is not enough.
2. Costs are applied before evaluating reward/risk.
3. ML probability is used only when a validated model is available; otherwise the system explicitly falls back to a rule-score proxy.
4. Thin liquidity, poor data quality, severe anomaly risk, active cooldown, or excessive correlated exposure can block an alert.
5. Alert ranking is a filtering/ranking mechanism, not a profitability guarantee.

## Core calculations

### Net reward/risk

`round_trip_cost = slippage + 2*fee + funding_cost`

`effective_reward = target_pct - round_trip_cost`

`effective_risk = stop_pct + round_trip_cost`

`net_rr = effective_reward / effective_risk`

### Expected value

`EV_pct = p * effective_reward - (1-p) * effective_risk`

`EV_R = EV_pct / effective_risk`

For a validated ML model, `p` is the model probability for the exact label definition used by the research dataset. Without a model, a clearly labelled rule-score proxy is used only to keep the engine operational; it should not be interpreted as a calibrated win probability.

### Signal confidence decay

The risk score is multiplied by a time-decay factor:

`decay = exp(-ln(2) * age / half_life)`

This means the signal's contribution halves every configured half-life unless a fresh snapshot replaces it.

### Correlated exposure

`correlation_penalty = correlated_exposure / max_correlated_exposure`, clipped to `[0,1]`.

At or above the exposure cap the new alert is blocked. Below the cap it reduces ranking quality.

## Decision policy

- `ALERT / A`: risk score >= 78, EV_R >= 0.10, net R:R >= 1.8, data quality >= 80, and no hard blockers.
- `ALERT / B`: risk score >= 65, EV_R >= 0, net R:R >= 1.25, data quality >= 70, and no hard blockers.
- `WATCH / C`: intermediate quality without a hard blocker.
- `BLOCK`: any hard blocker or insufficient risk-adjusted quality.

These thresholds are engineering defaults. They must be stress-tested with out-of-sample data before being used as a trading rule.
