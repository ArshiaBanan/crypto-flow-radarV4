from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, asdict
from typing import Any

SCHEMA_VERSION = "production_integration_v33"


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def config_fingerprint(config: dict[str, Any]) -> str:
    return hashlib.sha256(_canon(config).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ComponentStatusV33:
    name: str
    enabled: bool
    ready: bool
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RuntimeManifestV33:
    runtime_id: str
    started_at_ms: int
    app_version: str
    config_hash: str
    components: tuple[ComponentStatusV33, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_component_matrix(*, settings: Any, storage: dict[str, Any], redis: dict[str, Any], dualwrite: dict[str, Any], monitoring_quarantine: bool) -> tuple[ComponentStatusV33, ...]:
    return (
        ComponentStatusV33("market_data", bool(settings.binance_enabled and settings.data_engine_enabled), bool(settings.binance_enabled and settings.data_engine_enabled), {"provider": "binance"}),
        ComponentStatusV33("external_enrichment", bool(settings.v23_enrichment_enabled), True, {"mode": "context_only"}),
        ComponentStatusV33("external_intelligence", bool(settings.v24_intelligence_enabled), True, {"mode": "consensus_context"}),
        ComponentStatusV33("fusion_risk", bool(settings.v20_enabled), not monitoring_quarantine, {"blocked_by_quarantine": monitoring_quarantine}),
        ComponentStatusV33("paper_trading", True, not monitoring_quarantine, {"quarantine": monitoring_quarantine}),
        ComponentStatusV33("monitoring", True, True, {"quarantine": monitoring_quarantine}),
        ComponentStatusV33("postgres", bool(settings.postgres_url), bool(storage.get("postgres_reachable", False)), storage),
        ComponentStatusV33("redis", bool(settings.redis_url), bool(redis.get("reachable", False)), redis),
        ComponentStatusV33("dualwrite", bool(settings.v32_enabled), bool(dualwrite.get("ready", False)) if settings.v32_enabled else True, dualwrite),
    )


def integration_readiness(components: tuple[ComponentStatusV33, ...], *, production: bool) -> dict[str, Any]:
    critical = {"market_data", "fusion_risk", "paper_trading", "monitoring"}
    failures = [c.name for c in components if c.name in critical and c.enabled and not c.ready]
    if production:
        for c in components:
            if c.name in {"postgres", "redis", "dualwrite"} and c.enabled and not c.ready:
                failures.append(c.name)
    return {"ready": not failures, "failures": failures, "critical_components": sorted(critical)}
