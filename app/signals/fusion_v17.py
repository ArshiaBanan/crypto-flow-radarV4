from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any
import math

from app.features.math import clamp


@dataclass(frozen=True, slots=True)
class FusionResult:
    direction: str
    fusion_score: float
    rule_score: float
    ml_probability: float | None
    regime_fit: float
    organic_score: float
    liquidity_score: float
    anomaly_score: float
    data_quality_score: float
    execution_score: float
    positive_evidence: tuple[str, ...]
    warnings: tuple[str, ...]


def _num(v: Any, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _direction(features: dict[str, Any], pump_score: float, dump_score: float) -> str:
    if pump_score >= dump_score:
        return "long"
    return "short"


def _regime_fit(features: dict[str, Any], direction: str) -> float:
    label = str(features.get("regime_label", "unknown"))
    conf = clamp(_num(features.get("regime_confidence")) * 100.0, 0.0, 100.0) if features.get("regime_confidence") is not None else 50.0
    if label == "unknown":
        return 50.0
    aligned = {
        "long": {"risk_on_trend", "mild_risk_on"},
        "short": {"risk_off_trend", "mild_risk_off"},
    }[direction]
    if label in aligned:
        return 55.0 + 0.45 * conf
    if label == "high_volatility":
        return 50.0
    return 35.0 + 0.15 * (100.0 - conf)


def _liquidity_score(features: dict[str, Any]) -> float:
    regime = str(features.get("liquidity_regime", "unknown"))
    if regime == "deep":
        base = 95.0
    elif regime == "normal":
        base = 80.0
    elif regime == "thin":
        base = 55.0
    elif regime == "very_thin":
        base = 20.0
    else:
        base = 60.0
    spread = _num(features.get("spread_bps"), 0.0)
    if spread > 20:
        base -= min(30.0, spread - 20.0)
    return clamp(base)


def _data_quality_score(features: dict[str, Any]) -> float:
    if features.get("data_quality_score") is not None:
        return clamp(_num(features.get("data_quality_score")), 0.0, 100.0)
    required = [
        "close", "bar_volume_quote", "taker_imbalance", "return_5m_pct",
        "oi_change_pct", "funding_rate", "orderbook_imbalance",
    ]
    present = sum(features.get(k) is not None for k in required)
    return present / len(required) * 100.0


def fuse_snapshot(
    features: dict[str, Any],
    *,
    pump_score: float,
    dump_score: float,
    ml_probability: float | None = None,
) -> FusionResult:
    direction = _direction(features, pump_score, dump_score)
    rule_score = max(float(pump_score), float(dump_score))
    organic = clamp(_num(features.get("organic_score"), 50.0))
    anomaly = clamp(_num(features.get("anomaly_score"), 0.0))
    regime_fit = _regime_fit(features, direction)
    liquidity = _liquidity_score(features)
    dq = _data_quality_score(features)

    taker = _num(features.get("taker_imbalance"))
    ret5 = _num(features.get("return_5m_pct"))
    oi = _num(features.get("oi_change_pct"))
    sign = 1.0 if direction == "long" else -1.0

    execution = 60.0
    positive: list[str] = []
    warnings: list[str] = []
    if sign * taker >= 0.35:
        positive.append("directional_taker_confirmation")
    if sign * ret5 > 0:
        positive.append("price_momentum_alignment")
    if sign * ret5 * oi > 0:
        positive.append("price_oi_confirmation")
    if organic >= 70:
        positive.append("organic_flow_confirmation")
    if regime_fit >= 70:
        positive.append("regime_alignment")
    if liquidity < 60:
        warnings.append("liquidity_penalty")
        execution -= 20
    if anomaly >= 45:
        warnings.append("anomaly_caution")
        execution -= 15
    if dq < 80:
        warnings.append("incomplete_data_context")
    if regime_fit < 45:
        warnings.append("regime_disagreement")

    components = [
        (rule_score, 0.30),
        ((ml_probability * 100.0) if ml_probability is not None else rule_score, 0.25),
        (organic, 0.12),
        (regime_fit, 0.10),
        (liquidity, 0.08),
        (100.0 - anomaly, 0.08),
        (dq, 0.05),
        (clamp(execution), 0.02),
    ]
    total_weight = sum(w for _, w in components)
    fusion = clamp(sum(v * w for v, w in components) / total_weight)

    return FusionResult(
        direction=direction,
        fusion_score=round(fusion, 2),
        rule_score=round(rule_score, 2),
        ml_probability=None if ml_probability is None else round(float(ml_probability), 6),
        regime_fit=round(regime_fit, 2),
        organic_score=round(organic, 2),
        liquidity_score=round(liquidity, 2),
        anomaly_score=round(anomaly, 2),
        data_quality_score=round(dq, 2),
        execution_score=round(clamp(execution), 2),
        positive_evidence=tuple(dict.fromkeys(positive)),
        warnings=tuple(dict.fromkeys(warnings)),
    )


@dataclass(frozen=True, slots=True)
class FusedSnapshot:
    symbol: str
    ts_ms: int
    result: FusionResult

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "ts_ms": self.ts_ms, **asdict(self.result)}
