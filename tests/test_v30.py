from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app
from app.observability_v30 import MetricsRegistryV30
from app.ops_v30 import RuntimeControllerV30
from app.runtime_guard_v30 import production_gate_v30
from app.storage_v30 import RedisHealthV30, StorageHealthV30


def test_v30_metrics_counter_gauge_and_prometheus():
    m = MetricsRegistryV30()
    m.inc("demo_requests_total", labels={"path": "/x"})
    m.inc("demo_requests_total", 2, labels={"path": "/x"})
    m.set_gauge("demo_ready", 1)
    m.observe("demo_latency", 7.5, labels={"route": "x"})
    snap = m.snapshot()
    assert any(x["value"] == 3 for x in snap["counters"])
    assert any(x["name"] == "demo_ready" and x["value"] == 1 for x in snap["gauges"])
    assert any(x["name"] == "demo_latency" and x["count"] == 1 for x in snap["histograms"])
    text = m.prometheus()
    assert "demo_requests_total" in text
    assert 'path="/x"' in text


def test_v30_sqlite_health(tmp_path):
    db = tmp_path / "radar.db"
    conn = sqlite3.connect(db)
    conn.execute("create table t(x integer)")
    conn.commit()
    conn.close()
    h = StorageHealthV30(str(db), "sqlite")
    report = h.run()
    assert report["ready"] is True
    assert report["active"]["name"] == "sqlite"


def test_v30_sqlite_missing_is_not_ready(tmp_path):
    db = tmp_path / "missing" / "radar.db"
    h = StorageHealthV30(str(db), "sqlite")
    report = h.run()
    assert report["ready"] is False
    assert "SQLITE_UNREACHABLE" in report["reasons"]


def test_v30_require_postgres_blocks_sqlite(tmp_path):
    db = tmp_path / "radar.db"
    sqlite3.connect(db).close()
    h = StorageHealthV30(str(db), "sqlite", None, True)
    report = h.run()
    assert report["ready"] is False
    assert "CORE_STORAGE_BACKEND_NOT_POSTGRES" in report["reasons"]


def test_v30_production_gate_requires_admin_key():
    a = production_gate_v30(app_env="production", admin_api_key=None, require_admin_key=True)
    assert a["ready"] is False
    assert "ADMIN_API_KEY_REQUIRED_IN_PRODUCTION" in a["reasons"]
    b = production_gate_v30(app_env="dev", admin_api_key=None, require_admin_key=True)
    assert b["ready"] is True


def test_v30_runtime_kill_switch_and_readiness(tmp_path):
    db = tmp_path / "radar.db"
    sqlite3.connect(db).close()
    r = RuntimeControllerV30()
    r.start()
    ok = r.readiness(storage=StorageHealthV30(str(db), "sqlite").run(), redis={"reachable": False}, require_redis=False, production=False)
    assert ok["ready"] is True
    assert ok["checks"]["heartbeat_fresh"] is True
    r.set_kill(True, "incident")
    bad = r.readiness(storage=StorageHealthV30(str(db), "sqlite").run(), redis={"reachable": False}, require_redis=False, production=False)
    assert bad["ready"] is False
    assert "KILL_SWITCH_ACTIVE" in bad["reasons"]


def test_v30_redis_unconfigured():
    r = asyncio.run(RedisHealthV30(None).check())
    assert r.configured is False
    assert r.reachable is False


def test_v30_health_contains_runtime_storage():
    from app import main as m
    m.settings.data_engine_enabled = False
    m.settings.binance_enabled = False
    m.settings.v23_enrichment_enabled = False
    m.settings.v24_intelligence_enabled = False
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "3.7.0"
        assert "runtime" in data and "storage" in data and "production_gate" in data


def test_v30_metrics_endpoint_and_request_id():
    from app import main as m
    m.settings.data_engine_enabled = False
    m.settings.binance_enabled = False
    with TestClient(app) as client:
        response = client.get("/v30/metrics")
        assert response.status_code == 200
        assert "cfr_http_requests_total" in response.text
        r2 = client.get("/v30/runtime")
        assert r2.status_code == 200
        assert r2.headers.get("X-Request-ID")


def test_v30_readiness_dev_with_sqlite(tmp_path):
    from app import main as m
    old_path = m.settings.sqlite_path
    old_backend = m.settings.storage_backend
    old_env = m.settings.app_env
    old_req = m.settings.v30_require_postgres
    try:
        db = tmp_path / "radar.db"
        sqlite3.connect(db).close()
        m.settings.sqlite_path = str(db)
        m.settings.storage_backend = "sqlite"
        m.settings.app_env = "dev"
        m.settings.v30_require_postgres = False
        m.storage_health_v30 = StorageHealthV30(str(db), "sqlite", None, False)
        with TestClient(app) as client:
            r = client.get("/v30/readiness")
            assert r.status_code == 200
            assert r.json()["ready"] is True
    finally:
        m.settings.sqlite_path = old_path
        m.settings.storage_backend = old_backend
        m.settings.app_env = old_env
        m.settings.v30_require_postgres = old_req
        m.storage_health_v30 = StorageHealthV30(old_path, old_backend, m.settings.postgres_url, old_req)


def test_v30_production_readiness_blocks_sqlite():
    from app import main as m
    old_env = m.settings.app_env
    old_key = m.settings.admin_api_key
    old_backend = m.settings.storage_backend
    old_req = m.settings.v30_require_postgres
    try:
        m.settings.app_env = "production"
        m.settings.admin_api_key = "secret"
        m.settings.storage_backend = "sqlite"
        m.settings.v30_require_postgres = True
        with TestClient(app) as client:
            r = client.get("/v30/readiness")
            assert r.status_code == 200
            body = r.json()
            assert body["ready"] is False
            assert "PRODUCTION_REQUIRES_POSTGRES_CORE_STORAGE" in body["runtime"]["reasons"]
    finally:
        m.settings.app_env = old_env
        m.settings.admin_api_key = old_key
        m.settings.storage_backend = old_backend
        m.settings.v30_require_postgres = old_req


def test_v30_kill_switch_requires_admin_key():
    from app import main as m
    old = m.settings.admin_api_key
    try:
        m.settings.admin_api_key = None
        with TestClient(app) as client:
            r = client.post("/v30/kill-switch?enabled=true")
            assert r.status_code == 503
    finally:
        m.settings.admin_api_key = old
