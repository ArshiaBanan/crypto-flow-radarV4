import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _client(tmp_path: Path):
    os.environ["SQLITE_PATH"] = str(tmp_path / "cfr_v35.db")
    os.environ["APP_ENV"] = "dev"
    os.environ["BINANCE_ENABLED"] = "false"
    os.environ["DATA_ENGINE_ENABLED"] = "false"
    os.environ["V32_ENABLED"] = "false"
    os.environ["V30_REQUIRE_POSTGRES"] = "false"
    os.environ["V30_REQUIRE_REDIS"] = "false"
    from app.main import app, saas_v35
    saas_v35.sqlite_path = str(tmp_path / "cfr_v35.db")
    saas_v35._init()
    return TestClient(app)


def test_v35_register_login_watchlist_preferences(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/v35/auth/register", json={"email": "trader@example.com", "password": "correct-horse-battery"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"]["plan_code"] == "RESEARCH"
        assert body["csrf_token"]

        me = c.get("/v35/auth/me")
        assert me.status_code == 200
        csrf = me.json()["csrf_token"]

        created = c.post("/v35/watchlists", json={"name": "Momentum"}, headers={"X-CSRF-Token": csrf})
        assert created.status_code == 200, created.text
        wid = created.json()["watchlist"]["watchlist_id"]

        updated = c.patch(f"/v35/watchlists/{wid}", json={"symbols": ["BTCUSDT", "ETHUSDT", "BTCUSDT"]}, headers={"X-CSRF-Token": csrf})
        assert updated.status_code == 200
        assert updated.json()["watchlist"]["symbols"] == ["BTCUSDT", "ETHUSDT"]

        prefs = c.put("/v35/preferences", json={"alert_min_score": 82, "preferred_side": "LONG", "notify_browser": True, "notify_telegram": True}, headers={"X-CSRF-Token": csrf})
        assert prefs.status_code == 200
        assert prefs.json()["preferences"]["alert_min_score"] == 82.0
        assert prefs.json()["preferences"]["notify_telegram"] is False

        wl = c.get("/v35/watchlists")
        assert wl.status_code == 200
        assert wl.json()["usage"]["watchlists"] == 1

        out = c.post("/v35/auth/logout", headers={"X-CSRF-Token": csrf})
        assert out.status_code == 200
        assert c.get("/v35/auth/me").status_code == 401


def test_v35_csrf_and_entitlement_limit(tmp_path):
    with _client(tmp_path) as c:
        c.post("/v35/auth/register", json={"email": "guest@example.com", "password": "correct-horse-battery"})
        assert c.post("/v35/watchlists", json={"name": "NoCsrf"}).status_code == 403
        csrf = c.get("/v35/auth/me").json()["csrf_token"]
        # Research entitlement allows exactly one watchlist.
        assert c.post("/v35/watchlists", json={"name": "One"}, headers={"X-CSRF-Token": csrf}).status_code == 200
        assert c.post("/v35/watchlists", json={"name": "Two"}, headers={"X-CSRF-Token": csrf}).status_code == 403
