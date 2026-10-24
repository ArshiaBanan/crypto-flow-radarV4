from app.risk_v17 import RiskInput, evaluate_risk, rank_alerts
from app.signals.fusion_v17 import fuse_snapshot


def _inp(**kwargs):
    base = dict(
        symbol="TESTUSDT", direction="long", ts_ms=1, fusion_score=88, rule_score=90,
        ml_probability=0.72, expected_move_pct=4.0, stop_pct=1.5, target_pct=4.0,
        liquidity_regime="normal", slippage_bps=2, fee_bps_per_side=4,
        funding_cost_pct=0.02, anomaly_score=15, data_quality_score=95,
        regime_fit=80, organic_score=80,
    )
    base.update(kwargs)
    return RiskInput(**base)


def test_positive_ev_and_net_rr_can_alert():
    d = evaluate_risk(_inp())
    assert d.action == "ALERT"
    assert d.expected_value_r > 0
    assert d.net_rr > 1.0


def test_negative_ev_blocks_even_with_high_scores():
    d = evaluate_risk(_inp(ml_probability=0.20, fusion_score=96, rule_score=98, target_pct=0.5))
    assert d.action == "BLOCK"
    assert "negative_expected_value" in d.blockers


def test_very_thin_liquidity_blocks():
    d = evaluate_risk(_inp(liquidity_regime="very_thin"))
    assert d.action == "BLOCK"
    assert "very_thin_liquidity" in d.blockers


def test_correlation_cap_blocks():
    d = evaluate_risk(_inp(correlated_exposure_pct=3.0, max_correlated_exposure_pct=3.0))
    assert d.action == "BLOCK"
    assert "correlated_exposure_cap" in d.blockers


def test_cooldown_blocks():
    d = evaluate_risk(_inp(cooldown_remaining_sec=120))
    assert d.action == "BLOCK"
    assert "cooldown_active" in d.blockers


def test_confidence_decay_reduces_score():
    fresh = evaluate_risk(_inp(signal_age_sec=0))
    stale = evaluate_risk(_inp(signal_age_sec=3600))
    assert stale.confidence_decay < fresh.confidence_decay
    assert stale.risk_score <= fresh.risk_score


def test_fusion_produces_directional_explanations():
    f = {
        "taker_imbalance": 0.60, "return_5m_pct": 1.2, "oi_change_pct": 2.0,
        "organic_score": 78, "anomaly_score": 10, "regime_label": "risk_on_trend",
        "regime_confidence": 0.8, "liquidity_regime": "normal",
        "orderbook_imbalance": 0.5, "close": 100, "bar_volume_quote": 1000000,
        "funding_rate": 0.0001,
    }
    r = fuse_snapshot(f, pump_score=90, dump_score=35, ml_probability=0.74)
    assert r.direction == "long"
    assert r.fusion_score > 70
    assert "directional_taker_confirmation" in r.positive_evidence


def test_rank_limits_correlation_cluster():
    rows = [
        {"symbol": "AAA", "direction": "long", "risk_score": 95, "action": "ALERT", "quality_tier": "A", "correlation_group": "L1"},
        {"symbol": "BBB", "direction": "long", "risk_score": 94, "action": "ALERT", "quality_tier": "A", "correlation_group": "L1"},
        {"symbol": "CCC", "direction": "long", "risk_score": 93, "action": "ALERT", "quality_tier": "A", "correlation_group": "L1"},
        {"symbol": "DDD", "direction": "short", "risk_score": 92, "action": "ALERT", "quality_tier": "B", "correlation_group": "DEX"},
    ]
    out = rank_alerts(rows, limit=10, max_per_correlation_group=2)
    assert [x.symbol for x in out] == ["AAA", "BBB", "DDD"]
    assert [x.rank for x in out] == [1, 2, 3]
