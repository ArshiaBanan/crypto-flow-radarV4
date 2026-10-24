from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

SCHEMA_VERSION = "saas_v35"
PBKDF2_ITERS = 210_000

PLANS = {
    "RESEARCH": {
        "name": "Research",
        "price_monthly": 0,
        "max_watchlists": 1,
        "max_symbols_per_watchlist": 10,
        "alerts": False,
        "external_intelligence": False,
        "paper_lab": True,
        "telegram": False,
        "api_access": False,
    },
    "PRO": {
        "name": "Pro Terminal",
        "price_monthly": 49,
        "max_watchlists": 10,
        "max_symbols_per_watchlist": 80,
        "alerts": True,
        "external_intelligence": True,
        "paper_lab": True,
        "telegram": True,
        "api_access": False,
    },
    "DESK": {
        "name": "Desk",
        "price_monthly": None,
        "max_watchlists": 100,
        "max_symbols_per_watchlist": 500,
        "alerts": True,
        "external_intelligence": True,
        "paper_lab": True,
        "telegram": True,
        "api_access": True,
    },
}

DDL = """
CREATE TABLE IF NOT EXISTS saas_users_v35 (
    user_id TEXT PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    plan_code TEXT NOT NULL DEFAULT 'RESEARCH',
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    created_at_ms INTEGER NOT NULL,
    last_login_at_ms INTEGER
);
CREATE INDEX IF NOT EXISTS ix_saas_users_email ON saas_users_v35(email);

CREATE TABLE IF NOT EXISTS saas_sessions_v35 (
    session_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    csrf_token_hash TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    last_seen_at_ms INTEGER NOT NULL,
    revoked_at_ms INTEGER,
    FOREIGN KEY(user_id) REFERENCES saas_users_v35(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_saas_sessions_user ON saas_sessions_v35(user_id);
CREATE INDEX IF NOT EXISTS ix_saas_sessions_expiry ON saas_sessions_v35(expires_at_ms);

CREATE TABLE IF NOT EXISTS saas_watchlists_v35 (
    watchlist_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    symbols_json TEXT NOT NULL DEFAULT '[]',
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE(user_id, name),
    FOREIGN KEY(user_id) REFERENCES saas_users_v35(user_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS ix_saas_watchlists_user ON saas_watchlists_v35(user_id);

CREATE TABLE IF NOT EXISTS saas_preferences_v35 (
    user_id TEXT PRIMARY KEY,
    alert_min_score REAL NOT NULL DEFAULT 70,
    preferred_side TEXT NOT NULL DEFAULT 'BOTH',
    notify_browser INTEGER NOT NULL DEFAULT 1,
    notify_telegram INTEGER NOT NULL DEFAULT 0,
    quiet_hours_json TEXT NOT NULL DEFAULT '{"enabled":false,"start":"22:00","end":"07:00"}',
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY(user_id) REFERENCES saas_users_v35(user_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS saas_audit_v35 (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_ms INTEGER NOT NULL,
    user_id TEXT,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_saas_audit_user_ts ON saas_audit_v35(user_id, ts_ms);

CREATE TABLE IF NOT EXISTS saas_meta_v35 (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _now_ms() -> int:
    return int(time.time() * 1000)


def _hash_secret(value: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", value.encode(), salt, PBKDF2_ITERS)
    return f"pbkdf2_sha256${PBKDF2_ITERS}${salt.hex()}${dk.hex()}"


def _verify_secret(value: str, encoded: str) -> bool:
    try:
        alg, iters, salt_hex, digest_hex = encoded.split("$", 3)
        if alg != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", value.encode(), bytes.fromhex(salt_hex), int(iters))
        return hmac.compare_digest(dk.hex(), digest_hex)
    except Exception:
        return False


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass(frozen=True)
class SaaSPrincipal:
    user_id: str
    email: str
    plan_code: str
    status: str


class SaaSStoreV35:
    def __init__(self, sqlite_path: str, session_ttl_seconds: int = 60 * 60 * 24 * 14):
        self.sqlite_path = sqlite_path
        self.session_ttl_ms = max(900, int(session_ttl_seconds)) * 1000
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.sqlite_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(DDL)
            conn.execute("INSERT INTO saas_meta_v35(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("schema_version", SCHEMA_VERSION))

    def _audit(self, conn: sqlite3.Connection, user_id: str | None, action: str, payload: dict[str, Any]) -> None:
        conn.execute("INSERT INTO saas_audit_v35(ts_ms,user_id,action,payload_json) VALUES(?,?,?,?)", (_now_ms(), user_id, action, json.dumps(payload, separators=(",", ":"), ensure_ascii=False)))

    def create_user(self, email: str, password: str, plan_code: str = "RESEARCH") -> dict[str, Any]:
        email = email.strip().lower()
        if "@" not in email or len(email) > 254:
            raise ValueError("invalid email")
        if len(password) < 10:
            raise ValueError("password must be at least 10 characters")
        plan_code = plan_code.upper()
        if plan_code not in PLANS:
            raise ValueError("unsupported plan")
        now = _now_ms()
        user_id = secrets.token_hex(16)
        with self._conn() as conn:
            try:
                conn.execute(
                    "INSERT INTO saas_users_v35(user_id,email,password_hash,plan_code,status,created_at_ms) VALUES(?,?,?,?,?,?)",
                    (user_id, email, _hash_secret(password), plan_code, "ACTIVE", now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("email already registered") from exc
            conn.execute(
                "INSERT INTO saas_preferences_v35(user_id,updated_at_ms) VALUES(?,?)",
                (user_id, now),
            )
            self._audit(conn, user_id, "REGISTER", {"email": email, "plan": plan_code})
        return self.public_user(user_id) or {}

    def authenticate(self, email: str, password: str) -> tuple[str, str, dict[str, Any]] | None:
        email = email.strip().lower()
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM saas_users_v35 WHERE email=?", (email,)).fetchone()
            if not row or row["status"] != "ACTIVE" or not _verify_secret(password, row["password_hash"]):
                return None
            now = _now_ms()
            conn.execute("UPDATE saas_users_v35 SET last_login_at_ms=? WHERE user_id=?", (now, row["user_id"]))
            token = secrets.token_urlsafe(36)
            csrf = secrets.token_urlsafe(24)
            conn.execute(
                "INSERT INTO saas_sessions_v35(session_id,user_id,token_hash,csrf_token_hash,created_at_ms,expires_at_ms,last_seen_at_ms) VALUES(?,?,?,?,?,?,?)",
                (secrets.token_hex(16), row["user_id"], _hash_token(token), _hash_token(csrf), now, now + self.session_ttl_ms, now),
            )
            self._audit(conn, row["user_id"], "LOGIN", {})
            return token, csrf, self._public_user_row(row)

    def principal_from_token(self, token: str | None) -> tuple[SaaSPrincipal, str] | None:
        if not token:
            return None
        now = _now_ms()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT s.session_id,s.user_id,s.expires_at_ms,u.email,u.plan_code,u.status FROM saas_sessions_v35 s JOIN saas_users_v35 u ON u.user_id=s.user_id WHERE s.token_hash=? AND s.revoked_at_ms IS NULL",
                (_hash_token(token),),
            ).fetchone()
            if not row or row["expires_at_ms"] <= now or row["status"] != "ACTIVE":
                return None
            conn.execute("UPDATE saas_sessions_v35 SET last_seen_at_ms=? WHERE session_id=?", (now, row["session_id"]))
            return SaaSPrincipal(row["user_id"], row["email"], row["plan_code"], row["status"]), row["session_id"]

    def rotate_csrf(self, session_id: str) -> str | None:
        token = secrets.token_urlsafe(24)
        with self._conn() as conn:
            row = conn.execute("SELECT session_id FROM saas_sessions_v35 WHERE session_id=? AND revoked_at_ms IS NULL AND expires_at_ms>?", (session_id, _now_ms())).fetchone()
            if not row:
                return None
            conn.execute("UPDATE saas_sessions_v35 SET csrf_token_hash=? WHERE session_id=?", (_hash_token(token), session_id))
        return token

    def validate_csrf(self, session_id: str, csrf_token: str | None) -> bool:
        if not csrf_token:
            return False
        with self._conn() as conn:
            row = conn.execute("SELECT csrf_token_hash,expires_at_ms,revoked_at_ms FROM saas_sessions_v35 WHERE session_id=?", (session_id,)).fetchone()
            return bool(row and not row["revoked_at_ms"] and row["expires_at_ms"] > _now_ms() and hmac.compare_digest(row["csrf_token_hash"], _hash_token(csrf_token)))

    def logout(self, session_id: str, user_id: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE saas_sessions_v35 SET revoked_at_ms=? WHERE session_id=?", (_now_ms(), session_id))
            self._audit(conn, user_id, "LOGOUT", {})

    def public_user(self, user_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM saas_users_v35 WHERE user_id=?", (user_id,)).fetchone()
            return self._public_user_row(row) if row else None

    @staticmethod
    def _public_user_row(row: sqlite3.Row) -> dict[str, Any]:
        plan = PLANS.get(row["plan_code"], PLANS["RESEARCH"])
        return {"user_id": row["user_id"], "email": row["email"], "plan_code": row["plan_code"], "plan": plan["name"], "status": row["status"], "created_at_ms": row["created_at_ms"], "entitlements": plan}

    def me(self, user_id: str) -> dict[str, Any]:
        user = self.public_user(user_id)
        if not user:
            raise ValueError("user not found")
        return {"user": user, "csrf_required_for_mutations": True}

    def list_watchlists(self, user_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM saas_watchlists_v35 WHERE user_id=? ORDER BY updated_at_ms DESC", (user_id,)).fetchall()
        return [self._watch_row(r) for r in rows]

    @staticmethod
    def _watch_row(row: sqlite3.Row) -> dict[str, Any]:
        return {"watchlist_id": row["watchlist_id"], "name": row["name"], "symbols": json.loads(row["symbols_json"] or "[]"), "created_at_ms": row["created_at_ms"], "updated_at_ms": row["updated_at_ms"]}

    def create_watchlist(self, user_id: str, name: str) -> dict[str, Any]:
        name = " ".join(name.strip().split())
        if not name or len(name) > 48:
            raise ValueError("watchlist name must be 1-48 characters")
        uid = secrets.token_hex(12); now = _now_ms()
        with self._conn() as conn:
            conn.execute("INSERT INTO saas_watchlists_v35(watchlist_id,user_id,name,symbols_json,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?)", (uid, user_id, name, "[]", now, now))
            self._audit(conn, user_id, "WATCHLIST_CREATE", {"watchlist_id": uid, "name": name})
            row = conn.execute("SELECT * FROM saas_watchlists_v35 WHERE watchlist_id=?", (uid,)).fetchone()
        return self._watch_row(row)

    def update_watchlist(self, user_id: str, watchlist_id: str, name: str | None, symbols: list[str] | None, max_symbols: int) -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM saas_watchlists_v35 WHERE watchlist_id=? AND user_id=?", (watchlist_id, user_id)).fetchone()
            if not row:
                raise KeyError("watchlist not found")
            clean_symbols = None
            if symbols is not None:
                clean_symbols = []
                for s in symbols:
                    s = str(s).strip().upper()
                    if not s or len(s) > 30:
                        continue
                    if s not in clean_symbols:
                        clean_symbols.append(s)
                if len(clean_symbols) > max_symbols:
                    raise ValueError(f"symbol limit exceeded ({max_symbols})")
            now = _now_ms()
            new_name = name.strip() if name is not None else row["name"]
            if not new_name or len(new_name) > 48:
                raise ValueError("invalid watchlist name")
            conn.execute(
                "UPDATE saas_watchlists_v35 SET name=?, symbols_json=?, updated_at_ms=? WHERE watchlist_id=? AND user_id=?",
                (new_name, json.dumps(clean_symbols if clean_symbols is not None else json.loads(row["symbols_json"]), separators=(",", ":")), now, watchlist_id, user_id),
            )
            self._audit(conn, user_id, "WATCHLIST_UPDATE", {"watchlist_id": watchlist_id})
            row = conn.execute("SELECT * FROM saas_watchlists_v35 WHERE watchlist_id=?", (watchlist_id,)).fetchone()
        return self._watch_row(row)

    def delete_watchlist(self, user_id: str, watchlist_id: str) -> None:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM saas_watchlists_v35 WHERE watchlist_id=? AND user_id=?", (watchlist_id, user_id))
            if cur.rowcount == 0:
                raise KeyError("watchlist not found")
            self._audit(conn, user_id, "WATCHLIST_DELETE", {"watchlist_id": watchlist_id})

    def preferences(self, user_id: str) -> dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM saas_preferences_v35 WHERE user_id=?", (user_id,)).fetchone()
            if not row:
                conn.execute("INSERT INTO saas_preferences_v35(user_id,updated_at_ms) VALUES(?,?)", (user_id, _now_ms()))
                return self.preferences(user_id)
        return {"alert_min_score": row["alert_min_score"], "preferred_side": row["preferred_side"], "notify_browser": bool(row["notify_browser"]), "notify_telegram": bool(row["notify_telegram"]), "quiet_hours": json.loads(row["quiet_hours_json"])}

    def update_preferences(self, user_id: str, values: dict[str, Any], allow_telegram: bool) -> dict[str, Any]:
        current = self.preferences(user_id)
        score = min(100.0, max(0.0, float(values.get("alert_min_score", current["alert_min_score"]))))
        side = str(values.get("preferred_side", current["preferred_side"])).upper()
        if side not in {"BOTH", "LONG", "SHORT"}:
            raise ValueError("preferred_side must be BOTH, LONG or SHORT")
        browser = bool(values.get("notify_browser", current["notify_browser"]))
        telegram = bool(values.get("notify_telegram", current["notify_telegram"])) and allow_telegram
        quiet = values.get("quiet_hours", current["quiet_hours"])
        if not isinstance(quiet, dict):
            raise ValueError("quiet_hours must be an object")
        with self._conn() as conn:
            conn.execute("INSERT INTO saas_preferences_v35(user_id,alert_min_score,preferred_side,notify_browser,notify_telegram,quiet_hours_json,updated_at_ms) VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET alert_min_score=excluded.alert_min_score,preferred_side=excluded.preferred_side,notify_browser=excluded.notify_browser,notify_telegram=excluded.notify_telegram,quiet_hours_json=excluded.quiet_hours_json,updated_at_ms=excluded.updated_at_ms", (user_id, score, side, int(browser), int(telegram), json.dumps(quiet, separators=(",", ":")), _now_ms()))
            self._audit(conn, user_id, "PREFERENCES_UPDATE", {"telegram": telegram, "side": side, "score": score})
        return self.preferences(user_id)

    def purge_sessions(self) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM saas_sessions_v35 WHERE revoked_at_ms IS NOT NULL OR expires_at_ms<=?", (_now_ms(),))
            return cur.rowcount

    def usage(self, user_id: str) -> dict[str, Any]:
        with self._conn() as conn:
            watch_count = conn.execute("SELECT COUNT(*) FROM saas_watchlists_v35 WHERE user_id=?", (user_id,)).fetchone()[0]
        plan_code = (self.public_user(user_id) or {}).get("plan_code", "RESEARCH")
        ent = PLANS.get(plan_code, PLANS["RESEARCH"])
        return {"watchlists": watch_count, "watchlists_limit": ent["max_watchlists"]}
