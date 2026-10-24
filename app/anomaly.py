from __future__ import annotations

from dataclasses import asdict, dataclass
from math import isfinite, log10
from typing import Any


def _f(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return v if isfinite(v) else default


def _clip(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _sigmoid_abs(x: float, scale: float = 1.0) -> float:
    """Map absolute anomaly magnitude to [0,1] without overflow."""
    import math
    z = _clip(abs(x) / max(scale, 1e-9), 0.0, 20.0)
    return 2.0 / (1.0 + math.exp(-z)) - 1.0


@dataclass(frozen=True, slots=True)
class AnomalyResult:
    score: float
    classification: str
    flags: tuple[str, ...]
    organic_score: float
    caution_score: float
    diagnostics: dict[str, float]


def detect_anomaly(f: dict[str, Any]) -> AnomalyResult:
    """
    Deterministic anomaly/manipulation *risk proxy*.

    This does not identify illegal manipulation or spoofing. It flags market states
    whose observable microstructure is unusually inconsistent, crowded, or fragile.
    All inputs are point-in-time features supplied by the caller.
    """
    taker = _f(f.get("taker_imbalance"))
    vol_z = _f(f.get("volume_z_median"))
    rvol = _f(f.get("rvol_1m_vs_history"), 1.0)
    ret1 = _f(f.get("return_1m_pct"))
    ret5 = _f(f.get("return_5m_pct"))
    oi = _f(f.get("oi_change_pct"))
    funding = _f(f.get("funding_rate"))
    ob = _f(f.get("orderbook_imbalance"))
    long_liq = max(0.0, _f(f.get("long_liquidation_usd")))
    short_liq = max(0.0, _f(f.get("short_liquidation_usd")))
    trades = max(0.0, _f(f.get("trades")))
    quote_vol = max(0.0, _f(f.get("bar_volume_quote")))
    spread_bps = max(0.0, _f(f.get("spread_bps")))
    depth_usd = max(0.0, _f(f.get("depth_usd")))
    price_accel = _f(f.get("price_acceleration_5m"))
    depth_change = _f(f.get("depth_change_pct"))
    spread_change = _f(f.get("spread_change_pct"))

    flags: list[str] = []
    anomaly = 0.0
    organic = 0.0
    caution = 0.0

    # 1) Vertical move / volume dislocation.
    if abs(ret1) >= 1.0 and abs(ret5) >= 1.5:
        flags.append("VERTICAL_PRICE_MOVE")
        anomaly += 12.0 * _clip(abs(ret1) / 2.0, 0.0, 1.5)
    if vol_z >= 3.0 or rvol >= 4.0:
        flags.append("EXTREME_VOLUME")
        anomaly += 12.0 * _clip(max(vol_z / 6.0, rvol / 8.0), 0.0, 1.5)
    if abs(ret5) >= 1.5 and vol_z < 1.0 and rvol < 1.5:
        flags.append("PRICE_VOLUME_DISLOCATION")
        anomaly += 12.0

    # 2) Positioning vs price divergence.
    if abs(ret5) >= 0.8 and abs(oi) >= 3.0 and (ret5 * oi) < 0:
        flags.append("PRICE_OI_DIVERGENCE")
        anomaly += 10.0
    if abs(ret5) >= 1.0 and oi * ret5 > 0:
        organic += 9.0  # price and OI moving together is evidence of participation
    elif abs(ret5) >= 1.0 and oi * ret5 <= 0:
        caution += 7.0

    # 3) Taker flow confirmation / disagreement.
    directional = 1.0 if ret5 > 0 else -1.0 if ret5 < 0 else 0.0
    taker_confirm = taker * directional
    if directional and abs(taker) >= 0.35 and taker_confirm > 0:
        organic += 9.0
    if directional and abs(taker) >= 0.35 and taker_confirm < 0:
        flags.append("TAKER_PRICE_DIVERGENCE")
        anomaly += 10.0

    # 4) Funding crowding. Extreme funding is a fragility warning, not a direction signal.
    funding_abs_bps = abs(funding) * 10000.0
    if funding_abs_bps >= 3.0:
        flags.append("EXTREME_FUNDING")
        anomaly += 8.0 * _clip(funding_abs_bps / 10.0, 0.0, 1.5)
        caution += 6.0

    # 5) Liquidation cascade proxy.
    liq_total = long_liq + short_liq
    if liq_total > 0:
        liq_ratio = liq_total / max(quote_vol, 1.0)
        if liq_ratio >= 0.05:
            flags.append("LIQUIDATION_CASCADE")
            anomaly += 14.0 * _clip(liq_ratio / 0.20, 0.0, 1.0)
            caution += 8.0
        liq_bias = (short_liq - long_liq) / liq_total
        if directional and directional * liq_bias > 0:
            organic += 4.0

    # 6) Order-book fragility.
    if abs(ob) >= 0.65 and abs(ret5) >= 0.8:
        # Strong book imbalance that accompanies a move can be confirming, but is fragile
        # if depth is simultaneously disappearing.
        organic += 4.0
    if depth_change <= -35.0:
        flags.append("DEPTH_COLLAPSE")
        anomaly += 12.0 * _clip(abs(depth_change) / 70.0, 0.0, 1.0)
        caution += 8.0
    if spread_change >= 50.0:
        flags.append("SPREAD_WIDENING")
        anomaly += 10.0 * _clip(spread_change / 150.0, 0.0, 1.0)
        caution += 7.0
    if spread_bps >= 15.0:
        flags.append("WIDE_SPREAD")
        anomaly += 8.0
        caution += 6.0

    # 7) Participation / wash-like proxy. This is deliberately framed as a proxy only.
    if trades > 0 and quote_vol > 0:
        avg_trade_usd = quote_vol / trades
        if avg_trade_usd < 20.0 and quote_vol > 2_000_000:
            flags.append("LOW_AVG_TRADE_SIZE")
            anomaly += 6.0
            caution += 5.0

    # 8) Momentum acceleration.
    if abs(price_accel) >= 1.0:
        flags.append("MOMENTUM_ACCELERATION")
        anomaly += 6.0 * _clip(abs(price_accel) / 3.0, 0.0, 1.0)

    # Score is a risk-of-anomalous-behaviour proxy, not probability.
    anomaly = _clip(anomaly - 0.45 * organic, 0.0, 100.0)
    organic_score = _clip(50.0 + organic - 0.25 * caution, 0.0, 100.0)
    caution_score = _clip(caution + 0.35 * anomaly, 0.0, 100.0)

    if anomaly >= 70:
        classification = "HIGH_ANOMALY"
    elif anomaly >= 45:
        classification = "CAUTION"
    elif organic_score >= 65 and anomaly < 30:
        classification = "RELATIVELY_ORGANIC"
    else:
        classification = "NORMAL"

    diagnostics = {
        "funding_abs_bps": funding_abs_bps,
        "liquidation_to_volume": liq_total / max(quote_vol, 1.0),
        "avg_trade_usd": quote_vol / trades if trades else 0.0,
        "taker_price_alignment": taker_confirm,
        "price_oi_alignment": ret5 * oi,
        "depth_change_pct": depth_change,
        "spread_change_pct": spread_change,
        "spread_bps": spread_bps,
        "depth_usd": depth_usd,
        "log_volume": log10(max(1.0, quote_vol)),
    }
    return AnomalyResult(round(anomaly, 2), classification, tuple(dict.fromkeys(flags)),
                         round(organic_score, 2), round(caution_score, 2), diagnostics)


def anomaly_features(f: dict[str, Any]) -> dict[str, Any]:
    r = detect_anomaly(f)
    return {
        "anomaly_score": r.score,
        "anomaly_classification": r.classification,
        "anomaly_flags": list(r.flags),
        "organic_score": r.organic_score,
        "caution_score": r.caution_score,
        "anomaly_diagnostics": r.diagnostics,
    }
