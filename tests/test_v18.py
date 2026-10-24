from app.paper_v18 import PaperSignal, PaperTradeStore, simulate_signal, summarize_results


def _sig(**kw):
    base = dict(
        signal_id="s1", ts_ms=1_000, symbol="BTCUSDT", direction="long", entry_price=100.0,
        stop_pct=2.0, target_pct=4.0, horizon_min=3, slippage_bps=0.0, fee_bps_per_side=0.0,
    )
    base.update(kw)
    return PaperSignal(**base)


def test_tp_resolution_long():
    bars = [(60_001,100,101,99,100.5),(120_001,100.5,104.5,100,104),(180_001,104,105,103,104)]
    r = simulate_signal(_sig(), bars)
    assert r.status == "RESOLVED"
    assert r.exit_reason == "TP"
    assert r.net_return_pct > 3.0
    assert r.net_r > 1.0


def test_sl_resolution_is_conservative_when_bar_is_ambiguous():
    bars = [(60_001,100,105,95,100)]
    r = simulate_signal(_sig(target_pct=5, stop_pct=2), bars)
    assert r.exit_reason == "SL"
    assert r.ambiguous_bar is True
    assert r.net_r < 0


def test_short_trade_uses_directional_slippage_and_returns():
    bars = [(60_001,100,101,97,98),(120_001,98,97,94,95)]
    r = simulate_signal(_sig(direction="short", target_pct=5, stop_pct=2, slippage_bps=10), bars)
    assert r.exit_reason == "TP"
    assert r.net_return_pct > 0
    assert r.slippage_pct > 0


def test_latency_moves_execution_forward():
    bars = [(60_001,100,101,99,100),(120_001,103,104,102,103)]
    fresh = simulate_signal(_sig(signal_id="fresh", latency_ms=0), bars)
    late = simulate_signal(_sig(signal_id="late", latency_ms=90_000), bars)
    assert fresh.execution_price != late.execution_price
    assert late.execution_ts_ms == 120_001


def test_no_data_is_not_a_fake_loss():
    r = simulate_signal(_sig(), [])
    assert r.status == "NO_DATA"
    assert r.net_r is None


def test_costs_reduce_net_return():
    bars = [(60_001,100,105,99,104)]
    gross = simulate_signal(_sig(signal_id="g", fee_bps_per_side=0, slippage_bps=0), bars)
    costly = simulate_signal(_sig(signal_id="c", fee_bps_per_side=10, slippage_bps=10), bars)
    assert costly.net_return_pct < gross.net_return_pct
    assert costly.fee_pct > 0
    assert costly.slippage_pct > 0


def test_summary_reports_basic_metrics():
    bars1 = [(60_001,100,105,99,104)]
    bars2 = [(60_001,100,99,97,98)]
    a = simulate_signal(_sig(signal_id="a", target_pct=4), bars1)
    b = simulate_signal(_sig(signal_id="b", target_pct=4), bars2)
    s = summarize_results([a,b])
    assert s["resolved"] == 2
    assert s["wins"] == 1
    assert s["losses"] == 1


def test_paper_store_persists_result(tmp_path):
    store = PaperTradeStore(str(tmp_path / "paper.db"))
    r = simulate_signal(_sig(), [(60_001,100,105,99,104)])
    store.write(r)
    rows = store.recent()
    store.close()
    assert len(rows) == 1
    assert rows[0]["signal_id"] == "s1"
    assert rows[0]["status"] == "RESOLVED"


def test_live_engine_executes_and_resolves_on_bar():
    from app.paper_v18 import LivePaperEngine
    eng = LivePaperEngine(initial_equity=10_000, risk_pct=1.0, max_open_positions=2)
    s = _sig(signal_id="live", ts_ms=1_000, target_pct=4, latency_ms=0)
    assert eng.submit(s) == "QUEUED"
    out = eng.on_bar("BTCUSDT", (2_000, 100, 105, 99, 104))
    assert len(out) == 1
    assert out[0].exit_reason == "TP"
    assert eng.snapshot()["closed_trades"] == 1
    assert eng.snapshot()["equity"] > 10_000


def test_live_engine_blocks_duplicate_symbol_direction():
    from app.paper_v18 import LivePaperEngine
    eng = LivePaperEngine()
    assert eng.submit(_sig(signal_id="a")) == "QUEUED"
    assert eng.submit(_sig(signal_id="b")) == "DUPLICATE_SYMBOL_DIRECTION"


def test_live_engine_respects_latency():
    from app.paper_v18 import LivePaperEngine
    eng = LivePaperEngine()
    s = _sig(signal_id="lat", latency_ms=90_000)
    eng.submit(s)
    assert eng.on_bar("BTCUSDT", (60_001,100,101,99,100)) == []
    out = eng.on_bar("BTCUSDT", (120_001,100,105,99,104))
    assert len(out) == 1
    assert out[0].execution_ts_ms == 120_001


def test_summary_counts_all_results_including_no_data():
    from app.paper_v18 import PaperResult
    r = PaperResult("x", "BTCUSDT", "long", 1, None, None, 100, None, None, "NO_DATA", "NO_DATA", None, None, None, None, 0, 0, 0, 0, 0, 0, False, 0, {})
    s = summarize_results([r])
    assert s["signals"] == 1
    assert s["resolved"] == 0
