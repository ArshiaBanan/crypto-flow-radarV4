from __future__ import annotations

from dataclasses import asdict, dataclass
from math import exp, isfinite
from typing import Any, Iterable


LIQUIDITY_MULTIPLIER = {
    "deep": 1.00,
    "normal": 0.90,
    "thin": 0.65,
    "very_thin": 0.35,
    "unknown": 0.75,
}


@dataclass(frozen=True, slots=True)
class RiskInput:
    symbol: str
    direction: str
    ts_ms: int
    fusion_score: float
    rule_score: float
    ml_probability: float | None
    expected_move_pct: float
    stop_pct: float
    target_pct: float
    liquidity_regime: str = "unknown"
    slippage_bps: float = 0.0
    fee_bps_per_side: float = 4.0
    funding_cost_pct: float = 0.0
    anomaly_score: float = 0.0
    data_quality_score: float = 100.0
    regime_fit: float = 50.0
    organic_score: float = 50.0
    signal_age_sec: float = 0.0
    confidence_half_life_sec: float = 900.0
    cooldown_remaining_sec: float = 0.0
    correlated_exposure_pct: float = 0.0
    max_correlated_exposure_pct: float = 3.0
    beta_btc: float = 0.0
    beta_eth: float = 0.0


@dataclass(frozen=True, slots=True)
class RiskDecision:
    action: str  # ALERT / WATCH / BLOCK
    quality_tier: str  # A / B / C / BLOCKED
    risk_score: float
    effective_reward_pct: float
    effective_risk_pct: float
    net_rr: float
    expected_value_pct: float
    expected_value_r: float
    liquidity_multiplier: float
    confidence_decay: float
    correlation_penalty: float
    warnings: tuple[str, ...]
    blockers: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite(v: float, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _confidence_decay(age_sec: float, half_life_sec: float) -> float:
    if age_sec <= 0:
        return 1.0
    if half_life_sec <= 0:
        return 0.0
    return _clamp(exp(-0.69314718056 * age_sec / half_life_sec), 0.0, 1.0)


def evaluate_risk(inp: RiskInput) -> RiskDecision:
    warnings: list[str] = []
    blockers: list[str] = []
    reasons: list[str] = []

    stop = max(0.0, _finite(inp.stop_pct))
    target = max(0.0, _finite(inp.target_pct))
    slip = max(0.0, _finite(inp.slippage_bps)) / 100.0
    fees = max(0.0, _finite(inp.fee_bps_per_side)) * 2.0 / 100.0
    funding = abs(_finite(inp.funding_cost_pct))
    round_trip_cost = slip + fees + funding

    effective_reward = target - round_trip_cost
    effective_risk = stop + round_trip_cost
    net_rr = effective_reward / effective_risk if effective_risk > 0 else 0.0

    p = inp.ml_probability
    if p is None or not (0.0 <= float(p) <= 1.0):
        p = _clamp(_finite(inp.rule_score, 50.0) / 100.0, 0.0, 1.0)
        warnings.append("ml_probability_unavailable_rule_proxy_used")
    else:
        p = _clamp(float(p), 0.0, 1.0)

    expected_value_pct = p * effective_reward - (1.0 - p) * effective_risk
    expected_value_r = expected_value_pct / effective_risk if effective_risk > 0 else 0.0

    liq_regime = inp.liquidity_regime if inp.liquidity_regime in LIQUIDITY_MULTIPLIER else "unknown"
    liq_mult = LIQUIDITY_MULTIPLIER[liq_regime]
    if liq_regime in {"thin", "very_thin"}:
        warnings.append(f"liquidity_{liq_regime}")
    if liq_regime == "very_thin":
        blockers.append("very_thin_liquidity")

    decay = _confidence_decay(_finite(inp.signal_age_sec), _finite(inp.confidence_half_life_sec, 900.0))
    if decay < 0.50:
        warnings.append("signal_confidence_decay")

    exposure = max(0.0, _finite(inp.correlated_exposure_pct))
    exposure_cap = max(0.01, _finite(inp.max_correlated_exposure_pct, 3.0))
    correlation_penalty = _clamp(exposure / exposure_cap, 0.0, 1.0)
    if correlation_penalty >= 1.0:
        blockers.append("correlated_exposure_cap")
    elif correlation_penalty >= 0.67:
        warnings.append("high_correlated_exposure")

    dq = _clamp(_finite(inp.data_quality_score, 100.0), 0.0, 100.0)
    anomaly = _clamp(_finite(inp.anomaly_score), 0.0, 100.0)
    regime_fit = _clamp(_finite(inp.regime_fit, 50.0), 0.0, 100.0)
    organic = _clamp(_finite(inp.organic_score, 50.0), 0.0, 100.0)
    fusion = _clamp(_finite(inp.fusion_score, 0.0), 0.0, 100.0)

    if dq < 60:
        blockers.append("data_quality_too_low")
    elif dq < 80:
        warnings.append("data_quality_warning")
    if anomaly >= 85:
        blockers.append("extreme_anomaly_risk")
    elif anomaly >= 60:
        warnings.append("high_anomaly_risk")
    if effective_risk <= 0 or effective_reward <= 0:
        blockers.append("non_positive_net_reward_or_risk")
    if expected_value_r <= -0.05:
        blockers.append("negative_expected_value")
    elif expected_value_r < 0:
        warnings.append("weak_expected_value")
    if inp.cooldown_remaining_sec > 0:
        blockers.append("cooldown_active")

    expected_move = abs(_finite(inp.expected_move_pct))
    if expected_move and stop > expected_move * 1.75:
        warnings.append("stop_much_wider_than_expected_move")

    execution_quality = _clamp(100.0 * liq_mult - 0.5 * max(0.0, slip - 2.0), 0.0, 100.0)
    rr_quality = _clamp(net_rr / 4.0 * 100.0, 0.0, 100.0)
    ev_quality = _clamp(50.0 + expected_value_r * 35.0, 0.0, 100.0)
    correlation_quality = 100.0 * (1.0 - correlation_penalty)

    raw_score = (
        0.23 * fusion
        + 0.15 * rr_quality
        + 0.18 * ev_quality
        + 0.12 * dq
        + 0.10 * execution_quality
        + 0.08 * regime_fit
        + 0.06 * organic
        + 0.04 * (100.0 - anomaly)
        + 0.04 * correlation_quality
    )
    risk_score = _clamp(raw_score * (0.65 + 0.35 * decay), 0.0, 100.0)

    if blockers:
        action = "BLOCK"
        tier = "BLOCKED"
    elif risk_score >= 78 and expected_value_r >= 0.10 and net_rr >= 1.8 and dq >= 80:
        action, tier = "ALERT", "A"
    elif risk_score >= 65 and expected_value_r >= 0.0 and net_rr >= 1.25 and dq >= 70:
        action, tier = "ALERT", "B"
    elif risk_score >= 50:
        action, tier = "WATCH", "C"
    else:
        action, tier = "BLOCK", "BLOCKED"

    if action == "ALERT":
        reasons.append("risk_adjusted_quality_threshold_passed")
    if expected_value_r >= 0.10:
        reasons.append("positive_expected_value_margin")
    if net_rr >= 2.0:
        reasons.append("net_rr_above_2")
    if liq_mult >= 0.9:
        reasons.append("execution_liquidity_supportive")
    if decay >= 0.75:
        reasons.append("signal_is_fresh")

    return RiskDecision(
        action=action,
        quality_tier=tier,
        risk_score=round(risk_score, 2),
        effective_reward_pct=round(effective_reward, 5),
        effective_risk_pct=round(effective_risk, 5),
        net_rr=round(net_rr, 4),
        expected_value_pct=round(expected_value_pct, 5),
        expected_value_r=round(expected_value_r, 4),
        liquidity_multiplier=round(liq_mult, 3),
        confidence_decay=round(decay, 5),
        correlation_penalty=round(correlation_penalty, 4),
        warnings=tuple(dict.fromkeys(warnings)),
        blockers=tuple(dict.fromkeys(blockers)),
        reasons=tuple(dict.fromkeys(reasons)),
    )


@dataclass(frozen=True, slots=True)
class RankedAlert:
    rank: int
    symbol: str
    direction: str
    score: float
    action: str
    quality_tier: str
    correlation_group: str | None


def rank_alerts(
    rows: Iterable[dict[str, Any]],
    *,
    limit: int = 20,
    max_per_correlation_group: int = 2,
) -> list[RankedAlert]:
    candidates = [r for r in rows if r.get("action") == "ALERT"]
    candidates.sort(key=lambda r: (-_finite(r.get("risk_score")), str(r.get("symbol", "")), str(r.get("direction", ""))))
    counts: dict[str, int] = {}
    out: list[RankedAlert] = []
    for row in candidates:
        group = row.get("correlation_group")
        if group and counts.get(str(group), 0) >= max_per_correlation_group:
            continue
        if group:
            counts[str(group)] = counts.get(str(group), 0) + 1
        out.append(
            RankedAlert(
                rank=len(out) + 1,
                symbol=str(row.get("symbol", "")),
                direction=str(row.get("direction", "")),
                score=round(_finite(row.get("risk_score")), 2),
                action=str(row.get("action")),
                quality_tier=str(row.get("quality_tier", "")),
                correlation_group=str(group) if group else None,
            )
        )
        if len(out) >= max(1, int(limit)):
            break
    return out
