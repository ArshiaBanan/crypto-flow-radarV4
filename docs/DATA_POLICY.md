# Data quality policy

1. Never treat a single provider's signal as ground truth.
2. Store source + observed timestamp + fetch timestamp for every normalized observation.
3. Keep raw payloads or a content-addressed immutable archive for reproducibility.
4. Normalize symbol/pair identifiers before joining data across vendors.
5. Do not mix spot and perpetual metrics without explicit labels.
6. Do not call a volume spike "capital inflow" unless an independent flow/flow-proxy signal supports it.
7. Every signal must be backtestable using only data available at the signal timestamp.
8. Record missingness; never silently replace a missing feature with a bullish or bearish value.
9. Include liquidity and slippage risk in the signal record.
10. Version the scoring formula. Historical scores must always be reproducible under the formula version that produced them.


## v0.2 provenance rules

- Raw Binance events are stored before feature computation.
- 1-minute bars are derived only from trades whose event timestamp is at or before the bar close.
- OI snapshots are timestamped and ΔOI uses only samples already observed by the engine.
- Order-book imbalance is explicitly treated as a transient feature, not proof of executed flow.
- Liquidation flow is reset after each scored 1-minute snapshot so the same event is not counted repeatedly.
- Any future ML label must be generated strictly after the feature timestamp; no future price information may enter the feature row.
