from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException


@dataclass
class RuntimeStateV30:
    started_at_ms: int | None = None
    last_heartbeat_ms: int | None = None
    startup_complete: bool = False
    shutdown_requested: bool = False


class RuntimeControllerV30:
    def __init__(self) -> None:
        self.state = RuntimeStateV30()
        self.kill_switch = False
        self.kill_reason = ""

    def start(self) -> None:
        now = int(time.time() * 1000)
        self.state.started_at_ms = now
        self.state.last_heartbeat_ms = now
        self.state.startup_complete = True
        self.state.shutdown_requested = False

    def heartbeat(self) -> None:
        self.state.last_heartbeat_ms = int(time.time() * 1000)

    def set_kill(self, enabled: bool, reason: str = "manual") -> None:
        self.kill_switch = bool(enabled)
        self.kill_reason = reason

    def guard_signal_generation(self) -> None:
        if self.kill_switch:
            raise HTTPException(status_code=503, detail=f"runtime kill switch active: {self.kill_reason}")

    def status(self) -> dict[str, Any]:
        return {
            "startup_complete": self.state.startup_complete,
            "shutdown_requested": self.state.shutdown_requested,
            "started_at_ms": self.state.started_at_ms,
            "last_heartbeat_ms": self.state.last_heartbeat_ms,
            "kill_switch": self.kill_switch,
            "kill_reason": self.kill_reason,
        }

    def readiness(self, *, storage: dict[str, Any], redis: dict[str, Any], require_redis: bool, production: bool, max_heartbeat_age_s: int = 60) -> dict[str, Any]:
        now = int(time.time() * 1000)
        heartbeat_age_s = None if self.state.last_heartbeat_ms is None else max(0.0, (now - self.state.last_heartbeat_ms) / 1000.0)
        checks = {
            "runtime_started": self.state.startup_complete,
            "heartbeat_fresh": heartbeat_age_s is not None and heartbeat_age_s <= max(1, int(max_heartbeat_age_s)),
            "storage": bool(storage.get("ready")),
            "redis": (bool(redis.get("reachable")) if require_redis else True),
            "kill_switch_clear": not self.kill_switch,
        }
        if production and storage.get("backend") != "postgres":
            checks["production_core_storage"] = False
        else:
            checks["production_core_storage"] = True
        ready = all(checks.values())
        reasons: list[str] = []
        if not checks["heartbeat_fresh"]:
            reasons.append("RUNTIME_HEARTBEAT_STALE")
        if not checks["storage"]:
            reasons.extend(storage.get("reasons", []))
        if require_redis and not checks["redis"]:
            reasons.append("REDIS_UNREACHABLE")
        if self.kill_switch:
            reasons.append("KILL_SWITCH_ACTIVE")
        if not checks["production_core_storage"]:
            reasons.append("PRODUCTION_REQUIRES_POSTGRES_CORE_STORAGE")
        return {"ready": ready, "checks": checks, "reasons": sorted(set(reasons)), "heartbeat_age_s": heartbeat_age_s}
