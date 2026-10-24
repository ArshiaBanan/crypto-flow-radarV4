from app.monitoring_v19 import (
    DriftPolicy,
    MonitoringEngine,
    MonitoringStore,
    LiveMonitoringRuntime,
    calibration_metrics,
    ks_statistic,
    performance_metrics,
    psi,
)


def test_psi_low_for_same_distribution():
    a = list(range(1, 101))
    b = list(range(1, 101))
    assert psi(a, b) < 1e-9


def test_psi_detects_distribution_shift():
    base = [i / 100 for i in range(100)]
    current = [0.8 + i / 500 for i in range(100)]
    assert psi(base, current) > 0.25


def test_ks_detects_shift():
    base = [i / 100 for i in range(100)]
    current = [0.8 + i / 500 for i in range(100)]
    assert ks_statistic(base, current) > 0.3


def test_calibration_metrics():
    p = [0.9] * 10 + [0.1] * 10
    y = [1] * 10 + [0] * 10
    m = calibration_metrics(p, y)
    assert m["brier"] < 0.02
    assert m["ece"] < 0.06


def test_performance_metrics_dd():
    m = performance_metrics([1, -0.5, -1.0, 0.5])
    assert m["count"] == 4
    assert m["max_drawdown_r"] >= 1.0


def test_monitoring_healthy():
    eng = MonitoringEngine(DriftPolicy(min_samples=5))
    seq = [i / 100 for i in range(20)]
    rep = eng.evaluate(
        baseline_features={"rvol": seq},
        current_features={"rvol": seq},
        baseline_predictions=seq,
        current_predictions=seq,
        current_probabilities=[0.9, 0.1] * 10,
        current_outcomes=[1, 0] * 10,
        current_returns_r=[1.0, -0.5] * 10,
        latest_data_ts_ms=9_950,
        now_ms=10_000,
    )
    assert rep.status == "HEALTHY"
    assert rep.quarantine is False


def test_monitoring_quarantines_on_drift_and_stale_data():
    eng = MonitoringEngine(DriftPolicy(min_samples=5))
    base = [i / 100 for i in range(20)]
    current = [0.8 + i / 500 for i in range(20)]
    rep = eng.evaluate(
        baseline_features={"rvol": base},
        current_features={"rvol": current},
        baseline_predictions=base,
        current_predictions=current,
        current_probabilities=[0.99] * 20,
        current_outcomes=[0] * 20,
        current_returns_r=[-1.0] * 20,
        latest_data_ts_ms=0,
        now_ms=1_000_000,
    )
    assert rep.status == "QUARANTINED"
    assert rep.quarantine is True
    assert any(f.domain == "freshness" and f.severity == "BLOCK" for f in rep.findings)


def test_insufficient_data_does_not_quarantine():
    eng = MonitoringEngine(DriftPolicy(min_samples=30))
    rep = eng.evaluate(baseline_features={"x": [1, 2]}, current_features={"x": [1, 2]})
    assert rep.status == "INSUFFICIENT_DATA"
    assert rep.quarantine is False


def test_monitoring_store_audit_and_state(tmp_path):
    eng = MonitoringEngine(DriftPolicy(min_samples=3))
    rep = eng.evaluate(
        baseline_features={"x": [1, 2, 3]},
        current_features={"x": [1, 2, 3]},
        latest_data_ts_ms=1_000,
        now_ms=1_010,
    )
    store = MonitoringStore(str(tmp_path / "m.db"))
    store.record(rep)
    rows = store.recent()
    state = store.state()
    store.set_quarantine(True, reason="test", ts_ms=2_000)
    state2 = store.state()
    store.close()
    assert len(rows) == 1
    assert state["quarantine"]["value"] is False
    assert state2["quarantine"]["value"]["quarantine"] is True


def test_live_monitoring_runtime_auto_evaluates_after_window(tmp_path):
    policy = DriftPolicy(min_samples=5)
    engine = MonitoringEngine(policy)
    store = MonitoringStore(str(tmp_path / "rt.db"))
    runtime = LiveMonitoringRuntime(engine, store, baseline_size=10, current_size=5, eval_interval_ms=1)
    for i in range(15):
        runtime.observe_features("BTCUSDT", 1_000 + i, {"rvol": float(i)})
    report = runtime.last_report
    store.close()
    assert report is not None
    assert report.status in {"HEALTHY", "WARN", "QUARANTINED"}


def test_live_monitoring_runtime_skips_non_numeric_features(tmp_path):
    store = MonitoringStore(str(tmp_path / "rt2.db"))
    runtime = LiveMonitoringRuntime(MonitoringEngine(DriftPolicy(min_samples=2)), store, baseline_size=4, current_size=2, eval_interval_ms=1)
    for i in range(6):
        runtime.observe_features("BTCUSDT", 1_000 + i, {"x": float(i), "stage": "BUY", "flag": True})
    store.close()
    assert "x" in runtime.feature_buffers["btcusdt"]
    assert "stage" not in runtime.feature_buffers["btcusdt"]


def test_live_monitoring_runtime_isolates_symbols(tmp_path):
    store = MonitoringStore(str(tmp_path / "rt3.db"))
    runtime = LiveMonitoringRuntime(MonitoringEngine(DriftPolicy(min_samples=2)), store, baseline_size=4, current_size=2, eval_interval_ms=1)
    for i in range(6):
        runtime.observe_features("BTCUSDT", 1_000 + i, {"x": float(i)})
        runtime.observe_features("SOLUSDT", 1_000 + i, {"x": float(100 + i)})
    store.close()
    assert set(runtime.feature_buffers) == {"btcusdt", "solusdt"}
    assert set(runtime.last_report_by_symbol) == {"btcusdt", "solusdt"}
