from __future__ import annotations

import sqlite3

from app.live_validation_v33 import CohortSpecV33, LivePaperValidationHarnessV33, LivePaperValidationStoreV33
from app.production_integration_v33 import build_component_matrix, config_fingerprint, integration_readiness
from app.validation_v21 import ValidationPolicy


def _make_db(path, rows):
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE paper_trades (
            signal_id TEXT PRIMARY KEY, symbol TEXT, direction TEXT, signal_ts_ms INTEGER,
            execution_ts_ms INTEGER, exit_ts_ms INTEGER, signal_price REAL, execution_price REAL,
            exit_price REAL, status TEXT, exit_reason TEXT, gross_return_pct REAL, net_return_pct REAL,
            gross_r REAL, net_r REAL, mfe_pct REAL, mae_pct REAL, slippage_pct REAL, fee_pct REAL,
            funding_pct REAL, latency_ms INTEGER, ambiguous_bar INTEGER, bars_observed INTEGER, metadata TEXT
        )"""
    )
    conn.executemany(
        "INSERT INTO paper_trades VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit(); conn.close()


def _row(sid, symbol, direction, ts, net_r, tier="A", status="RESOLVED", exit_ts=None):
    return (sid, symbol, direction, ts, ts + 1000, exit_ts if exit_ts is not None else ts + 60_000,
            100.0, 100.0, 101.0, status, "TP", net_r * 1.0, net_r * 1.0, net_r, net_r,
            2.0, -1.0, 0.01, 0.02, 0.0, 10, 0, 10, '{"quality_tier":"%s","ml_probability":1.0}' % tier)


def test_v33_empty_db_is_insufficient(tmp_path):
    db = tmp_path / "paper.db"
    _make_db(db, [])
    store = LivePaperValidationStoreV33(str(db))
    run = LivePaperValidationHarnessV33(str(db), store).run(cohort=CohortSpecV33(), policy=ValidationPolicy(min_resolved=2))
    assert run.decision == "INSUFFICIENT_DATA"
    assert run.integrity.valid is True
    assert run.integrity.rows_seen == 0


def test_v33_ready_for_manual_review_on_good_cohort(tmp_path):
    db = tmp_path / "paper.db"
    rows = [_row(f"s{i}", "btcusdt", "long", i * 100_000, 0.5, "A") for i in range(6)]
    _make_db(db, rows)
    store = LivePaperValidationStoreV33(str(db))
    run = LivePaperValidationHarnessV33(str(db), store).run(
        cohort=CohortSpecV33("btc-a", symbol="BTCUSDT", quality_tier="A"),
        policy=ValidationPolicy(min_resolved=5, min_expectancy_r=0.1, bootstrap_iters=200),
    )
    assert run.decision == "READY_FOR_MANUAL_REVIEW"
    assert "MANUAL_PROMOTION_REQUIRED" in run.reasons
    assert run.integrity.duplicate_signal_ids == 0


def test_v33_invalid_temporal_dataset_is_blocked(tmp_path):
    db = tmp_path / "paper.db"
    rows = [_row("bad", "btcusdt", "long", 1000, 0.5, exit_ts=500)]
    _make_db(db, rows)
    store = LivePaperValidationStoreV33(str(db))
    run = LivePaperValidationHarnessV33(str(db), store).run(policy=ValidationPolicy(min_resolved=1))
    assert run.decision == "INVALID_DATASET"
    assert run.integrity.invalid_temporal_rows == 1


def test_v33_quality_tier_filters_metadata(tmp_path):
    db = tmp_path / "paper.db"
    rows = [_row("a1", "solusdt", "long", 1000, 0.6, "A"), _row("b1", "solusdt", "long", 2000, -0.2, "B")]
    _make_db(db, rows)
    store = LivePaperValidationStoreV33(str(db))
    run = LivePaperValidationHarnessV33(str(db), store).run(cohort=CohortSpecV33("a", symbol="SOLUSDT", quality_tier="A"), policy=ValidationPolicy(min_resolved=1, bootstrap_iters=200))
    assert run.integrity.rows_seen == 1
    assert run.report["sample_size"] == 1


def test_v33_integration_matrix_and_config_fingerprint_are_stable():
    class S:
        binance_enabled=True; data_engine_enabled=True; v23_enrichment_enabled=True
        v24_intelligence_enabled=True; v20_enabled=True; postgres_url="x"; redis_url="y"; v32_enabled=True
    comps = build_component_matrix(settings=S(), storage={"postgres_reachable": True}, redis={"reachable": True}, dualwrite={"ready": True}, monitoring_quarantine=False)
    r = integration_readiness(comps, production=True)
    assert r["ready"] is True
    assert config_fingerprint({"b":2,"a":1}) == config_fingerprint({"a":1,"b":2})


def test_v33_production_readiness_fails_on_quarantine():
    class S:
        binance_enabled=True; data_engine_enabled=True; v23_enrichment_enabled=False
        v24_intelligence_enabled=False; v20_enabled=True; postgres_url="x"; redis_url="y"; v32_enabled=True
    comps = build_component_matrix(settings=S(), storage={"postgres_reachable": True}, redis={"reachable": True}, dualwrite={"ready": True}, monitoring_quarantine=True)
    r = integration_readiness(comps, production=True)
    assert r["ready"] is False
    assert "fusion_risk" in r["failures"]


def test_v33_api_status_and_validation_endpoint():
    from fastapi.testclient import TestClient
    from app.main import app, settings
    settings.data_engine_enabled = False
    settings.binance_enabled = False
    settings.v32_enabled = False
    with TestClient(app) as client:
        status = client.get('/v33/status')
        assert status.status_code == 200
        assert status.json()['version'] == '3.3.0'
        validation = client.post('/v33/validation/run', json={"cohort": "all", "min_resolved": 2, "bootstrap_iters": 200})
        assert validation.status_code == 200
        assert validation.json()['decision'] in {'INSUFFICIENT_DATA', 'VALIDATION_FAILED', 'INVALID_DATASET', 'READY_FOR_MANUAL_REVIEW'}
