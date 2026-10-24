from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class StorageCheckV30:
    name: str
    configured: bool
    reachable: bool
    latency_ms: float | None
    detail: str
    active: bool = False


class StorageHealthV30:
    def __init__(self, sqlite_path: str, backend: str = "sqlite", postgres_url: str | None = None, require_postgres: bool = False) -> None:
        self.sqlite_path = sqlite_path
        self.backend = str(backend).lower()
        self.postgres_url = postgres_url
        self.require_postgres = bool(require_postgres)

    def sqlite(self) -> StorageCheckV30:
        start = time.perf_counter()
        try:
            conn = sqlite3.connect(self.sqlite_path, timeout=2.0)
            row = conn.execute("PRAGMA quick_check").fetchone()
            conn.close()
            ok = bool(row and str(row[0]).lower() == "ok")
            return StorageCheckV30("sqlite", True, ok, (time.perf_counter() - start) * 1000, "quick_check_ok" if ok else f"quick_check:{row}", active=self.backend == "sqlite")
        except Exception as exc:
            return StorageCheckV30("sqlite", True, False, (time.perf_counter() - start) * 1000, str(exc), active=self.backend == "sqlite")

    def postgres(self) -> StorageCheckV30:
        if not self.postgres_url:
            return StorageCheckV30("postgres", False, False, None, "POSTGRES_URL not configured", active=self.backend == "postgres")
        start = time.perf_counter()
        try:
            import psycopg  # type: ignore
            with psycopg.connect(self.postgres_url, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
            return StorageCheckV30("postgres", True, True, (time.perf_counter() - start) * 1000, "select_1_ok", active=self.backend == "postgres")
        except ImportError:
            return StorageCheckV30("postgres", True, False, (time.perf_counter() - start) * 1000, "psycopg_not_installed", active=self.backend == "postgres")
        except Exception as exc:
            return StorageCheckV30("postgres", True, False, (time.perf_counter() - start) * 1000, str(exc), active=self.backend == "postgres")

    def run(self) -> dict[str, Any]:
        checks = [self.sqlite(), self.postgres()]
        active = next((c for c in checks if c.active), checks[0])
        reasons: list[str] = []
        ready = active.reachable
        if self.require_postgres:
            if self.backend != "postgres":
                ready = False
                reasons.append("CORE_STORAGE_BACKEND_NOT_POSTGRES")
            elif not checks[1].reachable:
                ready = False
                reasons.append("POSTGRES_UNREACHABLE")
        elif self.backend == "sqlite" and not checks[0].reachable:
            ready = False
            reasons.append("SQLITE_UNREACHABLE")
        return {
            "ready": ready,
            "backend": self.backend,
            "active": {"name": active.name, "reachable": active.reachable},
            "checks": [c.__dict__ for c in checks],
            "reasons": reasons,
        }


class RedisHealthV30:
    def __init__(self, url: str | None = None, required: bool = False) -> None:
        self.url = url
        self.required = bool(required)

    async def check(self) -> StorageCheckV30:
        if not self.url:
            return StorageCheckV30("redis", False, False, None, "REDIS_URL not configured")
        start = time.perf_counter()
        try:
            import redis.asyncio as redis  # type: ignore
            client = redis.from_url(self.url, socket_connect_timeout=2, socket_timeout=2)
            try:
                await client.ping()
            finally:
                await client.aclose()
            return StorageCheckV30("redis", True, True, (time.perf_counter() - start) * 1000, "ping_ok")
        except ImportError:
            return StorageCheckV30("redis", True, False, (time.perf_counter() - start) * 1000, "redis_package_not_installed")
        except Exception as exc:
            return StorageCheckV30("redis", True, False, (time.perf_counter() - start) * 1000, str(exc))


class RedisLeaseV30:
    def __init__(self, url: str | None, key: str, ttl_s: int = 30) -> None:
        self.url = url
        self.key = key
        self.ttl_s = max(5, int(ttl_s))
        self.token = os.urandom(16).hex()
        self.client = None
        self.acquired = False

    async def acquire(self) -> bool:
        if not self.url:
            return False
        try:
            import redis.asyncio as redis  # type: ignore
            self.client = redis.from_url(self.url, socket_connect_timeout=2, socket_timeout=2)
            self.acquired = bool(await self.client.set(self.key, self.token, ex=self.ttl_s, nx=True))
            if not self.acquired:
                await self.client.aclose()
                self.client = None
            return self.acquired
        except Exception:
            if self.client is not None:
                try:
                    await self.client.aclose()
                except Exception:
                    pass
                self.client = None
            return False

    async def release(self) -> None:
        if self.client is None or not self.acquired:
            return
        try:
            await self.client.eval("if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end", 1, self.key, self.token)
        finally:
            await self.client.aclose()
            self.client = None
            self.acquired = False
