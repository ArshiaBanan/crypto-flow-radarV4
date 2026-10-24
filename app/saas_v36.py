from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import smtplib
import sqlite3
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any

SCHEMA_VERSION = "saas_v36"
PBKDF2_ITERS = 260_000
VERIFICATION_TTL_MS = 15 * 60 * 1000
RESET_TTL_MS = 15 * 60 * 1000
RESEND_COOLDOWN_MS = 60 * 1000
MAX_CODE_ATTEMPTS = 6
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_\.]{3,24}$")

PLANS: dict[str, dict[str, Any]] = {
    "FREE": {
        "name": "Free", "price_monthly_usd": 0, "signal_window": "weekly", "signal_limit": 1,
        "allowed_signal_kinds": ["STANDARD"], "tracking_slots": 5, "email_alerts": False, "email_24_7": False,
        "whale_tools": False, "advanced_whales": False, "stealth_signals": False, "early_alerts": False,
        "personal_telegram": False, "sms_alerts": False, "api_access": False, "priority_delivery": False,
        "delayed_intelligence": True, "description": "شروع رایگان؛ هفته‌ای یک سیگنال منتخب + Tracking پایه.",
    },
    "PRO": {
        "name": "Pro", "price_monthly_usd": 49, "signal_window": "daily", "signal_limit": 10,
        "allowed_signal_kinds": ["STANDARD"], "tracking_slots": 25, "email_alerts": True, "email_24_7": True,
        "whale_tools": False, "advanced_whales": False, "stealth_signals": False, "early_alerts": False,
        "personal_telegram": False, "sms_alerts": False, "api_access": False, "priority_delivery": False,
        "delayed_intelligence": False, "description": "سیگنال‌های روزانه + هشدار ایمیلی 24/7؛ بدون To The Moon.",
    },
    "ELITE": {
        "name": "Elite", "price_monthly_usd": 299, "signal_window": "daily", "signal_limit": 50,
        "allowed_signal_kinds": ["STANDARD", "TO_THE_MOON", "STEALTH", "EARLY"], "tracking_slots": 200,
        "email_alerts": True, "email_24_7": True, "whale_tools": True, "advanced_whales": True,
        "stealth_signals": True, "early_alerts": True, "personal_telegram": True, "sms_alerts": True,
        "api_access": True, "priority_delivery": True, "delayed_intelligence": False,
        "description": "Elite کامل: To The Moon + Stealth + Early + رصد پیشرفته نهنگ‌ها + Telegram/SMS + API.",
    },
}

SIGNAL_PRIORITY = {"STANDARD": 1, "TO_THE_MOON": 2, "EARLY": 3, "STEALTH": 4}

