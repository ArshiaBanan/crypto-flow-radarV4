from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from app.validation_v21 import ValidationPolicy, ValidationStore, validate_paper_rows
from app.main import app, settings, validation_store


def _rows(n=120, r=0.25):
    return [
        {
            "status": "RESOLVED",
            "net_r": r,
            "ambiguous_bar": 0,
            "ml_probability": 0.70 if r > 0 else 0.30,
            "symbol": "aaa",
            "signal_ts_ms": i * 60_000,
            "metadata": {"quality_tier": "A"},
        }
        for i in range(n)
    ]


def test_v21_insufficient_data():
    report = validate_paper_rows(_rows(20), policy=ValidationPolicy(min_resolved=50))
    assert report.status == "INSUFFICIENT_DATA"
    assert report.resolved == 20


def test_v21_pass_gate():
    rows = _rows(120, 0.30)
    report = validate_paper_rows(
        rows,
        cohort="all",
        policy=ValidationPolicy(min_resolved=100, min_expectancy_r=0.10, max_calibration_brier=0.25, bootstrap_iters=400),
        generated_at_ms=123456,
    )
    assert report.status == "PASS"
    assert report.expectancy_r and report.expectancy_r > 0.1
    assert report.expectancy_ci_low and report.expectancy_ci_low >= 0.1


def test_v21_fail_drawdown_and_expectancy():
    rows = _rows(110, 0.05)
    rows[-5:] = [{**x, "net_r": -2.0, "ml_probability": 0.7} for x in rows[-5:]]
    report = validate_paper_rows(
        rows,
        policy=ValidationPolicy(min_resolved=100, min_expectancy_r=0.30, max_drawdown_pct=5.0, bootstrap_iters=400),
    )
    assert report.status == "FAIL"
    assert any(not c["pass"] for c in report.checks)


def test_v21_store_and_promotions(tmp_path):
    store = ValidationStore(str(tmp_path / "v21.db"))
    report = validate_paper_rows(_rows(120), policy=ValidationPolicy(min_resolved=100, bootstrap_iters=300), generated_at_ms=777)
    rid = store.record(report)
    assert store.recent(1)[0]["report_id"] == rid
    store.request_promotion("m1", "radar", "7", rid)
    assert store.promotions(1)[0]["status"] == "CANDIDATE"
    store.set_promotion("m1", "PROMOTED", "manual")
    assert store.promotions(1)[0]["status"] == "PROMOTED"
    store.close()


def test_v21_api_validation_insufficient():
    settings.data_engine_enabled = False
    settings.binance_enabled = False
    with TestClient(app) as client:
        resp = client.post('/v21/validation/paper', json={"cohort": "all", "min_resolved": 1000, "bootstrap_iters": 200})
        assert resp.status_code == 200
        assert resp.json()["report"]["status"] == "INSUFFICIENT_DATA"


def test_v21_quality_tier_can_be_filtered_from_paper_metadata(tmp_path):
    from app.paper_v18 import PaperTradeStore, PaperSignal, simulate_signal
    db = tmp_path / "paper.db"
    store = PaperTradeStore(str(db))
    s = PaperSignal("s1", 1000, "aaa", "long", 100.0, 1.0, 2.0, 5, quality_tier="A", fusion_score=90.0, ml_probability=0.8, expected_value_r=0.4)
    result = simulate_signal(s, [(1000, 100, 103, 99, 102)])
    store.write(result)
    store.close()
    from app.validation_v21 import load_paper_rows
    rows = load_paper_rows(str(db), quality_tier="A")
    assert rows and rows[0]["metadata"]["quality_tier"] == "A"
