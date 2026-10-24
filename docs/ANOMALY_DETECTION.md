# v1.4 — Anomaly & Manipulation-Risk Detection

This layer is a deterministic, point-in-time **market anomaly risk proxy**. It does not identify illegal market manipulation and must not be described as proof of spoofing, wash trading, or coordinated manipulation.

## Signals
- price/volume dislocation
- price/OI divergence
- taker-flow/price divergence
- extreme funding / crowding
- liquidation cascade proxy
- order-book/depth collapse
- spread widening
- low average trade size at high notional volume (wash-like proxy only)
- momentum acceleration

## Outputs
- `anomaly_score` (0–100 risk proxy; not probability)
- `anomaly_classification`: NORMAL, RELATIVELY_ORGANIC, CAUTION, HIGH_ANOMALY
- `anomaly_flags`
- `organic_score`
- `caution_score`
- diagnostic ratios

The alert engine should use these values as a **risk gate** and explanatory layer. They are not a standalone direction predictor.

## Important data rule
Any advanced inputs that are unavailable at the signal timestamp remain missing/neutral. The system must not fabricate historical order-book or liquidation data.
