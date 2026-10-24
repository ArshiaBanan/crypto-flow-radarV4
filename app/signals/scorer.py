from __future__ import annotations

from app.features.math import clamp


def _sigmoid(x: float) -> float:
    # Stable enough for the bounded feature ranges used here.
    import math
    return 1.0 / (1.0 + math.exp(-max(-12.0, min(12.0, x))))


def score_features(f: dict) -> dict:
    """Transparent v0.2 score. Every contribution is inspectable and bounded."""
    taker = float(f.get("taker_imbalance") or 0.0)
    oz = float(f.get("volume_z_median") or 0.0)
    oi = float(f.get("oi_change_pct") or 0.0)
    ob = float(f.get("orderbook_imbalance") or 0.0)
    ret = float(f.get("return_5m_pct") or 0.0)
    short_liq = float(f.get("short_liquidation_usd") or 0.0)
    long_liq = float(f.get("long_liquidation_usd") or 0.0)
    funding = float(f.get("funding_rate") or 0.0)

    # Positive evidence: abnormal volume + aggressive buys + OI confirmation + bid pressure.
    pump_raw = (
        1.35 * taker
        + 0.55 * clamp(oz / 4.0, -2, 2)
        + 0.025 * clamp(oi, -40, 40)
        + 0.70 * ob
        + 0.025 * clamp(ret, -20, 20)
        + 0.20 * _sigmoid(short_liq / 1_000_000.0) - 0.15 * _sigmoid(long_liq / 1_000_000.0)
        - 0.20 * clamp(funding * 100, -1, 1)
    )
    dump_raw = (
        -1.35 * taker
        - 0.55 * clamp(oz / 4.0, -2, 2)
        - 0.025 * clamp(oi, -40, 40)
        - 0.70 * ob
        - 0.025 * clamp(ret, -20, 20)
        + 0.20 * _sigmoid(long_liq / 1_000_000.0) - 0.15 * _sigmoid(short_liq / 1_000_000.0)
        + 0.20 * clamp(funding * 100, -1, 1)
    )
    pump = clamp(50 + 35 * pump_raw)
    dump = clamp(50 + 35 * dump_raw)
    if pump >= 80 and ret < 3:
        stage = "EARLY"
    elif pump >= 80:
        stage = "IGNITION"
    elif dump >= 80:
        stage = "DISTRIBUTION"
    else:
        stage = "WATCH"
    return {"pump_score": round(pump, 2), "dump_score": round(dump, 2), "stage": stage}

from dataclasses import dataclass


@dataclass(slots=True)
class ScoreResult:
    pump_score: float
    dump_score: float
    stage: str


def score_observation(*, change_1h_pct: float = 0.0, change_5m_pct: float = 0.0,
                      volume_rvol: float | None = None, taker_buy: float | None = None,
                      taker_sell: float | None = None, oi_change_pct: float | None = None,
                      funding_rate: float | None = None, short_liq_usd: float | None = None,
                      long_liq_usd: float | None = None, bid_value: float | None = None,
                      ask_value: float | None = None, smart_money_netflow_usd: float | None = None) -> ScoreResult:
    buy, sell = taker_buy or 0.0, taker_sell or 0.0
    taker = (buy - sell) / (buy + sell) if buy + sell else 0.0
    ob = ((bid_value or 0.0) - (ask_value or 0.0)) / ((bid_value or 0.0) + (ask_value or 0.0)) if (bid_value or 0.0) + (ask_value or 0.0) else 0.0
    rvol_bonus = max(0.0, min(12.0, ((volume_rvol or 1.0) - 1.0) * 3.0))
    oi_bonus = max(0.0, min(10.0, abs(oi_change_pct or 0.0) * 0.7))
    liq_total = (short_liq_usd or 0.0) + (long_liq_usd or 0.0)
    liq_bias = ((short_liq_usd or 0.0) - (long_liq_usd or 0.0)) / liq_total if liq_total else 0.0
    smart = max(-8.0, min(8.0, (smart_money_netflow_usd or 0.0) / 100_000.0 * 2.0))
    momentum = max(-8.0, min(8.0, change_5m_pct * 2.0))
    funding_bull = max(-4.0, min(4.0, -(funding_rate or 0.0) * 100_000.0 * 0.5))
    directional = taker * 35.0 + ob * 18.0 + momentum + funding_bull
    pump = clamp(50.0 + directional + rvol_bonus + (oi_bonus if oi_change_pct and oi_change_pct > 0 else 0) + liq_bias * 10.0 + smart)
    dump = clamp(50.0 - directional + rvol_bonus + (oi_bonus if oi_change_pct and oi_change_pct > 0 else 0) - liq_bias * 10.0 - smart)
    stage = "WATCH"
    if pump >= 80 and change_1h_pct < 3:
        stage = "EARLY"
    elif pump >= 80:
        stage = "IGNITION"
    elif dump >= 80:
        stage = "DISTRIBUTION"
    return ScoreResult(round(pump, 2), round(dump, 2), stage)
