from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app, settings, observation_ledger
from app.observation_v22 import ObservationLedgerV22, public_config_hash


def test_v22_snapshot_is_deterministic_and_event_linked(tmp_path):
    db = tmp_path / "obs.db"
    ledger = ObservationLedgerV22(str(db))
    sid = ledger.start_session(environment="test", code_version="2.2.0", config_hash="cfg", ts_ms=1000)
    oid1 = ledger.record_snapshot(
        session_id=sid, ts_ms=2000, symbol="AAAUSDT", source="test", price=100.0,
        features={"x": 1.25}, pump_score=88.0, dump_score=12.0,
        fusion_score=91.0, action="ALERT", quality_tier="A", risk_score=87.0,
        ml_probability=0.82, anomaly_score=4.0, data_quality_score=98.0, regime_fit=0.9,
        model_context={"models": [{"name": "xgb", "version": "1.1"}]}, config_hash="cfg",
    )
    oid2 = ledger.record_snapshot(
        session_id=sid, ts_ms=2000, symbol="AAAUSDT", source="test", price=100.0,
        features={"x": 1.25}, pump_score=88.0, dump_score=12.0,
        fusion_score=91.0, action="ALERT", quality_tier="A", risk_score=87.0,
        ml_probability=0.82, anomaly_score=4.0, data_quality_score=98.0, regime_fit=0.9,
        model_context={"models": [{"name": "xgb", "version": "1.1"}]}, config_hash="cfg",
    )
    assert oid1 == oid2
    eid = ledger.append_event(oid1, "ALERT_CREATED", reference_id="a1", payload={"action": "ALERT"}, ts_ms=2100)
    row = ledger.get(oid1)
    assert row and row["events"][0]["event_id"] == eid
    assert ledger.verify_chain(sid)["valid"] is True
    ledger.stop_session(sid, ts_ms=3000)
    assert ledger.latest_session()["status"] == "STOPPED"
    ledger.close()


def test_v22_chain_detects_tampering(tmp_path):
    db = tmp_path / "obs.db"
    ledger = ObservationLedgerV22(str(db))
    sid = ledger.start_session(ts_ms=1000)
    ledger.record_snapshot(session_id=sid, ts_ms=2000, symbol="a", source="test", price=10, features={"x": 1})
    ledger.conn.execute("UPDATE market_observations_v22 SET price=11")
    ledger.conn.commit()
    report = ledger.verify_chain(sid)
    assert report["valid"] is False
    assert report["broken"]
    ledger.close()


def test_v22_config_hash_ignores_secrets():
    h1 = public_config_hash({"binance_enabled": True, "admin_api_key": "secret-a", "lcw_api_key": "a"})
    h2 = public_config_hash({"binance_enabled": True, "admin_api_key": "secret-b", "lcw_api_key": "b"})
    assert h1 == h2


def test_v22_api_status_and_recent():
    settings.data_engine_enabled = False
    settings.binance_enabled = False
    with TestClient(app) as client:
        s = client.get("/v22/observation/status")
        assert s.status_code == 200
        assert s.json()["schema_version"] == "observation_v22"
        r = client.get("/v22/observations?limit=5")
        assert r.status_code == 200
        assert isinstance(r.json()["rows"], list)

import asyncio


def test_v22_pipeline_order_and_event_link(tmp_path):
    from app.production_v20 import AlertLedger, ProductionRuntimeV20
    from app.observation_v22 import ObservationLedgerV22

    db = tmp_path / "e2e.db"
    ledger = ObservationLedgerV22(str(db))
    sid = ledger.start_session(environment="test", code_version="2.2.0", config_hash="cfg", ts_ms=1_000)

    oid = ledger.record_snapshot(
        session_id=sid, ts_ms=2_000, symbol="AAAUSDT", source="binance_live", price=100,
        features={"taker_imbalance": 0.6, "volume_z_median": 3.5, "close": 100},
        pump_score=92, dump_score=10, config_hash="cfg",
    )
    assert ledger.get(oid)["events"] == []

    alert_db = tmp_path / "alerts.db"
    alerts = AlertLedger(str(alert_db))
    rt = ProductionRuntimeV20(alerts, auto_paper_submit=False)
    features = {
        "symbol": "AAAUSDT", "ts_ms": 2_000, "close": 100.0,
        "taker_imbalance": 0.6, "volume_z_median": 3.5, "return_5m_pct": 1.5,
        "return_15m_pct": 2.0, "oi_change_pct": 3.0, "funding_rate": 0.0,
        "orderbook_imbalance": 0.5, "anomaly_score": 5.0, "organic_score": 80.0,
        "liquidity_regime": "normal", "regime_label": "risk_on_trend", "regime_confidence": 0.8,
        "bar_volume_quote": 2_000_000, "depth_usd": 1_000_000, "spread_bps": 2.0,
    }
    result = asyncio.run(rt.consider(features, 92, 10))
    assert result is not None
    event, _ = result
    ledger.append_event(oid, "ALERT_CREATED", reference_id=event.alert_id, payload={"action": event.action}, ts_ms=2_000)
    ledger.append_event(oid, "PAPER_RESOLVED", reference_id="paper-1", payload={"status": "RESOLVED", "net_r": 0.75}, ts_ms=62_000)
    events = ledger.get(oid)["events"]
    assert [e["event_type"] for e in events] == ["ALERT_CREATED", "PAPER_RESOLVED"]
    ledger.close(); alerts.close()
