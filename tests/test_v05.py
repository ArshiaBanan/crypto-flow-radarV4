from __future__ import annotations

from app.features.replay import materialize_symbol
from app.signals.episodes import build_episodes
from app.storage.sqlite import SQLiteStore
from app.backtest import SignalEvent, evaluate_signal


def test_episode_cooldown_collapses_repeated_snapshots():
    rows = [
        {"ts_ms": 1_000, "symbol": "btcusdt", "pump_score": 85, "dump_score": 40, "stage": "EARLY", "close": 100},
        {"ts_ms": 5 * 60_000, "symbol": "btcusdt", "pump_score": 92, "dump_score": 35, "stage": "IGNITION", "close": 101},
        {"ts_ms": 10 * 60_000, "symbol": "btcusdt", "pump_score": 88, "dump_score": 30, "stage": "IGNITION", "close": 102},
        {"ts_ms": 30 * 60_000, "symbol": "btcusdt", "pump_score": 90, "dump_score": 20, "stage": "IGNITION", "close": 104},
    ]
    episodes = build_episodes(rows, min_score=80, cooldown_min=15)
    assert len(episodes) == 2
    assert episodes[0].peak_score == 92
    assert episodes[0].snapshots == 3
    assert episodes[1].entry_price == 104


def test_costs_reduce_net_r():
    s = SignalEvent(1_000, "BTCUSDT", 90, 10, "EARLY", 100.0)
    bars = [(60_000, 100, 105, 99, 104)]
    o = evaluate_signal(s, bars, "long", 5, 2, 60, fee_bps_per_side=4, slippage_bps_per_side=2)
    assert o.gross_r == 2.5
    assert round(o.cost_pct, 6) == 0.12
    assert o.net_r < o.gross_r


def test_same_bar_tp_sl_is_marked_ambiguous_and_conservative():
    s = SignalEvent(1_000, "BTCUSDT", 90, 10, "EARLY", 100.0)
    bars = [(60_000, 100, 106, 97, 100)]
    o = evaluate_signal(s, bars, "long", 5, 2, 60)
    assert o.ambiguous_bar is True
    assert o.hit_sl is True
    assert o.hit_tp is False
    assert o.gross_r == -1.0


def test_point_in_time_feature_materialization(tmp_path):
    db = SQLiteStore(str(tmp_path / "replay.db"))
    for i in range(120):
        ts = i * 60_000
        close = 100.0 + i * 0.01
        db.upsert_bar({
            "symbol": "btcusdt", "bucket_ms": ts,
            "open": close, "high": close + 0.05, "low": close - 0.05, "close": close,
            "volume_quote": 1000 + i, "buy_quote": 600 + i, "sell_quote": 400,
            "trades": 10, "source": "test",
        })
    db.write_oi("btcusdt", 114 * 60_000, 100.0)
    db.write_oi("btcusdt", 119 * 60_000, 110.0)
    db.write_funding("btcusdt", 119 * 60_000, 0.001)
    stats = materialize_symbol(db, "btcusdt", 119 * 60_000, 119 * 60_000, history_bars=120)
    assert stats.written == 1
    row = db.conn.execute("SELECT features FROM feature_snapshots WHERE symbol='btcusdt'").fetchone()
    assert row is not None
    import json
    features = json.loads(row[0])
    assert features["oi_change_pct"] is not None
    assert features["funding_rate"] == 0.001


def test_funding_is_directional_cost_or_credit():
    s_long = SignalEvent(1_000, "BTCUSDT", 90, 10, "EARLY", 100.0)
    bars = [(60_000, 100, 105, 99, 104)]
    long_o = evaluate_signal(s_long, bars, "long", 5, 2, 60, funding_events=[(500, 0.001)])
    short_o = evaluate_signal(s_long, bars, "short", 5, 2, 60, funding_events=[(500, 0.001)])
    assert long_o.funding_cost_pct == 0.0  # event is before entry and must not leak backward
    assert short_o.funding_cost_pct == 0.0
    long_o = evaluate_signal(s_long, bars, "long", 5, 2, 60, funding_events=[(30_000, 0.001)])
    short_o = evaluate_signal(s_long, bars, "short", 5, 2, 60, funding_events=[(30_000, 0.001)])
    assert long_o.funding_cost_pct == 0.1
    assert short_o.funding_cost_pct == -0.1