DDL = """
CREATE TABLE IF NOT EXISTS saas_users_v36 (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    plan_code TEXT NOT NULL DEFAULT 'FREE',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    email_verified INTEGER NOT NULL DEFAULT 0,
    email_verified_at_ms INTEGER,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    phone_e164 TEXT,
    telegram_chat_id TEXT,
    avatar_seed TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    last_login_at_ms INTEGER,
    updated_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_saas_users_v36_email ON saas_users_v36(email);

CREATE TABLE IF NOT EXISTS saas_sessions_v36 (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_token_hash TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    last_seen_at_ms INTEGER NOT NULL,
    revoked_at_ms INTEGER,
    FOREIGN KEY(user_id) REFERENCES saas_users_v36(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_saas_sessions_v36_user ON saas_sessions_v36(user_id);

CREATE TABLE IF NOT EXISTS saas_watchlists_v36 (
    watchlist_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    symbols_json TEXT NOT NULL DEFAULT '[]',
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE(user_id, name),
    FOREIGN KEY(user_id) REFERENCES saas_users_v36(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_saas_watchlists_v36_user ON saas_watchlists_v36(user_id);

CREATE TABLE IF NOT EXISTS saas_preferences_v36 (
    user_id TEXT PRIMARY KEY,
    alert_min_score REAL NOT NULL DEFAULT 70,
    preferred_side TEXT NOT NULL DEFAULT 'BOTH',
    notify_browser INTEGER NOT NULL DEFAULT 1,
    notify_email INTEGER NOT NULL DEFAULT 1,
    notify_telegram INTEGER NOT NULL DEFAULT 0,
    notify_sms INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES saas_users_v36(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS saas_email_verifications_v36 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    sent_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    consumed_at_ms INTEGER,
    delivery_status TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES saas_users_v36(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_saas_email_verif_v36_user ON saas_email_verifications_v36(user_id, sent_at_ms);

CREATE TABLE IF NOT EXISTS saas_password_resets_v36 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    sent_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    consumed_at_ms INTEGER,
    delivery_status TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES saas_users_v36(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_saas_password_resets_v36_user ON saas_password_resets_v36(user_id, sent_at_ms);

CREATE TABLE IF NOT EXISTS saas_subscriptions_v36 (
    subscription_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    plan_code TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT 'internal',
    external_id TEXT,
    period_start_ms INTEGER,
    period_end_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE(provider, external_id),
    FOREIGN KEY(user_id) REFERENCES saas_users_v36(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_saas_subscriptions_v36_user ON saas_subscriptions_v36(user_id, status);

CREATE TABLE IF NOT EXISTS saas_signal_usage_v36 (
    usage_key TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    bucket_start_ms INTEGER NOT NULL,
    window_kind TEXT NOT NULL,
    delivered_count INTEGER NOT NULL DEFAULT 0,
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES saas_users_v36(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_saas_usage_v36_user ON saas_signal_usage_v36(user_id, bucket_start_ms);

CREATE TABLE IF NOT EXISTS saas_signal_delivery_v36 (
    delivery_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    signal_kind TEXT NOT NULL,
    channel TEXT NOT NULL,
    reserved_at_ms INTEGER NOT NULL,
    delivered_at_ms INTEGER,
    status TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(user_id, signal_id, channel),
    FOREIGN KEY(user_id) REFERENCES saas_users_v36(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_saas_delivery_v36_user ON saas_signal_delivery_v36(user_id, reserved_at_ms);

CREATE TABLE IF NOT EXISTS saas_user_alerts_v36 (
    user_alert_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    signal_kind TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    ts_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at_ms INTEGER NOT NULL,
    read_at_ms INTEGER,
    UNIQUE(user_id, signal_id),
    FOREIGN KEY(user_id) REFERENCES saas_users_v36(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_saas_user_alerts_v36_user_ts ON saas_user_alerts_v36(user_id, ts_ms DESC);

CREATE TABLE IF NOT EXISTS saas_audit_v36 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    user_id TEXT,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_saas_audit_v36_user_ts ON saas_audit_v36(user_id, ts_ms);

CREATE TABLE IF NOT EXISTS saas_meta_v36 (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hash_secret(value: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", value.encode(), salt, PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt.hex()}${digest.hex()}"


def _verify_secret(value: str, encoded: str) -> bool:
    try:
        alg, iters, salt_hex, digest_hex = encoded.split("$", 3)
        if alg != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", value.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _hash_code(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _normalize_username(username: str) -> str:
    return username.strip().lower()


def _six_digit_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _bucket_start(now_ms: int, window_kind: str) -> int:
    now_s = now_ms // 1000
    if window_kind == "weekly":
        # ISO week bucket, UTC; 604800-second buckets anchored at Thursday is avoided.
        day = (now_s // 86400)
        return ((day - 3) // 7) * 7 * 86400 * 1000
    return (now_ms // 86_400_000) * 86_400_000


@dataclass(frozen=True)
class SaaSPrincipalV36:
    user_id: str
    username: str
    email: str
    plan_code: str
    status: str
    email_verified: bool


class GmailEmailSenderV36:
    def __init__(self, host: str, port: int, username: str | None, password: str | None, from_email: str | None, mode: str = "smtp", timeout: float = 15.0):
        self.host = host
        self.port = int(port)
        self.username = username
        self.password = password
        self.from_email = from_email or username
        self.mode = (mode or "smtp").lower()
        self.timeout = max(2.0, float(timeout))

    @property
    def configured(self) -> bool:
        return self.mode == "console" or bool(self.username and self.password and self.from_email)

    def send(self, recipient: str, subject: str, text_body: str, html_body: str | None = None) -> dict[str, Any]:
        if self.mode == "console":
            print(f"[CFR-EMAIL-CONSOLE] recipient={recipient} subject={subject}\n{text_body}")
            return {"ok": True, "status": "CONSOLE"}
        if not self.configured:
            return {"ok": False, "status": "NOT_CONFIGURED", "error": "Gmail SMTP is not configured"}
        msg = EmailMessage()
        msg["From"] = self.from_email
        msg["To"] = recipient
        msg["Subject"] = subject
        msg.set_content(text_body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")
        try:
            with smtplib.SMTP(self.host, self.port, timeout=self.timeout) as smtp:
                smtp.ehlo()
                smtp.starttls()
                smtp.ehlo()
                smtp.login(self.username, self.password)
                smtp.send_message(msg)
            return {"ok": True, "status": "SENT"}
        except Exception as exc:
            return {"ok": False, "status": "FAILED", "error": str(exc)}


class SaaSStoreV36:
    def __init__(self, sqlite_path: str, session_ttl_seconds: int = 1_209_600, email_sender: GmailEmailSenderV36 | None = None, webhook_secret: str | None = None):
        self.sqlite_path = sqlite_path
        self.session_ttl_ms = max(900, int(session_ttl_seconds)) * 1000
        self.email_sender = email_sender or GmailEmailSenderV36("smtp.gmail.com", 587, None, None, None, mode="smtp")
        self.webhook_secret = webhook_secret
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(DDL)
            conn.execute("INSERT INTO saas_meta_v36(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("schema_version", SCHEMA_VERSION))

    def _audit(self, conn: sqlite3.Connection, user_id: str | None, action: str, payload: dict[str, Any]) -> None:
        conn.execute(
            "INSERT INTO saas_audit_v36(ts_ms,user_id,action,payload_json) VALUES(?,?,?,?)",
            (_now_ms(), user_id, action, json.dumps(payload, separators=(",", ":"), ensure_ascii=False)),
        )

    def create_user(self, username: str, email: str, password: str, display_name: str = "") -> tuple[dict[str, Any], str, dict[str, Any]]:
        username = _normalize_username(username)
        email = _normalize_email(email)
        if not USERNAME_RE.fullmatch(username):
            raise ValueError("username must be 3-24 chars: letters, numbers, underscore or dot")
        if "@" not in email or len(email) > 254:
            raise ValueError("invalid email")
        if len(password) < 10:
            raise ValueError("password must be at least 10 characters")
        now = _now_ms()
        user_id = secrets.token_hex(16)
        code = _six_digit_code()
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO saas_users_v36(user_id,username,email,password_hash,display_name,plan_code,status,email_verified,avatar_seed,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (user_id, username, email, _hash_secret(password), display_name.strip()[:80], "FREE", "ACTIVE", 0, secrets.token_hex(8), now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("username or email already registered") from exc
            conn.execute("INSERT INTO saas_preferences_v36(user_id,updated_at_ms) VALUES(?,?)", (user_id, now))
            conn.execute("UPDATE saas_email_verifications_v36 SET consumed_at_ms=? WHERE user_id=? AND consumed_at_ms IS NULL", (now, user_id))
            conn.execute(
                "INSERT INTO saas_email_verifications_v36(user_id,code_hash,sent_at_ms,expires_at_ms,delivery_status) VALUES(?,?,?,?,?)",
                (user_id, _hash_code(code), now, now + VERIFICATION_TTL_MS, "PENDING"),
            )
            self._audit(conn, user_id, "REGISTER", {"username": username, "email": email})
        delivery = self._send_verification(user_id, email, code)
        with self._conn() as conn:
            conn.execute("UPDATE saas_email_verifications_v36 SET delivery_status=? WHERE user_id=? AND consumed_at_ms IS NULL AND sent_at_ms=?", (delivery["status"], user_id, now))
        return self.public_user(user_id) or {}, code, delivery

    def _send_verification(self, user_id: str, email: str, code: str) -> dict[str, Any]:
        subject = "Verify your Crypto Flow Radar account"
        body = (
            "Your Crypto Flow Radar verification code is: " + code + "\n\n"
            "This code expires in 15 minutes. If you did not create this account, you can ignore this email."
        )
        result = self.email_sender.send(email, subject, body)
        return {"status": result.get("status", "FAILED"), "ok": bool(result.get("ok")), "error": result.get("error")}

    def resend_verification(self, email: str) -> tuple[dict[str, Any], str, dict[str, Any]]:
        email = _normalize_email(email)
        now = _now_ms()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM saas_users_v36 WHERE email=?", (email,)).fetchone()
            if not row:
                raise ValueError("account not found")
            if row["email_verified"]:
                raise ValueError("email already verified")
            latest = conn.execute("SELECT * FROM saas_email_verifications_v36 WHERE user_id=? ORDER BY sent_at_ms DESC LIMIT 1", (row["user_id"],)).fetchone()
            if latest and now - latest["sent_at_ms"] < RESEND_COOLDOWN_MS:
                remaining = (RESEND_COOLDOWN_MS - (now - latest["sent_at_ms"]) + 999) // 1000
                raise CooldownError(int(remaining))
            code = _six_digit_code()
            conn.execute("UPDATE saas_email_verifications_v36 SET consumed_at_ms=? WHERE user_id=? AND consumed_at_ms IS NULL", (now, row["user_id"]))
            conn.execute(
                "INSERT INTO saas_email_verifications_v36(user_id,code_hash,sent_at_ms,expires_at_ms,delivery_status) VALUES(?,?,?,?,?)",
                (row["user_id"], _hash_code(code), now, now + VERIFICATION_TTL_MS, "PENDING"),
            )
        delivery = self._send_verification(row["user_id"], email, code)
        with self._conn() as conn:
            conn.execute("UPDATE saas_email_verifications_v36 SET delivery_status=? WHERE user_id=? AND consumed_at_ms IS NULL AND sent_at_ms=?", (delivery["status"], row["user_id"], now))
        return self.public_user(row["user_id"]) or {}, code, delivery

    def verify_email(self, email: str, code: str) -> dict[str, Any]:
        email = _normalize_email(email)
        code = str(code).strip()
        now = _now_ms()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM saas_users_v36 WHERE email=?", (email,)).fetchone()
            if not row:
                raise ValueError("account not found")
            if row["email_verified"]:
                return self.public_user(row["user_id"]) or {}
            ver = conn.execute("SELECT * FROM saas_email_verifications_v36 WHERE user_id=? AND consumed_at_ms IS NULL ORDER BY sent_at_ms DESC LIMIT 1", (row["user_id"],)).fetchone()
            if not ver:
                raise ValueError("verification code not found")
            if ver["attempts"] >= MAX_CODE_ATTEMPTS:
                raise ValueError("too many verification attempts; resend a new code")
            if ver["expires_at_ms"] <= now:
                raise ValueError("verification code expired; resend a new code")
            if not hmac.compare_digest(ver["code_hash"], _hash_code(code)):
                conn.execute("UPDATE saas_email_verifications_v36 SET attempts=attempts+1 WHERE id=?", (ver["id"],))
                raise ValueError("invalid verification code")
            conn.execute("UPDATE saas_email_verifications_v36 SET consumed_at_ms=? WHERE id=?", (now, ver["id"]))
            conn.execute("UPDATE saas_users_v36 SET email_verified=1,email_verified_at_ms=?,updated_at_ms=? WHERE user_id=?", (now, now, row["user_id"]))
            self._audit(conn, row["user_id"], "EMAIL_VERIFIED", {})
            updated = conn.execute("SELECT * FROM saas_users_v36 WHERE user_id=?", (row["user_id"],)).fetchone()
            return self._public_user_row(updated) if updated else {}

    def authenticate(self, identifier: str, password: str) -> tuple[str, str, dict[str, Any]] | None:
        identifier = identifier.strip().lower()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM saas_users_v36 WHERE email=? OR username=?", (identifier, identifier)).fetchone()
            if not row or row["status"] != "ACTIVE" or not _verify_secret(password, row["password_hash"]):
                return None
            if not row["email_verified"]:
                latest = conn.execute("SELECT sent_at_ms FROM saas_email_verifications_v36 WHERE user_id=? ORDER BY sent_at_ms DESC LIMIT 1", (row["user_id"],)).fetchone()
                remaining = 0 if not latest else max(0, int((RESEND_COOLDOWN_MS - (_now_ms() - latest["sent_at_ms"]) + 999) // 1000))
                raise EmailVerificationRequired(row["email"], remaining)
            now = _now_ms()
            token, csrf = secrets.token_urlsafe(36), secrets.token_urlsafe(24)
            conn.execute("UPDATE saas_users_v36 SET last_login_at_ms=?,updated_at_ms=? WHERE user_id=?", (now, now, row["user_id"]))
            conn.execute(
                "INSERT INTO saas_sessions_v36(session_id,user_id,token_hash,csrf_token_hash,created_at_ms,expires_at_ms,last_seen_at_ms) VALUES(?,?,?,?,?,?,?)",
                (secrets.token_hex(16), row["user_id"], _hash_token(token), _hash_token(csrf), now, now + self.session_ttl_ms, now),
            )
            self._audit(conn, row["user_id"], "LOGIN", {})
            return token, csrf, self._public_user_row(row)

    def principal_from_token(self, token: str | None) -> tuple[SaaSPrincipalV36, str] | None:
        if not token:
            return None
        now = _now_ms()
        with self._conn() as conn:
            row = conn.execute("SELECT s.session_id,s.user_id,s.expires_at_ms,u.username,u.email,u.plan_code,u.status,u.email_verified FROM saas_sessions_v36 s JOIN saas_users_v36 u ON u.user_id=s.user_id WHERE s.token_hash=? AND s.revoked_at_ms IS NULL", (_hash_token(token),)).fetchone()
            if not row or row["expires_at_ms"] <= now or row["status"] != "ACTIVE" or not row["email_verified"]:
                return None
            conn.execute("UPDATE saas_sessions_v36 SET last_seen_at_ms=? WHERE session_id=?", (now, row["session_id"]))
            return SaaSPrincipalV36(row["user_id"], row["username"], row["email"], row["plan_code"], row["status"], bool(row["email_verified"])), row["session_id"]

    def rotate_csrf(self, session_id: str) -> str | None:
        token = secrets.token_urlsafe(24)
        with self._conn() as conn:
            row = conn.execute("SELECT session_id FROM saas_sessions_v36 WHERE session_id=? AND revoked_at_ms IS NULL AND expires_at_ms>?", (session_id, _now_ms())).fetchone()
            if not row:
                return None
            conn.execute("UPDATE saas_sessions_v36 SET csrf_token_hash=? WHERE session_id=?", (_hash_token(token), session_id))
        return token

    def validate_csrf(self, session_id: str, csrf_token: str | None) -> bool:
        if not csrf_token:
            return False
        with self._conn() as conn:
            row = conn.execute("SELECT csrf_token_hash,expires_at_ms,revoked_at_ms FROM saas_sessions_v36 WHERE session_id=?", (session_id,)).fetchone()
            return bool(row and not row["revoked_at_ms"] and row["expires_at_ms"] > _now_ms() and hmac.compare_digest(row["csrf_token_hash"], _hash_token(csrf_token)))

    def logout(self, session_id: str, user_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE saas_sessions_v36 SET revoked_at_ms=? WHERE session_id=?", (_now_ms(), session_id))
            self._audit(conn, user_id, "LOGOUT", {})

    def public_user(self, user_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM saas_users_v36 WHERE user_id=?", (user_id,)).fetchone()
            return self._public_user_row(row) if row else None

    def _public_user_row(self, row: sqlite3.Row) -> dict[str, Any]:
        normalized_plan = "ELITE" if row["plan_code"] == "LEGENDARY" else row["plan_code"]
        plan = PLANS.get(normalized_plan, PLANS["FREE"])
        return {
            "user_id": row["user_id"],
            "username": row["username"],
            "email": row["email"],
            "display_name": row["display_name"],
            "plan_code": normalized_plan,
            "plan": plan["name"],
            "status": row["status"],
            "email_verified": bool(row["email_verified"]),
            "email_verified_at_ms": row["email_verified_at_ms"],
            "timezone": row["timezone"],
            "phone_e164": row["phone_e164"],
            "telegram_chat_id": row["telegram_chat_id"],
            "created_at_ms": row["created_at_ms"],
            "last_login_at_ms": row["last_login_at_ms"],
            "entitlements": plan,
            "signal_usage": self.signal_usage(row["user_id"]),
        }

    def update_profile(self, user_id: str, *, username: str | None = None, display_name: str | None = None, timezone: str | None = None, phone_e164: str | None = None, telegram_chat_id: str | None = None) -> dict[str, Any]:
        fields: list[str] = []
        values: list[Any] = []
        now = _now_ms()
        if username is not None:
            username = _normalize_username(username)
            if not USERNAME_RE.fullmatch(username):
                raise ValueError("invalid username")
            fields.append("username=?"); values.append(username)
        if display_name is not None:
            fields.append("display_name=?"); values.append(display_name.strip()[:80])
        if timezone is not None:
            fields.append("timezone=?"); values.append(timezone.strip()[:64] or "UTC")
        if phone_e164 is not None:
            phone_e164 = phone_e164.strip()[:24]
            fields.append("phone_e164=?"); values.append(phone_e164 or None)
        if telegram_chat_id is not None:
            fields.append("telegram_chat_id=?"); values.append(telegram_chat_id.strip()[:80] or None)
        if not fields:
            return self.public_user(user_id) or {}
        fields.append("updated_at_ms=?"); values.append(now); values.append(user_id)
        with self._conn() as conn:
            try:
                conn.execute(f"UPDATE saas_users_v36 SET {', '.join(fields)} WHERE user_id=?", values)
            except sqlite3.IntegrityError as exc:
                raise ValueError("username already in use") from exc
            self._audit(conn, user_id, "PROFILE_UPDATED", {"fields": [f.split("=")[0] for f in fields]})
        return self.public_user(user_id) or {}

    def set_plan(self, user_id: str, plan_code: str, provider: str = "internal", external_id: str | None = None, period_start_ms: int | None = None, period_end_ms: int | None = None, status: str = "ACTIVE") -> dict[str, Any]:
        plan_code = plan_code.upper()
        if plan_code == "LEGENDARY":
            plan_code = "ELITE"
        if plan_code not in PLANS:
            raise ValueError("unsupported plan")
        now = _now_ms()
        with self._conn() as conn:
            conn.execute("UPDATE saas_users_v36 SET plan_code=?,updated_at_ms=? WHERE user_id=?", (plan_code, now, user_id))
            conn.execute("UPDATE saas_subscriptions_v36 SET status='REPLACED',updated_at_ms=? WHERE user_id=? AND status='ACTIVE'", (now, user_id))
            sub_id = secrets.token_hex(16)
            conn.execute("INSERT INTO saas_subscriptions_v36(subscription_id,user_id,plan_code,status,provider,external_id,period_start_ms,period_end_ms,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,?,?,?)", (sub_id,user_id,plan_code,status,provider,external_id,period_start_ms,period_end_ms,now,now))
            self._audit(conn, user_id, "PLAN_CHANGED", {"plan": plan_code, "provider": provider, "external_id": external_id})
        return self.public_user(user_id) or {}

    def subscription(self, user_id: str) -> dict[str, Any]:
        user = self.public_user(user_id) or {}
        with self._conn() as conn:
            sub = conn.execute("SELECT * FROM saas_subscriptions_v36 WHERE user_id=? ORDER BY created_at_ms DESC LIMIT 1", (user_id,)).fetchone()
        return {"plan": PLANS[user.get("plan_code", "FREE")], "plan_code": user.get("plan_code", "FREE"), "status": sub["status"] if sub else "ACTIVE", "provider": sub["provider"] if sub else "internal", "external_id": sub["external_id"] if sub else None, "period_start_ms": sub["period_start_ms"] if sub else None, "period_end_ms": sub["period_end_ms"] if sub else None}

    def signal_usage(self, user_id: str) -> dict[str, Any]:
        user = self.public_user_basic(user_id)
        plan = PLANS[user["plan_code"]]
        now = _now_ms()
        kind = plan["signal_window"]
        bucket = _bucket_start(now, kind)
        with self._conn() as conn:
            row = conn.execute("SELECT delivered_count FROM saas_signal_usage_v36 WHERE usage_key=?", (f"{user_id}:{kind}:{bucket}",)).fetchone()
        used = int(row["delivered_count"]) if row else 0
        return {"window": kind, "limit": plan["signal_limit"], "used": used, "remaining": max(0, plan["signal_limit"] - used), "reset_at_ms": bucket + (604_800_000 if kind == "weekly" else 86_400_000), "allowed_signal_kinds": plan["allowed_signal_kinds"], "tracking_slots": plan["tracking_slots"]}

    def public_user_basic(self, user_id: str) -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute("SELECT user_id,username,email,plan_code FROM saas_users_v36 WHERE user_id=?", (user_id,)).fetchone()
            if not row:
                raise ValueError("user not found")
            d=dict(row)
            if d.get("plan_code")=="LEGENDARY": d["plan_code"]="ELITE"
            return d

    def can_reserve_signal(self, user_id: str, signal_kind: str) -> tuple[bool, dict[str, Any]]:
        signal_kind = signal_kind.upper()
        user = self.public_user_basic(user_id)
        plan = PLANS[user["plan_code"]]
        usage = self.signal_usage(user_id)
        allowed = signal_kind in plan["allowed_signal_kinds"]
        return allowed and usage["remaining"] > 0, {**usage, "reason": None if allowed and usage["remaining"] > 0 else ("SIGNAL_KIND_LOCKED" if not allowed else "SIGNAL_QUOTA_EXHAUSTED")}

    def reserve_signal(self, user_id: str, signal_id: str, signal_kind: str, channel: str) -> dict[str, Any]:
        ok, detail = self.can_reserve_signal(user_id, signal_kind)
        channel = channel.upper()
        if not ok:
            raise EntitlementError(detail["reason"], detail)
        plan = PLANS[self.public_user_basic(user_id)["plan_code"]]
        channel_allowed = {"IN_APP": True, "EMAIL": plan["email_alerts"], "TELEGRAM": plan["personal_telegram"], "SMS": plan["sms_alerts"]}.get(channel, False)
        if not channel_allowed:
            raise EntitlementError("CHANNEL_LOCKED", {"channel": channel, "plan": plan["name"]})
        now = _now_ms(); bucket = _bucket_start(now, plan["signal_window"]); usage_key = f"{user_id}:{plan['signal_window']}:{bucket}"
        with self._conn() as conn:
            try:
                conn.execute("INSERT INTO saas_signal_delivery_v36(delivery_id,user_id,signal_id,signal_kind,channel,reserved_at_ms,status) VALUES(?,?,?,?,?,?,?)", (secrets.token_hex(16),user_id,signal_id,signal_kind,channel,now,"RESERVED"))
            except sqlite3.IntegrityError as exc:
                raise ValueError("signal already reserved for this user/channel") from exc
            already = conn.execute("SELECT 1 FROM saas_signal_delivery_v36 WHERE user_id=? AND signal_id=? AND channel<>? LIMIT 1", (user_id, signal_id, channel)).fetchone()
            if not already:
                conn.execute("INSERT INTO saas_signal_usage_v36(usage_key,user_id,bucket_start_ms,window_kind,delivered_count,updated_at_ms) VALUES(?,?,?,?,1,?) ON CONFLICT(usage_key) DO UPDATE SET delivered_count=delivered_count+1,updated_at_ms=excluded.updated_at_ms", (usage_key,user_id,bucket,plan["signal_window"],now))
            self._audit(conn, user_id, "SIGNAL_RESERVED", {"signal_id": signal_id, "kind": signal_kind, "channel": channel})
        return self.signal_usage(user_id)

    def mark_delivery(self, user_id: str, signal_id: str, channel: str, status: str = "DELIVERED") -> None:
        with self._conn() as conn:
            conn.execute("UPDATE saas_signal_delivery_v36 SET status=?,delivered_at_ms=? WHERE user_id=? AND signal_id=? AND channel=?", (status, _now_ms(), user_id, signal_id, channel.upper()))

    def list_watchlists(self, user_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM saas_watchlists_v36 WHERE user_id=? ORDER BY updated_at_ms DESC", (user_id,)).fetchall()
        return [{"watchlist_id": r["watchlist_id"], "name": r["name"], "symbols": json.loads(r["symbols_json"]), "created_at_ms": r["created_at_ms"], "updated_at_ms": r["updated_at_ms"]} for r in rows]

    def _tracking_count(self, user_id: str) -> int:
        return sum(len(x["symbols"]) for x in self.list_watchlists(user_id))

    def create_watchlist(self, user_id: str, name: str) -> dict[str, Any]:
        name = name.strip()[:48]
        if not name: raise ValueError("watchlist name required")
        plan = PLANS[self.public_user_basic(user_id)["plan_code"]]
        rows = self.list_watchlists(user_id)
        if len(rows) >= max(1, plan["tracking_slots"] // 5):
            raise EntitlementError("WATCHLIST_LIMIT", {"limit": max(1, plan["tracking_slots"] // 5)})
        now = _now_ms(); wid = secrets.token_hex(16)
        with self._conn() as conn:
            try:
                conn.execute("INSERT INTO saas_watchlists_v36(watchlist_id,user_id,name,symbols_json,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?)", (wid,user_id,name,"[]",now,now))
            except sqlite3.IntegrityError as exc:
                raise ValueError("watchlist name already exists") from exc
            self._audit(conn,user_id,"WATCHLIST_CREATED",{"name":name})
        return next(x for x in self.list_watchlists(user_id) if x["watchlist_id"]==wid)

    def update_watchlist(self, user_id: str, watchlist_id: str, name: str | None = None, symbols: list[str] | None = None) -> dict[str, Any]:
        current = next((x for x in self.list_watchlists(user_id) if x["watchlist_id"] == watchlist_id), None)
        if not current: raise ValueError("watchlist not found")
        plan = PLANS[self.public_user_basic(user_id)["plan_code"]]
        fields=[]; values=[]; now=_now_ms()
        if name is not None:
            fields.append("name=?"); values.append(name.strip()[:48])
        if symbols is not None:
            normalized=[]; seen=set()
            for sym in symbols:
                s=str(sym).strip().upper()
                if s and s not in seen: seen.add(s); normalized.append(s)
            if len(normalized) > plan["tracking_slots"]: raise EntitlementError("TRACKING_LIMIT", {"limit":plan["tracking_slots"],"requested":len(normalized)})
            other = self._tracking_count(user_id) - len(current["symbols"])
            if other + len(normalized) > plan["tracking_slots"]: raise EntitlementError("TRACKING_LIMIT", {"limit":plan["tracking_slots"],"requested":other+len(normalized)})
            fields.append("symbols_json=?"); values.append(json.dumps(normalized, separators=(",", ":")))
        if fields:
            fields.append("updated_at_ms=?"); values.append(now); values.extend([watchlist_id,user_id])
            with self._conn() as conn:
                try: conn.execute(f"UPDATE saas_watchlists_v36 SET {', '.join(fields)} WHERE watchlist_id=? AND user_id=?", values)
                except sqlite3.IntegrityError as exc: raise ValueError("watchlist name already exists") from exc
                self._audit(conn,user_id,"WATCHLIST_UPDATED",{"watchlist_id":watchlist_id})
        return next(x for x in self.list_watchlists(user_id) if x["watchlist_id"]==watchlist_id)

    def delete_watchlist(self, user_id: str, watchlist_id: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM saas_watchlists_v36 WHERE watchlist_id=? AND user_id=?", (watchlist_id,user_id))
            self._audit(conn,user_id,"WATCHLIST_DELETED",{"watchlist_id":watchlist_id})

    def preferences(self, user_id: str) -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM saas_preferences_v36 WHERE user_id=?", (user_id,)).fetchone()
        if not row: return {}
        return {"alert_min_score": row["alert_min_score"], "preferred_side": row["preferred_side"], "notify_browser": bool(row["notify_browser"]), "notify_email": bool(row["notify_email"]), "notify_telegram": bool(row["notify_telegram"]), "notify_sms": bool(row["notify_sms"])}

    def update_preferences(self, user_id: str, values: dict[str, Any]) -> dict[str, Any]:
        plan = PLANS[self.public_user_basic(user_id)["plan_code"]]
        if values.get("notify_email") and not plan["email_alerts"]: values["notify_email"] = False
        if values.get("notify_telegram") and not plan["personal_telegram"]: values["notify_telegram"] = False
        if values.get("notify_sms") and not plan["sms_alerts"]: values["notify_sms"] = False
        side = str(values.get("preferred_side", "BOTH")).upper()
        if side not in {"BOTH","LONG","SHORT"}: raise ValueError("preferred_side must be BOTH/LONG/SHORT")
        with self._conn() as conn:
            conn.execute("UPDATE saas_preferences_v36 SET alert_min_score=?,preferred_side=?,notify_browser=?,notify_email=?,notify_telegram=?,notify_sms=?,updated_at_ms=? WHERE user_id=?", (float(values.get("alert_min_score",70)),side,int(bool(values.get("notify_browser",True))),int(bool(values.get("notify_email",plan["email_alerts"]))),int(bool(values.get("notify_telegram",False))),int(bool(values.get("notify_sms",False))),_now_ms(),user_id))
            self._audit(conn,user_id,"PREFERENCES_UPDATED",{})
        return self.preferences(user_id)

    def eligible_users(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT user_id,username,email,plan_code,phone_e164,telegram_chat_id FROM saas_users_v36 WHERE status='ACTIVE' AND email_verified=1").fetchall()
        return [dict(r) for r in rows]

    def record_user_alert(self, user_id: str, signal_id: str, signal_kind: str, symbol: str, direction: str, ts_ms: int, payload: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO saas_user_alerts_v36(user_alert_id,user_id,signal_id,signal_kind,symbol,direction,ts_ms,payload_json,created_at_ms) VALUES(?,?,?,?,?,?,?,?,?)", (secrets.token_hex(16),user_id,signal_id,signal_kind.upper(),symbol.lower(),direction.lower(),int(ts_ms),json.dumps(payload,separators=(",",":"),ensure_ascii=False),_now_ms()))

    def user_alerts(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        limit=max(1,min(int(limit),200))
        with self._conn() as conn:
            rows=conn.execute("SELECT * FROM saas_user_alerts_v36 WHERE user_id=? ORDER BY ts_ms DESC LIMIT ?",(user_id,limit)).fetchall()
        out=[]
        for r in rows:
            d=dict(r); d["payload"]=json.loads(d.pop("payload_json") or "{}"); out.append(d)
        return out

    def forgot_password(self, email: str) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        email = _normalize_email(email); now = _now_ms()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM saas_users_v36 WHERE email=?", (email,)).fetchone()
            if not row:
                return None, "", {"status": "NOT_FOUND", "ok": False}
            latest = conn.execute("SELECT * FROM saas_password_resets_v36 WHERE user_id=? ORDER BY sent_at_ms DESC LIMIT 1", (row["user_id"],)).fetchone()
            if latest and now - latest["sent_at_ms"] < RESEND_COOLDOWN_MS:
                remaining = (RESEND_COOLDOWN_MS - (now - latest["sent_at_ms"]) + 999) // 1000
                raise CooldownError(int(remaining))
            code = _six_digit_code()
            conn.execute("INSERT INTO saas_password_resets_v36(user_id,code_hash,sent_at_ms,expires_at_ms,delivery_status) VALUES(?,?,?,?,?)", (row["user_id"],_hash_code(code),now,now+RESET_TTL_MS,"PENDING"))
        delivery = self.email_sender.send(email, "Reset your Crypto Flow Radar password", f"Your password reset code is: {code}\n\nThis code expires in 15 minutes.")
        with self._conn() as conn:
            conn.execute("UPDATE saas_password_resets_v36 SET delivery_status=? WHERE user_id=? AND sent_at_ms=?", (delivery.get("status","FAILED"), row["user_id"], now))
        return self.public_user(row["user_id"]), code, delivery

    def reset_password(self, email: str, code: str, new_password: str) -> dict[str, Any]:
        if len(new_password) < 10:
            raise ValueError("password must be at least 10 characters")
        email = _normalize_email(email); now = _now_ms()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM saas_users_v36 WHERE email=?", (email,)).fetchone()
            if not row: raise ValueError("account not found")
            reset = conn.execute("SELECT * FROM saas_password_resets_v36 WHERE user_id=? AND consumed_at_ms IS NULL ORDER BY sent_at_ms DESC LIMIT 1", (row["user_id"],)).fetchone()
            if not reset or reset["expires_at_ms"] <= now: raise ValueError("reset code expired or missing")
            if reset["attempts"] >= MAX_CODE_ATTEMPTS: raise ValueError("too many reset attempts")
            if not hmac.compare_digest(reset["code_hash"], _hash_code(str(code).strip())):
                conn.execute("UPDATE saas_password_resets_v36 SET attempts=attempts+1 WHERE id=?", (reset["id"],))
                raise ValueError("invalid reset code")
            conn.execute("UPDATE saas_password_resets_v36 SET consumed_at_ms=? WHERE id=?", (now, reset["id"]))
            conn.execute("UPDATE saas_users_v36 SET password_hash=?,updated_at_ms=? WHERE user_id=?", (_hash_secret(new_password),now,row["user_id"]))
            conn.execute("UPDATE saas_sessions_v36 SET revoked_at_ms=? WHERE user_id=? AND revoked_at_ms IS NULL", (now,row["user_id"]))
            self._audit(conn,row["user_id"],"PASSWORD_RESET",{})
        return self.public_user(row["user_id"]) or {}

    def billing_webhook_valid(self, raw_body: bytes, signature: str | None) -> bool:
        if not self.webhook_secret:
            return False
        expected = hmac.new(self.webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return bool(signature and hmac.compare_digest(expected, signature))

    def apply_billing_event(self, event: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event.get("type", "")); data = event.get("data") or {}
        user_id = str(data.get("user_id", ""))
        plan_code = str(data.get("plan_code", "FREE")).upper()
        if not user_id or plan_code not in PLANS:
            raise ValueError("invalid billing event")
        status = str(data.get("status", "ACTIVE"))
        if event_type in {"subscription.active", "subscription.renewed", "subscription.plan_changed"}:
            return self.set_plan(user_id, plan_code, provider=str(data.get("provider","webhook")), external_id=data.get("external_id"), period_start_ms=data.get("period_start_ms"), period_end_ms=data.get("period_end_ms"), status=status)
        if event_type in {"subscription.canceled", "subscription.expired"}:
            return self.set_plan(user_id, "FREE", provider=str(data.get("provider","webhook")), external_id=data.get("external_id"), period_start_ms=data.get("period_start_ms"), period_end_ms=data.get("period_end_ms"), status="CANCELED" if event_type.endswith("canceled") else "EXPIRED")
        raise ValueError("unsupported billing event")



class SaaSNotificationRouterV36:
    def __init__(self, store: SaaSStoreV36, email_sender: GmailEmailSenderV36, telegram_bot_token: str | None = None, twilio_account_sid: str | None = None, twilio_auth_token: str | None = None, twilio_from_number: str | None = None):
        self.store = store
        self.email_sender = email_sender
        self.telegram_bot_token = telegram_bot_token
        self.twilio_account_sid = twilio_account_sid
        self.twilio_auth_token = twilio_auth_token
        self.twilio_from_number = twilio_from_number

    def _alert_kind(self, event: Any) -> str:
        return str(getattr(event, "signal_kind", None) or "STANDARD").upper()

    def _email_body(self, event: Any, kind: str) -> str:
        return (f"Crypto Flow Radar — {kind} Alert\n\n"
                f"{str(getattr(event,'symbol','')).upper()} | {str(getattr(event,'direction','')).upper()}\n"
                f"Fusion: {float(getattr(event,'fusion_score',0)):.1f} | Risk: {float(getattr(event,'risk_score',0)):.1f}\n"
                f"EV(R): {float(getattr(event,'expected_value_r',0)):.2f} | Net RR: {float(getattr(event,'net_rr',0)):.2f}\n"
                f"Entry reference: {float(getattr(event,'price',0)):.8g}\n\n"
                "This is a market-intelligence alert, not a guarantee of outcome.")

    async def _telegram(self, chat_id: str, event: Any, kind: str) -> tuple[bool, str | None]:
        if not self.telegram_bot_token or not chat_id:
            return False, "telegram_not_configured"
        import httpx
        text = (f"🚨 CFR {kind}\n{str(getattr(event,'symbol','')).upper()} | {str(getattr(event,'direction','')).upper()}\n"
                f"Fusion {float(getattr(event,'fusion_score',0)):.1f} | Risk {float(getattr(event,'risk_score',0)):.1f}\n"
                f"EV(R) {float(getattr(event,'expected_value_r',0)):.2f} | RR {float(getattr(event,'net_rr',0)):.2f}")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage", json={"chat_id": chat_id, "text": text})
                r.raise_for_status()
            return True, None
        except Exception as exc:
            return False, str(exc)

    async def _sms(self, phone: str, event: Any, kind: str) -> tuple[bool, str | None]:
        if not (self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number and phone):
            return False, "sms_not_configured"
        import httpx
        text = (f"CFR {kind}: {str(getattr(event,'symbol','')).upper()} {str(getattr(event,'direction','')).upper()} | "
                f"Fusion {float(getattr(event,'fusion_score',0)):.0f} | Risk {float(getattr(event,'risk_score',0)):.0f}")
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"https://api.twilio.com/2010-04-01/Accounts/{self.twilio_account_sid}/Messages.json",
                    data={"To": phone, "From": self.twilio_from_number, "Body": text},
                    auth=(self.twilio_account_sid, self.twilio_auth_token),
                )
                r.raise_for_status()
            return True, None
        except Exception as exc:
            return False, str(exc)

    async def route_alert(self, event: Any) -> dict[str, int]:
        if str(getattr(event, "action", "")) != "ALERT":
            return {"eligible": 0, "in_app": 0, "email": 0, "telegram": 0, "sms": 0}
        kind = self._alert_kind(event)
        payload = event.as_dict() if hasattr(event, "as_dict") else dict(event)
        counts = {"eligible": 0, "in_app": 0, "email": 0, "telegram": 0, "sms": 0}
        for user in self.store.eligible_users():
            counts["eligible"] += 1
            allowed, detail = self.store.can_reserve_signal(user["user_id"], kind)
            if not allowed:
                continue
            try:
                self.store.reserve_signal(user["user_id"], str(getattr(event, "alert_id", "signal")), kind, "IN_APP")
            except (EntitlementError, ValueError):
                continue
            self.store.record_user_alert(user["user_id"], str(getattr(event, "alert_id", "signal")), kind, str(getattr(event,"symbol","")), str(getattr(event,"direction","")), int(getattr(event,"ts_ms",_now_ms())), payload)
            counts["in_app"] += 1
            ent = PLANS[user["plan_code"]]
            prefs = self.store.preferences(user["user_id"])
            if prefs.get("notify_email") and ent["email_alerts"]:
                try:
                    self.store.reserve_signal(user["user_id"], str(getattr(event,"alert_id","signal")), kind, "EMAIL")
                    sent = self.email_sender.send(user["email"], f"CFR {kind} Alert — {str(getattr(event,'symbol','')).upper()}", self._email_body(event, kind))
                    self.store.mark_delivery(user["user_id"], str(getattr(event,"alert_id","signal")), "EMAIL", "DELIVERED" if sent.get("ok") else "FAILED")
                    if sent.get("ok"): counts["email"] += 1
                except (EntitlementError, ValueError):
                    pass
            if prefs.get("notify_telegram") and ent["personal_telegram"] and user.get("telegram_chat_id"):
                try:
                    self.store.reserve_signal(user["user_id"], str(getattr(event,"alert_id","signal")), kind, "TELEGRAM")
                    sent, _ = await self._telegram(user["telegram_chat_id"], event, kind)
                    self.store.mark_delivery(user["user_id"], str(getattr(event,"alert_id","signal")), "TELEGRAM", "DELIVERED" if sent else "FAILED")
                    if sent: counts["telegram"] += 1
                except (EntitlementError, ValueError):
                    pass
            if prefs.get("notify_sms") and ent["sms_alerts"] and user.get("phone_e164"):
                try:
                    self.store.reserve_signal(user["user_id"], str(getattr(event,"alert_id","signal")), kind, "SMS")
                    sent, _ = await self._sms(user["phone_e164"], event, kind)
                    self.store.mark_delivery(user["user_id"], str(getattr(event,"alert_id","signal")), "SMS", "DELIVERED" if sent else "FAILED")
                    if sent: counts["sms"] += 1
                except (EntitlementError, ValueError):
                    pass
        return counts

class CooldownError(ValueError):
    def __init__(self, remaining_seconds: int):
        super().__init__(f"resend available in {remaining_seconds}s")
        self.remaining_seconds = remaining_seconds


class EmailVerificationRequired(ValueError):
    def __init__(self, email: str, remaining_seconds: int = 0):
        super().__init__("email verification required")
        self.email = email
        self.remaining_seconds = int(max(0, remaining_seconds))


class EntitlementError(ValueError):
    def __init__(self, reason: str, detail: dict[str, Any]):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
