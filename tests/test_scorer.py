from app.signals.scorer import score_observation


def test_bullish_flow_score_is_above_neutral():
    result = score_observation(
        change_1h_pct=1.8,
        change_5m_pct=0.6,
        volume_rvol=4.0,
        taker_buy=130,
        taker_sell=70,
        oi_change_pct=8.0,
        funding_rate=-0.00008,
        short_liq_usd=1_200_000,
        long_liq_usd=300_000,
        bid_value=8_000_000,
        ask_value=5_000_000,
        smart_money_netflow_usd=350_000,
    )
    assert result.pump_score > 70
    assert result.dump_score < 50
    assert result.stage in {"EARLY", "IGNITION", "WATCH"}


def test_bearish_flow_score_is_above_neutral():
    result = score_observation(
        change_1h_pct=-2.0,
        change_5m_pct=-0.7,
        volume_rvol=4.0,
        taker_buy=60,
        taker_sell=140,
        oi_change_pct=7.0,
        funding_rate=0.00008,
        short_liq_usd=300_000,
        long_liq_usd=1_300_000,
        bid_value=4_000_000,
        ask_value=8_000_000,
        smart_money_netflow_usd=-350_000,
    )
    assert result.dump_score > 70
    assert result.pump_score < 50
