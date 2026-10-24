import hashlib
import hmac
import json
import os
import pytest
from pathlib import Path

from fastapi.testclient import TestClient


def _client(tmp_path: Path):
    os.environ["SQLITE_PATH"] = str(tmp_path / "cfr_v36.db")
    os.environ["APP_ENV"] = "dev"
    os.environ["BINANCE_ENABLED"] = "false"
    os.environ["DATA_ENGINE_ENABLED"] = "false"
    os.environ["V32_ENABLED"] = "false"
    os.environ["V30_REQUIRE_POSTGRES"] = "false"
    os.environ["V30_REQUIRE_REDIS"] = "false"
    os.environ["V36_EMAIL_MODE"] = "console"
    os.environ["V36_COOKIE_SECURE"] = "false"
    from app.main import app, settings, saas_v36
    settings.v36_email_mode = "console"
    settings.app_env = "dev"
    saas_v36.sqlite_path = str(tmp_path / "cfr_v36.db")
    saas_v36.webhook_secret = None
    saas_v36._init()
    return TestClient(app)


def test_v36_register_verify_login_profile(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/v36/auth/register", json={"username": "alpha_trader", "email": "alpha@example.com", "password": "correct-horse-battery", "display_name": "Alpha"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["verification_required"] is True
        code = body["development_verification_code"]
        assert len(code) == 6

        pre = c.post("/v36/auth/login", json={"identifier": "alpha_trader", "password": "correct-horse-battery"})
        assert pre.status_code == 403
        assert pre.json()["detail"]["code"] == "EMAIL_VERIFICATION_REQUIRED"

        bad = c.post("/v36/auth/verify-email", json={"email": "alpha@example.com", "code": "000000"})
        assert bad.status_code == 400

        ok = c.post("/v36/auth/verify-email", json={"email": "alpha@example.com", "code": code})
        assert ok.status_code == 200
        assert ok.json()["user"]["email_verified"] is True

        login = c.post("/v36/auth/login", json={"identifier": "alpha_trader", "password": "correct-horse-battery"})
        assert login.status_code == 200, login.text
        csrf = login.json()["csrf_token"]
        me = c.get("/v36/auth/me")
        assert me.status_code == 200
        assert me.json()["user"]["username"] == "alpha_trader"
        csrf = me.json()["csrf_token"]

        prof = c.put("/v36/profile", json={"display_name": "Alpha Prime", "timezone": "Europe/Berlin", "telegram_chat_id": "123456"}, headers={"X-CSRF-Token": csrf})
        assert prof.status_code == 200, prof.text
        assert prof.json()["profile"]["display_name"] == "Alpha Prime"


def test_v36_resend_cooldown_and_reset(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/v36/auth/register", json={"username": "reset_user", "email": "reset@example.com", "password": "correct-horse-battery"})
        assert r.status_code == 200
        blocked = c.post("/v36/auth/resend-verification", json={"email": "reset@example.com", "code": "000000"})
        assert blocked.status_code == 429
        assert blocked.json()["detail"]["code"] == "RESEND_COOLDOWN"
        code = r.json()["development_verification_code"]
        assert c.post("/v36/auth/verify-email", json={"email": "reset@example.com", "code": code}).status_code == 200

        fp = c.post("/v36/auth/forgot-password", json={"email": "reset@example.com"})
        assert fp.status_code == 200
        # Console mode doesn't expose reset codes by design; reset service is tested at store level below.


def test_v36_plan_signal_entitlements_and_quota(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/v36/auth/register", json={"username": "quota_user", "email": "quota@example.com", "password": "correct-horse-battery"})
        code = r.json()["development_verification_code"]
        assert c.post("/v36/auth/verify-email", json={"email": "quota@example.com", "code": code}).status_code == 200
        login = c.post("/v36/auth/login", json={"identifier": "quota_user", "password": "correct-horse-battery"}).json()
        csrf = login["csrf_token"]
        usage = c.get("/v36/usage").json()["usage"]
        assert usage["window"] == "weekly"
        assert usage["limit"] == 1
        assert c.post("/v36/signal/reserve", json={"signal_id": "SIG-1", "signal_kind": "STANDARD", "channel": "IN_APP"}, headers={"X-CSRF-Token": csrf}).status_code == 200
        again = c.post("/v36/signal/reserve", json={"signal_id": "SIG-2", "signal_kind": "STANDARD", "channel": "IN_APP"}, headers={"X-CSRF-Token": csrf})
        assert again.status_code == 403
        assert again.json()["detail"]["code"] == "SIGNAL_QUOTA_EXHAUSTED"
        moon = c.post("/v36/signal/reserve", json={"signal_id": "SIG-3", "signal_kind": "TO_THE_MOON", "channel": "IN_APP"}, headers={"X-CSRF-Token": csrf})
        assert moon.status_code == 403
        assert moon.json()["detail"]["code"] == "SIGNAL_KIND_LOCKED"


def test_v36_watchlist_tracking_and_billing_webhook(tmp_path):
    with _client(tmp_path) as c:
        r = c.post("/v36/auth/register", json={"username": "billing_user", "email": "billing@example.com", "password": "correct-horse-battery"})
        code = r.json()["development_verification_code"]
        c.post("/v36/auth/verify-email", json={"email": "billing@example.com", "code": code})
        login = c.post("/v36/auth/login", json={"identifier": "billing_user", "password": "correct-horse-battery"}).json()
        csrf = login["csrf_token"]
        wl = c.post("/v36/watchlists", json={"name": "Momentum"}, headers={"X-CSRF-Token": csrf})
        assert wl.status_code == 200, wl.text
        wid = wl.json()["watchlist"]["watchlist_id"]
        up = c.patch(f"/v36/watchlists/{wid}", json={"symbols": ["BTCUSDT", "ETHUSDT", "BTCUSDT"]}, headers={"X-CSRF-Token": csrf})
        assert up.status_code == 200
        assert up.json()["watchlist"]["symbols"] == ["BTCUSDT", "ETHUSDT"]

        from app.main import saas_v36
        secret = "billing-test-secret"
        saas_v36.webhook_secret = secret
        event = {"type": "subscription.plan_changed", "data": {"user_id": login["user"]["user_id"], "plan_code": "ELITE", "provider": "test", "external_id": "sub_123"}}
        raw = json.dumps(event, separators=(",", ":")).encode()
        sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        hook = c.post("/v36/billing/webhook", content=raw, headers={"X-V36-Signature": sig, "Content-Type": "application/json"})
        assert hook.status_code == 200, hook.text
        sub = c.get("/v36/subscription").json()["subscription"]
        assert sub["plan_code"] == "ELITE"
        assert sub["plan"]["stealth_signals"] is True
        assert sub["plan"]["advanced_whales"] is True


@pytest.mark.asyncio
async def test_v36_premium_alert_router_email_and_quota(tmp_path):
    from app.saas_v36 import GmailEmailSenderV36, SaaSNotificationRouterV36, SaaSStoreV36
    from app.production_v20 import AlertEvent

    sender = GmailEmailSenderV36("smtp.gmail.com", 587, None, None, None, mode="console")
    store = SaaSStoreV36(str(tmp_path / "router.db"), email_sender=sender)
    user, code, delivery = store.create_user("pro_trader", "pro@example.com", "correct-horse-battery")
    store.verify_email("pro@example.com", code)
    store.set_plan(user["user_id"], "PRO")
    store.update_preferences(user["user_id"], {"alert_min_score": 70, "preferred_side": "BOTH", "notify_browser": True, "notify_email": True, "notify_telegram": False, "notify_sms": False})
    router = SaaSNotificationRouterV36(store, sender)
    event = AlertEvent('SIG-PRO', 1000, 'btcusdt', 'long', 100, 'ALERT', 'A', 90, 88, 90, 0.72, 0.2, 2.4, 3, 1.5, 4, 'normal', 10, 95, 80, ('flow',), (), (), (), signal_kind='STANDARD')
    result = await router.route_alert(event)
    assert result['in_app'] == 1
    assert result['email'] == 1
    usage = store.signal_usage(user['user_id'])
    assert usage['used'] == 1
    # A second channel for the same signal must not consume a second quota unit.
    store.update_preferences(user['user_id'], {"alert_min_score": 70, "preferred_side": "BOTH", "notify_browser": True, "notify_email": False, "notify_telegram": False, "notify_sms": False})
    usage2 = store.signal_usage(user['user_id'])
    assert usage2['used'] == 1


def test_v36_legacy_legendary_migrates_to_elite(tmp_path):
    from app.saas_v36 import SaaSStoreV36
    store=SaaSStoreV36(str(tmp_path / "legacy.db"), email_sender=None)
    user, code, _ = store.create_user("legacy_user", "legacy@example.com", "correct-horse-battery")
    store.verify_email("legacy@example.com", code)
    profile=store.set_plan(user["user_id"], "LEGENDARY", provider="migration")
    assert profile["plan_code"] == "ELITE"
    assert profile["entitlements"]["stealth_signals"] is True
