from app.backtest import SignalEvent, evaluate_signal


def test_long_tp_before_sl():
    s = SignalEvent(1000, "BTCUSDT", 90, 20, "EARLY", 100.0)
    bars = [(1060,100,101,99,100),(1120,100,106,99,105)]
    o = evaluate_signal(s, bars, "long", 5, 2, 60)
    assert o.hit_tp is True
    assert o.hit_sl is False
    assert o.time_to_tp_min == 2


def test_short_sl_before_tp():
    s = SignalEvent(1000, "ETHUSDT", 20, 90, "DISTRIBUTION", 100.0)
    bars = [(1060,100,103,99,102),(1120,102,106,101,105)]
    o = evaluate_signal(s, bars, "short", 5, 2, 60)
    assert o.hit_sl is True
    assert o.hit_tp is False
    assert o.time_to_sl_min == 1
