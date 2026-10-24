from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Header, Request, Response, Cookie, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
import asyncio
import logging
import time
import uuid
import json
import sqlite3
from typing import Any

from app.collectors.binance_futures import BinanceFuturesClient
from app.data_engine import BinanceDataEngine
from app import detector_v40
from app.storage.factory import build_durable_store

from app.settings import settings
from app.collectors.livecoinwatch import LiveCoinWatchClient
from app.collectors.cryptometer import CryptoMeterClient
from app.storage.in_memory import InMemoryStore
from app.paper_v18 import LivePaperEngine, PaperTradeStore, build_paper_signal
from app.monitoring_v19 import MonitoringEngine, MonitoringStore, DriftPolicy, LiveMonitoringRuntime
from app.production_v20 import AlertLedger, TelegramNotifier, ProductionRuntimeV20, CorrelationExposure
from app.api_v20 import require_admin_key
from app.validation_v21 import ValidationPolicy, ValidationStore, validate_paper_rows, load_paper_rows
from app.observation_v22 import ObservationLedgerV22, public_config_hash
from app.enrichment_v23 import EnrichmentStoreV23, MultiSourceRuntimeV23
from app.intelligence_v24 import (
    ExternalIntelligenceStoreV24, ExternalIntelligenceRuntimeV24, ConsensusEngineV24,
    fetch_token_security
)
from app.collectors.coinglass_v24 import CoinGlassClientV24
from app.collectors.geckoterminal_v24 import GeckoTerminalClientV24
from app.collectors.goplus_v24 import GoPlusClientV24
from app.resilience_v25 import GLOBAL_PROVIDER_HEALTH_V25, ResilienceConfigV25
from app.observability_v30 import METRICS_V30, configure_logging_v30
from app.storage_v30 import StorageHealthV30, RedisHealthV30, RedisLeaseV30
from app.ops_v30 import RuntimeControllerV30
from app.runtime_guard_v30 import production_gate_v30
from app.storage_v32 import SQLiteOutboxV32, PostgreSQLLedgerV32, DualWriteControllerV32, DEFAULT_TABLES
from app.live_validation_v33 import LivePaperValidationStoreV33, LivePaperValidationHarnessV33, CohortSpecV33
from app.production_integration_v33 import build_component_matrix, integration_readiness, config_fingerprint
from app.saas_v35 import SaaSStoreV35, SaaSPrincipal, PLANS
from app.saas_v36 import SaaSStoreV36, SaaSPrincipalV36, PLANS as PLANS_V36, GmailEmailSenderV36, SaaSNotificationRouterV36, CooldownError, EmailVerificationRequired, EntitlementError
from app.market_data_v37 import MarketDataFederationV37, ASSET_INDEX
from app.market_data_v38 import MarketDataFederationV38, ASSET_INDEX_V38

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
configure_logging_v30(settings.log_level)
LOGGER = logging.getLogger("crypto_flow_radar.v30")
GLOBAL_PROVIDER_HEALTH_V25.config = ResilienceConfigV25(
    timeout_s=settings.v25_http_timeout_seconds,
    max_attempts=settings.v25_http_max_attempts,
    base_backoff_s=settings.v25_backoff_base_seconds,
    max_backoff_s=settings.v25_backoff_max_seconds,
    circuit_failure_threshold=settings.v25_circuit_failure_threshold,
    circuit_open_seconds=settings.v25_circuit_open_seconds,
    max_response_bytes=settings.v25_max_response_bytes,
    max_concurrency=settings.v25_max_concurrency,
    min_request_interval_ms=settings.v25_min_request_interval_ms,
).normalized()
store = InMemoryStore()
durable_store = build_durable_store(settings)
paper_store = PaperTradeStore(settings.sqlite_path)
paper_engine = LivePaperEngine(paper_store)
monitoring_store = MonitoringStore(settings.sqlite_path)
monitoring_engine = MonitoringEngine(DriftPolicy())
monitoring_runtime = LiveMonitoringRuntime(monitoring_engine, monitoring_store)
alert_ledger = AlertLedger(settings.sqlite_path)
validation_store = ValidationStore(settings.sqlite_path)
observation_ledger = ObservationLedgerV22(settings.sqlite_path)
enrichment_store = EnrichmentStoreV23(settings.sqlite_path)
telegram = TelegramNotifier(settings.v20_telegram_bot_token, settings.v20_telegram_chat_id, settings.v20_telegram_enabled)
production_runtime: ProductionRuntimeV20 | None = None
observation_session_id: str | None = None
enrichment_runtime: MultiSourceRuntimeV23 | None = None
intelligence_store_v24 = ExternalIntelligenceStoreV24(settings.sqlite_path)
intelligence_runtime_v24: ExternalIntelligenceRuntimeV24 | None = None
ops_runtime_v30 = RuntimeControllerV30()
storage_health_v30 = StorageHealthV30(settings.sqlite_path, settings.storage_backend, settings.postgres_url, settings.v30_require_postgres)
redis_health_v30 = RedisHealthV30(settings.redis_url, settings.v30_require_redis)
redis_lease_v30 = RedisLeaseV30(settings.redis_url, settings.v30_redis_lease_key, settings.v30_redis_lease_ttl_seconds)

_v32_table_names = tuple(x.strip() for x in settings.v32_tables.split(",") if x.strip()) or DEFAULT_TABLES
v32_outbox: SQLiteOutboxV32 | None = SQLiteOutboxV32(settings.sqlite_path, _v32_table_names) if settings.v32_enabled else None
v32_postgres: PostgreSQLLedgerV32 | None = None
v33_validation_store = LivePaperValidationStoreV33(settings.sqlite_path) if settings.v33_enabled else None
v33_validation_harness = LivePaperValidationHarnessV33(settings.sqlite_path, v33_validation_store) if settings.v33_enabled and v33_validation_store is not None else None
saas_v35 = SaaSStoreV35(settings.sqlite_path, settings.v35_session_ttl_seconds) if settings.v35_enabled else None
email_sender_v36 = GmailEmailSenderV36(settings.v36_smtp_host, settings.v36_smtp_port, settings.v36_smtp_username, settings.v36_smtp_password, settings.v36_smtp_from_email, settings.v36_email_mode, settings.v36_email_timeout_seconds)
saas_v36 = SaaSStoreV36(settings.sqlite_path, settings.v36_session_ttl_seconds, email_sender_v36, settings.v36_billing_webhook_secret) if settings.v36_enabled else None
premium_alert_router_v36 = SaaSNotificationRouterV36(saas_v36, email_sender_v36, settings.v36_telegram_bot_token or settings.v20_telegram_bot_token, settings.v36_twilio_account_sid, settings.v36_twilio_auth_token, settings.v36_twilio_from_number) if saas_v36 is not None else None
market_v37 = MarketDataFederationV37(settings.sqlite_path, settings)
market_v38 = MarketDataFederationV38(settings.sqlite_path, settings)
v32_controller: DualWriteControllerV32 | None = None
if settings.v32_enabled and settings.postgres_url and settings.v32_dualwrite_mode != "off":
    try:
        v32_postgres = PostgreSQLLedgerV32(settings.postgres_url)
    except Exception as exc:
        LOGGER.warning("v3.2 PostgreSQL ledger unavailable: %s", exc)
        v32_postgres = None
if v32_outbox is not None:
    v32_controller = DualWriteControllerV32(v32_outbox, v32_postgres, settings.v32_dualwrite_mode)

def _paper_submit(payload: dict):
    signal = build_paper_signal(payload)
    action = paper_engine.submit(signal)
    return signal.signal_id

production_runtime = ProductionRuntimeV20(
    alert_ledger,
    cooldown_ms=settings.v20_alert_cooldown_seconds * 1000,
    stop_pct=settings.v20_stop_pct,
    target_pct=settings.v20_target_pct,
    horizon_min=settings.v20_horizon_min,
    per_alert_risk_pct=settings.v20_per_alert_risk_pct,
    max_correlated_exposure_pct=settings.v20_max_correlated_exposure_pct,
    telegram=telegram,
    auto_paper_submit=settings.v20_auto_paper_submit,
    quarantine_provider=monitoring_store.is_quarantined,
    paper_submitter=_paper_submit,
    premium_alert_router=premium_alert_router_v36,
)
correlation_exposure = CorrelationExposure(durable_store.recent_bars, settings.v20_per_alert_risk_pct, settings.v20_correlation_threshold)
class _ObservationRuntimeV22:
    def __init__(self, ledger, config_hash=""):
        self.ledger = ledger
        self.session_id = None
        self.config_hash = config_hash
    def start(self, session_id: str):
        self.session_id = session_id
    def model_context(self):
        models = alert_ledger.models()
        return {
            "models": [{"name": x.get("name"), "version": x.get("version"), "status": x.get("status")} for x in models],
            "enrichment_schema": "enrichment_v23",
        }
    def record_snapshot(self, **kwargs):
        if not self.session_id:
            return None
        return self.ledger.record_snapshot(session_id=self.session_id, feature_schema_version="observation_v22", **kwargs)
    def record_alert_created(self, observation_id, alert_id, payload, ts_ms):
        return self.ledger.append_event(observation_id, "ALERT_CREATED", reference_id=alert_id, payload=payload, ts_ms=ts_ms)
    def record_paper_resolved(self, observation_id, signal_id, payload, ts_ms):
        return self.ledger.append_event(observation_id, "PAPER_RESOLVED", reference_id=signal_id, payload=payload, ts_ms=ts_ms)

observation_runtime = _ObservationRuntimeV22(observation_ledger, public_config_hash(settings.model_dump()))
engine_task: asyncio.Task | None = None
ops_heartbeat_task: asyncio.Task | None = None
v32_pump_task: asyncio.Task | None = None
v40_detector_task: asyncio.Task | None = None


async def _v32_dualwrite_loop() -> None:
    while True:
        await asyncio.sleep(max(0.25, float(settings.v32_poll_seconds)))
        if v32_controller is None:
            continue
        try:
            applied = v32_controller.pump_once(settings.v32_batch_size)
            if applied:
                METRICS_V30.inc("cfr_v32_events_applied_total", value=applied)
            st = v32_controller.status()
            METRICS_V30.set_gauge("cfr_v32_pending_events", st.pending_events)
        except Exception as exc:
            LOGGER.error("v3.2 dual-write pump failed: %s", exc)
            METRICS_V30.inc("cfr_v32_pump_errors_total")


async def _v30_heartbeat_loop() -> None:
    while True:
        await asyncio.sleep(5)
        ops_runtime_v30.heartbeat()
        METRICS_V30.set_gauge("cfr_runtime_heartbeat_age_s", 0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    global engine_task, ops_heartbeat_task, v32_pump_task, observation_session_id, enrichment_runtime, intelligence_runtime_v24, v40_detector_task
    ops_runtime_v30.start()
    if v32_controller is not None:
        v32_pump_task = asyncio.create_task(_v32_dualwrite_loop())
    ops_heartbeat_task = asyncio.create_task(_v30_heartbeat_loop())
    if detector_v40.status().get("enabled"):
        v40_detector_task = asyncio.create_task(detector_v40.background_loop())
    METRICS_V30.inc("cfr_runtime_starts_total")
    lease_acquired = False
    if settings.v30_enabled and settings.v30_require_redis:
        lease_acquired = await redis_lease_v30.acquire()
        METRICS_V30.set_gauge("cfr_runtime_lease_acquired", 1 if lease_acquired else 0)
        if not lease_acquired:
            LOGGER.error("runtime lease unavailable; data engine will not start")
    if settings.v23_enrichment_enabled:
        lcw = None
        cm = None
        if settings.lcw_enabled and settings.lcw_api_key:
            lcw = LiveCoinWatchClient(settings.lcw_api_key)
        if settings.cryptometer_enabled and settings.cryptometer_api_key:
            cm = CryptoMeterClient(settings.cryptometer_api_key, settings.cryptometer_base_url)
        enrichment_runtime = MultiSourceRuntimeV23(
            enrichment_store, lcw=lcw, cryptometer=cm,
            lcw_interval_s=settings.v23_lcw_interval_seconds,
            cm_interval_s=settings.v23_cryptometer_interval_seconds,
        )
        if lcw or cm:
            await enrichment_runtime.start()

    if settings.v24_intelligence_enabled:
        cg = CoinGlassClientV24(settings.v24_coinglass_api_key, settings.v24_coinglass_base_url) if settings.v24_coinglass_api_key else None
        gt = GeckoTerminalClientV24(settings.v24_gecko_base_url) if settings.v24_gecko_enabled else None
        networks = tuple(x.strip().lower() for x in settings.v24_gecko_networks.split(",") if x.strip())
        intelligence_runtime_v24 = ExternalIntelligenceRuntimeV24(
            intelligence_store_v24, coinglass=cg, gecko=gt,
            poll_seconds=settings.v24_poll_seconds, gecko_networks=networks or ("solana",),
            consensus=ConsensusEngineV24(
                min_families=settings.v24_min_consensus_families,
                min_agreement=settings.v24_min_consensus_agreement,
                max_age_ms=settings.v24_consensus_max_age_ms,
            ),
        )
        if cg or gt:
            await intelligence_runtime_v24.start()

    can_start_engine = (not settings.v30_require_redis) or lease_acquired
    prod_gate = production_gate_v30(app_env=settings.app_env, admin_api_key=settings.admin_api_key, require_admin_key=settings.v30_require_admin_key_in_prod)
    if settings.data_engine_enabled and settings.binance_enabled and can_start_engine:
        cfg = settings.model_dump()
        observation_session_id = observation_ledger.start_session(
            environment=settings.app_env, code_version="3.3.0",
            config_hash=public_config_hash(cfg),
            metadata={"data_engine_enabled": settings.data_engine_enabled, "binance_enabled": settings.binance_enabled, "v20_enabled": settings.v20_enabled},
        )
        observation_runtime.start(observation_session_id)
        client = BinanceFuturesClient(settings.binance_futures_ws_url, settings.binance_futures_rest_url, settings.binance_ws_max_symbols, settings.binance_depth_levels)
        engine = BinanceDataEngine(client, store, durable_store, settings.binance_ws_max_symbols, paper_engine=paper_engine, monitoring_runtime=monitoring_runtime, production_runtime=production_runtime, correlation_exposure=correlation_exposure, observation_runtime=observation_runtime, enrichment_runtime=enrichment_runtime)
        engine_task = asyncio.create_task(engine.run())
    try:
        yield
    finally:
        if v32_pump_task:
            v32_pump_task.cancel()
            try:
                await v32_pump_task
            except asyncio.CancelledError:
                pass
            v32_pump_task = None
        if v40_detector_task:
            v40_detector_task.cancel()
            try:
                await v40_detector_task
            except asyncio.CancelledError:
                pass
            v40_detector_task = None
        if ops_heartbeat_task:
            ops_heartbeat_task.cancel()
            try:
                await ops_heartbeat_task
            except asyncio.CancelledError:
                pass
            ops_heartbeat_task = None
        if engine_task:
            engine_task.cancel()
            try:
                await engine_task
            except asyncio.CancelledError:
                pass
        if observation_session_id:
            observation_ledger.stop_session(observation_session_id)
            observation_runtime.session_id = None
        if enrichment_runtime is not None:
            await enrichment_runtime.stop()
            enrichment_runtime = None
        if intelligence_runtime_v24 is not None:
            await intelligence_runtime_v24.stop()
            intelligence_runtime_v24 = None
        if lease_acquired:
            await redis_lease_v30.release()
        ops_runtime_v30.state.shutdown_requested = True
        METRICS_V30.inc("cfr_runtime_stops_total")
        # Global stores are process-scoped singletons; do not close them at each TestClient lifespan.
        # The OS/process lifecycle owns final descriptor cleanup in deployment.


app = FastAPI(title="Crypto Flow Radar", version="3.9.0", lifespan=lifespan)
app.mount("/frontend", StaticFiles(directory=FRONTEND), name="frontend")


@app.middleware("http")
async def v30_observability_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:24]
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    except Exception:
        METRICS_V30.inc("cfr_http_exceptions_total", labels={"path": request.url.path})
        raise
    finally:
        duration_ms = (time.perf_counter() - start) * 1000.0
        METRICS_V30.inc("cfr_http_requests_total", labels={"method": request.method, "path": request.url.path, "status": status})
        METRICS_V30.observe("cfr_http_duration", duration_ms, labels={"method": request.method, "path": request.url.path})
        METRICS_V30.set_gauge("cfr_http_last_request_ts_ms", int(time.time() * 1000))
        try:
            response.headers["X-Request-ID"] = request_id
        except Exception:
            pass
        LOGGER.info("http_request", extra={"request_id": request_id, "path": request.url.path, "method": request.method, "status_code": status, "duration_ms": round(duration_ms, 2)})




class SaaSRegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=10, max_length=128)


class SaaSLoginRequest(BaseModel):
    email: str
    password: str = Field(min_length=1, max_length=128)


class WatchlistCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=48)


class WatchlistUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=48)
    symbols: list[str] | None = None


class SaaSPreferencesRequest(BaseModel):
    alert_min_score: float = Field(default=70, ge=0, le=100)
    preferred_side: str = "BOTH"
    notify_browser: bool = True
    notify_telegram: bool = False
    quiet_hours: dict[str, Any] = Field(default_factory=lambda: {"enabled": False, "start": "22:00", "end": "07:00"})


def _saas_required() -> SaaSStoreV35:
    if saas_v35 is None:
        raise HTTPException(503, "SaaS layer is disabled")
    return saas_v35


def _principal(session_token: str | None) -> tuple[SaaSPrincipal, str]:
    store = _saas_required()
    found = store.principal_from_token(session_token)
    if not found:
        raise HTTPException(401, "authentication required")
    return found


def _mutation_principal(session_token: str | None, csrf_token: str | None) -> SaaSPrincipal:
    principal, session_id = _principal(session_token)
    if not _saas_required().validate_csrf(session_id, csrf_token):
        raise HTTPException(403, "invalid or missing CSRF token")
    return principal


def _set_session(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.v35_cookie_name,
        value=token,
        httponly=True,
        secure=settings.v35_cookie_secure,
        samesite="lax",
        max_age=settings.v35_session_ttl_seconds,
        path="/",
    )


def _clear_session(response: Response) -> None:
    response.delete_cookie(key=settings.v35_cookie_name, path="/")



class V36RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=24)
    email: str
    password: str = Field(min_length=10, max_length=128)
    display_name: str = Field(default="", max_length=80)


class V36LoginRequest(BaseModel):
    identifier: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class V36CodeRequest(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)


class V36ResendRequest(BaseModel):
    email: str


class V36ForgotPasswordRequest(BaseModel):
    email: str


class V36ResetRequest(BaseModel):
    email: str
    code: str = Field(min_length=6, max_length=6)
    new_password: str = Field(min_length=10, max_length=128)


class V36ProfileRequest(BaseModel):
    username: str | None = Field(default=None, min_length=3, max_length=24)
    display_name: str | None = Field(default=None, max_length=80)
    timezone: str | None = Field(default=None, max_length=64)
    phone_e164: str | None = Field(default=None, max_length=24)
    telegram_chat_id: str | None = Field(default=None, max_length=80)


class V36WatchlistCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=48)


class V36WatchlistUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=48)
    symbols: list[str] | None = None


class V36PreferencesRequest(BaseModel):
    alert_min_score: float = Field(default=70, ge=0, le=100)
    preferred_side: str = "BOTH"
    notify_browser: bool = True
    notify_email: bool = True
    notify_telegram: bool = False
    notify_sms: bool = False


class V36ReserveSignalRequest(BaseModel):
    signal_id: str = Field(min_length=1, max_length=128)
    signal_kind: str = "STANDARD"
    channel: str = "IN_APP"


class V36BillingEvent(BaseModel):
    type: str
    data: dict[str, Any] = Field(default_factory=dict)


def _saas36_required() -> SaaSStoreV36:
    if saas_v36 is None:
        raise HTTPException(503, "SaaS v3.6 layer is disabled")
    return saas_v36


def _principal36(session_token: str | None) -> tuple[SaaSPrincipalV36, str]:
    found = _saas36_required().principal_from_token(session_token)
    if not found:
        raise HTTPException(401, "authentication required")
    return found


def _mutation_principal36(session_token: str | None, csrf_token: str | None) -> SaaSPrincipalV36:
    principal, session_id = _principal36(session_token)
    if not _saas36_required().validate_csrf(session_id, csrf_token):
        raise HTTPException(403, "invalid or missing CSRF token")
    return principal


def _set_session36(response: Response, token: str) -> None:
    response.set_cookie(key=settings.v36_cookie_name, value=token, httponly=True, secure=settings.v36_cookie_secure, samesite="lax", max_age=settings.v36_session_ttl_seconds, path="/")


def _clear_session36(response: Response) -> None:
    response.delete_cookie(key=settings.v36_cookie_name, path="/")


@app.get("/v36/plans")
async def v36_plans() -> dict[str, Any]:
    return {"version": "3.6.0", "plans": PLANS_V36}


@app.get("/v36/status")
async def v36_status(session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name)) -> dict[str, Any]:
    store = _saas36_required()
    if not session_token:
        return {"version": "3.6.0", "authenticated": False, "plans": PLANS_V36}
    found = store.principal_from_token(session_token)
    if not found:
        return {"version": "3.6.0", "authenticated": False, "plans": PLANS_V36}
    principal, _ = found
    return {"version": "3.6.0", "authenticated": True, "user": store.public_user(principal.user_id), "subscription": store.subscription(principal.user_id), "plans": PLANS_V36}


@app.post("/v36/auth/register")
async def v36_register(req: V36RegisterRequest) -> dict[str, Any]:
    store = _saas36_required()
    try:
        user, code, delivery = store.create_user(req.username, req.email, req.password, req.display_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    response: dict[str, Any] = {
        "version": "3.6.0",
        "user": user,
        "verification_required": True,
        "email_delivery": delivery,
        "resend_available_in_seconds": 60,
    }
    if settings.v36_email_mode == "console" or (settings.app_env != "production" and settings.v36_email_mode == "dev"):
        response["development_verification_code"] = code
    return response


@app.post("/v36/auth/verify-email")
async def v36_verify_email(req: V36CodeRequest, response: Response) -> dict[str, Any]:
    try:
        user = _saas36_required().verify_email(req.email, req.code)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    auth = _saas36_required().authenticate(req.email, "") if False else None
    return {"version": "3.6.0", "verified": True, "user": user, "next": "login"}


@app.post("/v36/auth/resend-verification")
async def v36_resend_verification(req: V36ResendRequest) -> dict[str, Any]:
    try:
        user, code, delivery = _saas36_required().resend_verification(req.email)
    except CooldownError as exc:
        raise HTTPException(429, {"code": "RESEND_COOLDOWN", "remaining_seconds": exc.remaining_seconds}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    out = {"version": "3.6.0", "verification_required": True, "email_delivery": delivery, "resend_available_in_seconds": 60, "user": user}
    if settings.v36_email_mode in {"console", "dev"} and settings.app_env != "production":
        out["development_verification_code"] = code
    return out


@app.post("/v36/auth/login")
async def v36_login(req: V36LoginRequest, response: Response) -> dict[str, Any]:
    try:
        auth = _saas36_required().authenticate(req.identifier, req.password)
    except EmailVerificationRequired as exc:
        raise HTTPException(403, {"code": "EMAIL_VERIFICATION_REQUIRED", "email": exc.email, "resend_available_in_seconds": exc.remaining_seconds}) from exc
    if not auth:
        raise HTTPException(401, "invalid username/email or password")
    token, csrf, user = auth
    _set_session36(response, token)
    return {"version": "3.6.0", "user": user, "csrf_token": csrf}


@app.post("/v36/auth/forgot-password")
async def v36_forgot_password(req: V36ForgotPasswordRequest) -> dict[str, Any]:
    email = req.email
    try:
        user, _, delivery = _saas36_required().forgot_password(email)
    except CooldownError as exc:
        raise HTTPException(429, {"code": "RESEND_COOLDOWN", "remaining_seconds": exc.remaining_seconds}) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"version": "3.6.0", "accepted": True, "email_delivery": delivery, "resend_available_in_seconds": 60}


@app.post("/v36/auth/reset-password")
async def v36_reset_password(req: V36ResetRequest) -> dict[str, Any]:
    try:
        user = _saas36_required().reset_password(req.email, req.code, req.new_password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"version": "3.6.0", "reset": True, "user": user}


@app.post("/v36/auth/logout")
async def v36_logout(response: Response, session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name), x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> dict[str, Any]:
    principal, session_id = _principal36(session_token)
    if not _saas36_required().validate_csrf(session_id, x_csrf_token):
        raise HTTPException(403, "invalid or missing CSRF token")
    _saas36_required().logout(session_id, principal.user_id)
    _clear_session36(response)
    return {"version": "3.6.0", "logged_out": True}


@app.get("/v36/auth/me")
async def v36_me(session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name)) -> dict[str, Any]:
    principal, session_id = _principal36(session_token)
    store = _saas36_required()
    csrf = store.rotate_csrf(session_id)
    return {"version": "3.6.0", "user": store.public_user(principal.user_id), "csrf_token": csrf, "subscription": store.subscription(principal.user_id)}


@app.get("/v36/profile")
async def v36_profile(session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name)) -> dict[str, Any]:
    principal, _ = _principal36(session_token)
    return {"version": "3.6.0", "profile": _saas36_required().public_user(principal.user_id)}


@app.put("/v36/profile")
async def v36_profile_update(req: V36ProfileRequest, session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name), x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> dict[str, Any]:
    principal = _mutation_principal36(session_token, x_csrf_token)
    try:
        user = _saas36_required().update_profile(principal.user_id, username=req.username, display_name=req.display_name, timezone=req.timezone, phone_e164=req.phone_e164, telegram_chat_id=req.telegram_chat_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"version": "3.6.0", "profile": user}


@app.get("/v36/subscription")
async def v36_subscription(session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name)) -> dict[str, Any]:
    principal, _ = _principal36(session_token)
    return {"version": "3.6.0", "subscription": _saas36_required().subscription(principal.user_id)}


@app.get("/v36/watchlists")
async def v36_watchlists(session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name)) -> dict[str, Any]:
    principal, _ = _principal36(session_token)
    rows = _saas36_required().list_watchlists(principal.user_id)
    plan = PLANS_V36[principal.plan_code]
    return {"version": "3.6.0", "rows": rows, "usage": {"watchlists": len(rows), "tracking_used": sum(len(x["symbols"]) for x in rows), "tracking_limit": plan["tracking_slots"]}}


@app.post("/v36/watchlists")
async def v36_watchlist_create(req: V36WatchlistCreateRequest, session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name), x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> dict[str, Any]:
    principal = _mutation_principal36(session_token, x_csrf_token)
    try: row = _saas36_required().create_watchlist(principal.user_id, req.name)
    except EntitlementError as exc: raise HTTPException(403, {"code": exc.reason, "detail": exc.detail}) from exc
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc
    return {"version": "3.6.0", "watchlist": row}


@app.patch("/v36/watchlists/{watchlist_id}")
async def v36_watchlist_update(watchlist_id: str, req: V36WatchlistUpdateRequest, session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name), x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> dict[str, Any]:
    principal = _mutation_principal36(session_token, x_csrf_token)
    try: row = _saas36_required().update_watchlist(principal.user_id, watchlist_id, req.name, req.symbols)
    except EntitlementError as exc: raise HTTPException(403, {"code": exc.reason, "detail": exc.detail}) from exc
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc
    return {"version": "3.6.0", "watchlist": row}


@app.delete("/v36/watchlists/{watchlist_id}")
async def v36_watchlist_delete(watchlist_id: str, session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name), x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> dict[str, Any]:
    principal = _mutation_principal36(session_token, x_csrf_token)
    _saas36_required().delete_watchlist(principal.user_id, watchlist_id)
    return {"version": "3.6.0", "deleted": True}


@app.get("/v36/preferences")
async def v36_preferences(session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name)) -> dict[str, Any]:
    principal, _ = _principal36(session_token)
    return {"version": "3.6.0", "preferences": _saas36_required().preferences(principal.user_id)}


@app.put("/v36/preferences")
async def v36_preferences_update(req: V36PreferencesRequest, session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name), x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> dict[str, Any]:
    principal = _mutation_principal36(session_token, x_csrf_token)
    try: prefs = _saas36_required().update_preferences(principal.user_id, req.model_dump())
    except ValueError as exc: raise HTTPException(400, str(exc)) from exc
    return {"version": "3.6.0", "preferences": prefs}


@app.get("/v36/usage")
async def v36_usage(session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name)) -> dict[str, Any]:
    principal, _ = _principal36(session_token)
    return {"version": "3.6.0", "usage": _saas36_required().signal_usage(principal.user_id)}


@app.post("/v36/signal/reserve")
async def v36_reserve_signal(req: V36ReserveSignalRequest, session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name), x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> dict[str, Any]:
    principal = _mutation_principal36(session_token, x_csrf_token)
    try:
        usage = _saas36_required().reserve_signal(principal.user_id, req.signal_id, req.signal_kind, req.channel)
    except EntitlementError as exc:
        raise HTTPException(403, {"code": exc.reason, "detail": exc.detail}) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"version": "3.6.0", "reserved": True, "usage": usage}


@app.get("/v36/my-alerts")
async def v36_my_alerts(limit: int = 50, session_token: str | None = Cookie(default=None, alias=settings.v36_cookie_name)) -> dict[str, Any]:
    principal, _ = _principal36(session_token)
    return {"version": "3.6.0", "rows": _saas36_required().user_alerts(principal.user_id, limit)}


@app.post("/v36/billing/webhook")
async def v36_billing_webhook(request: Request, x_v36_signature: str | None = Header(default=None, alias="X-V36-Signature")) -> dict[str, Any]:
    raw = await request.body()
    store = _saas36_required()
    if not store.billing_webhook_valid(raw, x_v36_signature):
        raise HTTPException(401, "invalid billing webhook signature")
    try:
        event = json.loads(raw.decode("utf-8"))
        result = store.apply_billing_event(event)
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"version": "3.6.0", "applied": True, "user": result}


@app.get("/v35/status")
async def v35_status(session_token: str | None = Cookie(default=None, alias=settings.v35_cookie_name)) -> dict:
    store = _saas_required()
    if not session_token:
        return {"version": "3.5.0", "authenticated": False, "plans": PLANS}
    found = store.principal_from_token(session_token)
    if not found:
        return {"version": "3.5.0", "authenticated": False, "plans": PLANS}
    principal, _ = found
    return {"version": "3.5.0", "authenticated": True, "user": store.public_user(principal.user_id), "usage": store.usage(principal.user_id), "plans": PLANS}


@app.post("/v35/auth/register")
async def v35_register(req: SaaSRegisterRequest, response: Response) -> dict:
    store = _saas_required()
    try:
        user = store.create_user(req.email, req.password, settings.v35_default_plan)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    auth = store.authenticate(req.email, req.password)
    if not auth:
        raise HTTPException(500, "registration completed but session creation failed")
    token, csrf, _ = auth
    _set_session(response, token)
    return {"version": "3.5.0", "user": user, "csrf_token": csrf}


@app.post("/v35/auth/login")
async def v35_login(req: SaaSLoginRequest, response: Response) -> dict:
    store = _saas_required()
    auth = store.authenticate(req.email, req.password)
    if not auth:
        raise HTTPException(401, "invalid email or password")
    token, csrf, user = auth
    _set_session(response, token)
    return {"version": "3.5.0", "user": user, "csrf_token": csrf}


@app.post("/v35/auth/logout")
async def v35_logout(response: Response, session_token: str | None = Cookie(default=None, alias=settings.v35_cookie_name), x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> dict:
    principal, session_id = _principal(session_token)
    if not _saas_required().validate_csrf(session_id, x_csrf_token):
        raise HTTPException(403, "invalid or missing CSRF token")
    _saas_required().logout(session_id, principal.user_id)
    _clear_session(response)
    return {"version": "3.5.0", "logged_out": True}


@app.get("/v35/auth/me")
async def v35_me(session_token: str | None = Cookie(default=None, alias=settings.v35_cookie_name)) -> dict:
    principal, session_id = _principal(session_token)
    store = _saas_required()
    csrf = store.rotate_csrf(session_id)
    if not csrf:
        raise HTTPException(401, "session expired")
    return {"version": "3.5.0", **store.me(principal.user_id), "usage": store.usage(principal.user_id), "csrf_token": csrf}


@app.get("/v35/watchlists")
async def v35_watchlists(session_token: str | None = Cookie(default=None, alias=settings.v35_cookie_name)) -> dict:
    principal, _ = _principal(session_token)
    store = _saas_required()
    return {"version": "3.5.0", "rows": store.list_watchlists(principal.user_id), "usage": store.usage(principal.user_id)}


@app.post("/v35/watchlists")
async def v35_watchlist_create(req: WatchlistCreateRequest, session_token: str | None = Cookie(default=None, alias=settings.v35_cookie_name), x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> dict:
    principal = _mutation_principal(session_token, x_csrf_token)
    store = _saas_required()
    usage = store.usage(principal.user_id)
    if usage["watchlists"] >= usage["watchlists_limit"]:
        raise HTTPException(403, "watchlist entitlement limit reached")
    try:
        row = store.create_watchlist(principal.user_id, req.name)
    except sqlite3.IntegrityError:
        raise HTTPException(409, "watchlist name already exists")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"version": "3.5.0", "watchlist": row}


@app.patch("/v35/watchlists/{watchlist_id}")
async def v35_watchlist_update(watchlist_id: str, req: WatchlistUpdateRequest, session_token: str | None = Cookie(default=None, alias=settings.v35_cookie_name), x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> dict:
    principal = _mutation_principal(session_token, x_csrf_token)
    ent = PLANS.get(principal.plan_code, PLANS["RESEARCH"])
    try:
        row = _saas_required().update_watchlist(principal.user_id, watchlist_id, req.name, req.symbols, ent["max_symbols_per_watchlist"])
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"version": "3.5.0", "watchlist": row}


@app.delete("/v35/watchlists/{watchlist_id}")
async def v35_watchlist_delete(watchlist_id: str, session_token: str | None = Cookie(default=None, alias=settings.v35_cookie_name), x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> dict:
    principal = _mutation_principal(session_token, x_csrf_token)
    try:
        _saas_required().delete_watchlist(principal.user_id, watchlist_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"version": "3.5.0", "deleted": True}


@app.get("/v35/preferences")
async def v35_preferences(session_token: str | None = Cookie(default=None, alias=settings.v35_cookie_name)) -> dict:
    principal, _ = _principal(session_token)
    return {"version": "3.5.0", "preferences": _saas_required().preferences(principal.user_id)}


@app.put("/v35/preferences")
async def v35_preferences_update(req: SaaSPreferencesRequest, session_token: str | None = Cookie(default=None, alias=settings.v35_cookie_name), x_csrf_token: str | None = Header(default=None, alias="X-CSRF-Token")) -> dict:
    principal = _mutation_principal(session_token, x_csrf_token)
    ent = PLANS.get(principal.plan_code, PLANS["RESEARCH"])
    try:
        prefs = _saas_required().update_preferences(principal.user_id, req.model_dump(), allow_telegram=bool(ent.get("telegram")))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"version": "3.5.0", "preferences": prefs}


@app.get("/", include_in_schema=False)
async def root() -> FileResponse:
    return FileResponse(FRONTEND / "index.html")



@app.get("/health")
async def health() -> dict:
    ops_runtime_v30.heartbeat()
    storage = storage_health_v30.run()
    return {
        "status": "ok",
        "version": "3.7.0",
        "sources": {
            "binance": settings.binance_enabled,
            "livecoinwatch": settings.lcw_enabled and bool(settings.lcw_api_key),
            "cryptometer": settings.cryptometer_enabled and bool(settings.cryptometer_api_key),
            "coinglass": settings.v24_intelligence_enabled and bool(settings.v24_coinglass_api_key),
            "geckoterminal": settings.v24_intelligence_enabled and settings.v24_gecko_enabled,
            "goplus": settings.v24_intelligence_enabled and settings.v24_goplus_enabled,
            "market_federation": True,
            "cmc": settings.v37_cmc_enabled,
            "oanda": settings.v37_oanda_enabled and bool(settings.v37_oanda_api_token and settings.v37_oanda_account_id),
            "fxcm": settings.v37_fxcm_enabled,
            "arkham": bool(settings.v37_arkham_api_key),
            "etherscan_v2": bool(settings.v37_etherscan_api_key),
            "tradingeconomics": settings.v38_tradingeconomics_enabled and bool(settings.v38_tradingeconomics_api_key),
            "macro_extended": True,
        },
        "monitoring": {"quarantine": monitoring_store.is_quarantined()},
        "resilience": {"enabled": settings.v25_enabled, "providers": GLOBAL_PROVIDER_HEALTH_V25.status(), "fail_closed_external": settings.v25_fail_closed_external},
        "runtime": ops_runtime_v30.status(),
        "storage": storage,
        "production_gate": production_gate_v30(app_env=settings.app_env, admin_api_key=settings.admin_api_key, require_admin_key=settings.v30_require_admin_key_in_prod),
    }



@app.get("/api/live/quotes")
async def api_live_quotes() -> dict[str, Any]:
    rows = sorted(store.live_quote_snapshot(), key=lambda x: str(x.get("symbol", "")))
    return {"version": "3.9.0", "source": "binance_futures_ws", "ts_ms": int(time.time() * 1000), "rows": rows, "count": len(rows)}


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            payload = {"type": "market_tick", "version": "3.9.0", "ts_ms": int(time.time() * 1000), "source": "binance_futures_ws", "quotes": store.live_quote_snapshot(), "scanner": store.snapshot()}
            await websocket.send_json(payload)
            await asyncio.sleep(1.0)
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        LOGGER.debug("live websocket closed: %s", exc)


@app.get("/v37/assets")
async def v37_assets(asset_class: str = "ALL") -> dict[str, Any]:
    rows = market_v37.assets()
    if asset_class.upper() != "ALL":
        rows = [r for r in rows if r["asset_class"] == asset_class.upper()]
    return {"version":"3.7.0", "rows":rows, "count":len(rows)}

@app.get("/v37/market/overview")
async def v37_market_overview(asset_class: str = "ALL", refresh: bool = True) -> dict[str, Any]:
    if refresh:
        await market_v37.refresh(asset_class)
    return market_v37.overview(asset_class)

@app.post("/v37/market/refresh")
async def v37_market_refresh(asset_class: str = "ALL") -> dict[str, Any]:
    return await market_v37.refresh(asset_class)

@app.get("/v37/market/status")
async def v37_market_status() -> dict[str, Any]:
    return {"version":"3.7.0", "providers":market_v37.store.provider_status(), "assets":len(market_v37.assets()), "core":"multi_asset_market_federation"}

@app.get("/v38/assets")
async def v38_assets(asset_class: str = "ALL") -> dict[str, Any]:
    rows = market_v38.assets()
    if asset_class.upper() != "ALL":
        rows = [r for r in rows if r["asset_class"] == asset_class.upper()]
    return {"version":"3.8.0", "rows":rows, "count":len(rows)}

@app.get("/v38/market/overview")
async def v38_market_overview(asset_class: str = "ALL", refresh: bool = True) -> dict[str, Any]:
    if refresh:
        await market_v38.refresh(asset_class)
    return market_v38.overview(asset_class)

@app.post("/v38/market/refresh")
async def v38_market_refresh(asset_class: str = "ALL") -> dict[str, Any]:
    return await market_v38.refresh(asset_class)

@app.get("/v38/market/status")
async def v38_market_status() -> dict[str, Any]:
    return market_v38.status()

@app.get("/v38/status")
async def v38_status() -> dict[str, Any]:
    return market_v38.status()

@app.get("/v38/tradingview/symbol")
async def v38_tradingview_symbol(symbol: str) -> dict[str, Any]:
    spec = market_v38.resolve(symbol)
    if spec is None:
        raise HTTPException(404, "symbol not found")
    return {"symbol":spec["symbol"], "tradingview_symbol":spec["tradingview_symbol"], "asset_class":spec["asset_class"], "display_name":spec["display_name"], "provider_symbols":spec["provider_symbols"], "aliases":list(spec.get("aliases", ())) }

@app.get("/v37/status")
async def v37_status() -> dict[str, Any]:
    return {"version":"3.7.0", "market_federation": {"assets":len(market_v37.assets()), "crypto_universe_cached":len(market_v37.store.crypto_universe(2000)), "providers":market_v37.store.provider_status()}, "onchain": await market_v37.onchain_overview()}

@app.get("/v37/onchain/overview")
async def v37_onchain_overview() -> dict[str, Any]:
    return await market_v37.onchain_overview()

@app.get("/v37/onchain/live")
async def v37_onchain_live() -> dict[str, Any]:
    return await market_v37.onchain_live()

@app.get("/v37/crypto/universe")
async def v37_crypto_universe(limit: int = 200) -> dict[str, Any]:
    return {"version":"3.7.0","rows":market_v37.store.crypto_universe(limit),"count":len(market_v37.store.crypto_universe(limit))}

@app.get("/v37/tradingview/symbol")
async def v37_tradingview_symbol(symbol: str) -> dict[str, Any]:
    key = symbol.upper().replace(" ", "")
    spec = ASSET_INDEX.get(key) if 'ASSET_INDEX' in globals() else None
    if spec is None:
        spec = next((a for a in market_v37.assets() if a.get("symbol","").upper()==key), None)
        if spec is None:
            raise HTTPException(404, "symbol not found")
        return {"symbol":spec["symbol"],"tradingview_symbol":spec["tradingview_symbol"]}
    return {"symbol":spec.symbol,"tradingview_symbol":spec.tradingview_symbol,"asset_class":spec.asset_class,"display_name":spec.display_name}

@app.get("/scanner/livecoinwatch")
async def livecoinwatch_scanner(timeframe: str = "day") -> dict:
    if not settings.lcw_enabled or not settings.lcw_api_key:
        raise HTTPException(503, "LiveCoinWatch is enabled but LCW_API_KEY is not configured")
    if timeframe not in {"hour", "day", "week", "month", "quarter", "year"}:
        raise HTTPException(400, "Unsupported timeframe")
    client = LiveCoinWatchClient(settings.lcw_api_key)
    rows = await client.btc_volume_band(
        min_btc=settings.lcw_volume_min_btc,
        max_btc=settings.lcw_volume_max_btc,
        max_pages=settings.lcw_max_pages,
    )
    gainers, losers = client.rank_movers(rows, field=timeframe)
    return {
        "source": "livecoinwatch",
        "currency": "BTC",
        "volume_band_btc": [settings.lcw_volume_min_btc, settings.lcw_volume_max_btc],
        "universe_size": len(rows),
        "gainers": gainers[:50],
        "losers": losers[:50],
    }


@app.get("/scanner/cryptometer")
async def cryptometer_scanner(timeframe: str = "5m") -> dict:
    if not settings.cryptometer_enabled or not settings.cryptometer_api_key:
        raise HTTPException(503, "CryptoMeter is enabled but CRYPTOMETER_API_KEY is not configured")
    client = CryptoMeterClient(settings.cryptometer_api_key, settings.cryptometer_base_url)
    return await client.gainers_losers(timeframe=timeframe)


@app.get("/scanner/snapshot")
async def snapshot() -> dict:
    detector_rows = detector_v40.results(500)
    if detector_rows.get("rows"):
        return {"rows": detector_rows["rows"], "count": detector_rows["count"], "source": "pump_dump_detector_v40", "detector_status": detector_v40.status()}
    return {"rows": store.snapshot(), "count": len(store.latest), "source": "native_fusion_engine", "detector_status": detector_v40.status()}


@app.get("/v40/detector/status")
async def v40_detector_status() -> dict:
    return detector_v40.status()


@app.get("/v40/detector/results")
async def v40_detector_results(limit: int = 100) -> dict:
    return detector_v40.results(limit)


@app.post("/v40/detector/run")
async def v40_detector_run() -> dict:
    result = await detector_v40.run_scan()
    if result.get("status") == "DISABLED":
        raise HTTPException(409, result.get("message", "Detector disabled"))
    if result.get("status") == "ERROR":
        raise HTTPException(502, result.get("error", "Detector scan failed"))
    return result


@app.get("/scanner/top")
async def top_scanner(limit: int = 20) -> dict:
    rows = store.snapshot()
    limit = max(1, min(limit, 100))
    pump = sorted(rows, key=lambda r: float(r.get("pump_score", 0)), reverse=True)[:limit]
    dump = sorted(rows, key=lambda r: float(r.get("dump_score", 0)), reverse=True)[:limit]
    return {"pump": pump, "dump": dump, "tracked": len(rows)}


@app.get("/v20/status")
async def v20_status() -> dict:
    return {"version": "2.0.0", "production": production_runtime.status(), "alerts": {"recent": alert_ledger.recent(10)}}


@app.get("/v20/alerts")
async def v20_alerts(limit: int = 100, action: str | None = None) -> dict:
    limit = max(1, min(limit, 1000))
    return {"version": "2.0.0", "rows": alert_ledger.recent(limit, action=action)}


@app.get("/v20/alerts/{alert_id}")
async def v20_alert(alert_id: str) -> dict:
    row = alert_ledger.get(alert_id)
    if row is None:
        raise HTTPException(404, "alert not found")
    return {"version": "2.0.0", "alert": row}


@app.get("/v20/models")
async def v20_models() -> dict:
    return {"version": "2.0.0", "models": alert_ledger.models()}


@app.post("/v20/admin/pause")
async def v20_pause(enabled: bool = True, reason: str = "manual", x_admin_key: str | None = Header(default=None)) -> dict:
    require_admin_key(settings.admin_api_key, x_admin_key)
    production_runtime.set_paused(enabled, reason)
    return {"version": "2.0.0", "paused": enabled, "reason": reason}


@app.post("/v20/models/register")
async def v20_register_model(name: str, version: str, path: str | None = None, status: str = "ACTIVE", x_admin_key: str | None = Header(default=None)) -> dict:
    require_admin_key(settings.admin_api_key, x_admin_key)
    model_id = alert_ledger.register_model(name, version, path, status=status)
    return {"version": "2.0.0", "model_id": model_id}


class PaperSubmitRequest(BaseModel):
    ts_ms: int
    symbol: str
    direction: str
    entry_price: float = Field(gt=0)
    stop_pct: float = Field(default=2.0, gt=0)
    target_pct: float = Field(default=5.0, gt=0)
    horizon_min: int = Field(default=60, gt=0)
    risk_score: float = 0.0
    fusion_score: float = 0.0
    ml_probability: float | None = None
    quality_tier: str = "B"
    expected_value_r: float | None = None
    slippage_bps: float = Field(default=2.0, ge=0)
    fee_bps_per_side: float = Field(default=4.0, ge=0)
    funding_cost_pct: float = 0.0
    latency_ms: int = Field(default=0, ge=0)


class MonitoringRequest(BaseModel):
    baseline_features: dict[str, list[float]] = Field(default_factory=dict)
    current_features: dict[str, list[float]] = Field(default_factory=dict)
    baseline_predictions: list[float] = Field(default_factory=list)
    current_predictions: list[float] = Field(default_factory=list)
    current_probabilities: list[float] = Field(default_factory=list)
    current_outcomes: list[int] = Field(default_factory=list)
    current_returns_r: list[float] = Field(default_factory=list)
    latest_data_ts_ms: int | None = None
    now_ms: int | None = None


@app.post("/monitoring/evaluate")
async def monitoring_evaluate(req: MonitoringRequest) -> dict:
    report = monitoring_engine.evaluate(**req.model_dump())
    monitoring_store.record(report)
    return {"version": "2.0.0", "status": report.status, "quarantine": report.quarantine, "report": {
        "ts_ms": report.ts_ms,
        "findings": [f.__dict__ if hasattr(f, "__dict__") else {"domain": f.domain, "metric": f.metric, "value": f.value, "threshold_warn": f.threshold_warn, "threshold_block": f.threshold_block, "severity": f.severity, "message": f.message} for f in report.findings],
        "feature_metrics": report.feature_metrics,
        "prediction_metrics": report.prediction_metrics,
        "calibration": report.calibration,
        "performance": report.performance,
        "freshness": report.freshness,
    }}


@app.get("/monitoring/status")
async def monitoring_status() -> dict:
    return {"version": "2.0.0", "mode": "MONITORING_ONLY", "runtime": monitoring_runtime.status(), "state": monitoring_store.state(), "recent": monitoring_store.recent(10)}


@app.post("/monitoring/quarantine")
async def monitoring_quarantine(enabled: bool = True, reason: str = "manual") -> dict:
    monitoring_store.set_quarantine(enabled, reason=reason)
    return {"version": "2.0.0", "quarantine": enabled, "reason": reason}


@app.get("/paper/status")
async def paper_status() -> dict:
    return {"version": "2.0.0", "mode": "PAPER_ONLY", "monitoring_quarantine": monitoring_store.is_quarantined(), **paper_engine.snapshot()}


@app.get("/paper/recent")
async def paper_recent(limit: int = 100) -> dict:
    limit = max(1, min(limit, 500))
    rows = paper_store.recent(limit)
    return {"rows": rows, "count": len(rows)}


@app.post("/paper/submit")
async def paper_submit(req: PaperSubmitRequest) -> dict:
    signal = build_paper_signal(req.model_dump())
    if ops_runtime_v30.kill_switch:
        return {"signal_id": signal.signal_id, "action": "KILL_SWITCH_ACTIVE", "mode": "PAPER_ONLY", "reason": ops_runtime_v30.kill_reason}
    if monitoring_store.is_quarantined():
        return {"signal_id": signal.signal_id, "action": "MONITORING_QUARANTINED", "mode": "PAPER_ONLY", "reason": "v1.9 monitoring quarantine is active"}
    action = paper_engine.submit(signal)
    return {"signal_id": signal.signal_id, "action": action, "mode": "PAPER_ONLY"}


class ValidationQueryRequest(BaseModel):
    cohort: str = "all"
    symbol: str | None = None
    quality_tier: str | None = None
    min_resolved: int = Field(default=settings.v21_min_resolved, ge=1)
    min_expectancy_r: float = settings.v21_min_expectancy_r
    max_drawdown_pct: float = Field(default=settings.v21_max_drawdown_pct, ge=0)
    max_ambiguous_rate: float = Field(default=settings.v21_max_ambiguous_rate, ge=0, le=1)
    max_no_data_rate: float = Field(default=settings.v21_max_no_data_rate, ge=0, le=1)
    min_positive_rate: float = Field(default=settings.v21_min_positive_rate, ge=0, le=1)
    max_calibration_brier: float | None = Field(default=settings.v21_max_calibration_brier, ge=0)
    bootstrap_iters: int = Field(default=settings.v21_bootstrap_iters, ge=200, le=20000)


def _validation_policy(req: ValidationQueryRequest) -> ValidationPolicy:
    return ValidationPolicy(
        min_resolved=req.min_resolved,
        min_expectancy_r=req.min_expectancy_r,
        max_drawdown_pct=req.max_drawdown_pct,
        max_ambiguous_rate=req.max_ambiguous_rate,
        max_no_data_rate=req.max_no_data_rate,
        min_positive_rate=req.min_positive_rate,
        max_calibration_brier=req.max_calibration_brier,
        bootstrap_iters=req.bootstrap_iters,
    )


@app.post("/v21/validation/paper")
async def v21_validate_paper(req: ValidationQueryRequest) -> dict:
    rows = load_paper_rows(settings.sqlite_path, cohort=req.cohort, symbol=req.symbol, quality_tier=req.quality_tier)
    report = validate_paper_rows(rows, cohort=req.cohort, policy=_validation_policy(req))
    report_id = validation_store.record(report)
    return {"version": "2.1.0", "report_id": report_id, "report": {
        "cohort": report.cohort, "generated_at_ms": report.generated_at_ms, "status": report.status,
        "sample_size": report.sample_size, "resolved": report.resolved,
        "expectancy_r": report.expectancy_r, "expectancy_ci_low": report.expectancy_ci_low,
        "expectancy_ci_high": report.expectancy_ci_high, "hit_rate": report.hit_rate,
        "max_drawdown_pct": report.max_drawdown_pct, "ambiguous_rate": report.ambiguous_rate,
        "no_data_rate": report.no_data_rate, "calibration_brier": report.calibration_brier,
        "checks": report.checks, "metadata": report.metadata,
    }}


@app.get("/v21/validation/recent")
async def v21_validation_recent(limit: int = 50) -> dict:
    return {"version": "2.1.0", "rows": validation_store.recent(limit)}


@app.get("/v21/promotions")
async def v21_promotions(limit: int = 50) -> dict:
    return {"version": "2.1.0", "rows": validation_store.promotions(limit)}


@app.post("/v21/models/{model_id}/request-promotion")
async def v21_request_promotion(model_id: str, name: str, version: str, report_id: str, x_admin_key: str | None = Header(default=None)) -> dict:
    require_admin_key(settings.admin_api_key, x_admin_key)
    reports = [x for x in validation_store.recent(500) if x.get("report_id") == report_id]
    if not reports:
        raise HTTPException(404, "validation report not found")
    report = reports[0]
    if report.get("status") != "PASS":
        raise HTTPException(409, "model is not eligible for promotion; validation status is not PASS")
    validation_store.request_promotion(model_id, name, version, report_id, status="CANDIDATE", reason="passed validation gate")
    return {"version": "2.1.0", "model_id": model_id, "status": "CANDIDATE", "report_id": report_id}


@app.post("/v21/models/{model_id}/promote")
async def v21_promote_model(model_id: str, x_admin_key: str | None = Header(default=None)) -> dict:
    require_admin_key(settings.admin_api_key, x_admin_key)
    rows = [x for x in validation_store.promotions(500) if x.get("model_id") == model_id]
    if not rows:
        raise HTTPException(404, "promotion candidate not found")
    candidate = rows[0]
    if candidate.get("status") != "CANDIDATE":
        raise HTTPException(409, "model must be in CANDIDATE state before promotion")
    validation_store.set_promotion(model_id, "PROMOTED", "manual controlled promotion after validation")
    return {"version": "2.1.0", "model_id": model_id, "status": "PROMOTED", "automatic": False}


@app.post("/v21/models/{model_id}/reject")
async def v21_reject_model(model_id: str, reason: str = "manual rejection", x_admin_key: str | None = Header(default=None)) -> dict:
    require_admin_key(settings.admin_api_key, x_admin_key)
    rows = [x for x in validation_store.promotions(500) if x.get("model_id") == model_id]
    if not rows:
        raise HTTPException(404, "promotion candidate not found")
    validation_store.set_promotion(model_id, "REJECTED", reason)
    return {"version": "2.1.0", "model_id": model_id, "status": "REJECTED", "reason": reason}


@app.get("/v22/observation/status")
async def v22_observation_status() -> dict:
    return {"version": "2.2.0", **observation_ledger.status()}

@app.get("/v22/observations")
async def v22_observations(limit: int = 100, symbol: str | None = None) -> dict:
    return {"version": "2.2.0", "rows": observation_ledger.recent(limit, symbol=symbol)}

@app.get("/v22/observations/{observation_id}")
async def v22_observation(observation_id: str) -> dict:
    row = observation_ledger.get(observation_id)
    if row is None:
        raise HTTPException(404, "observation not found")
    return {"version": "2.2.0", "observation": row}

@app.get("/v22/observation/verify/{session_id}")
async def v22_verify_observation(session_id: str, symbol: str | None = None) -> dict:
    return {"version": "2.2.0", "verification": observation_ledger.verify_chain(session_id, symbol=symbol)}


@app.get("/v23/enrichment/status")
async def v23_enrichment_status() -> dict:
    return {"version": "2.3.0", "runtime": enrichment_runtime.status() if enrichment_runtime is not None else {"schema_version": "enrichment_v23", "running": False, "configured": False}, "health": enrichment_store.latest_health()}

@app.get("/v23/enrichment/snapshots")
async def v23_enrichment_snapshots(limit: int = 50) -> dict:
    return {"version": "2.3.0", "rows": enrichment_store.recent_snapshots(limit)}

@app.get("/v23/enrichment/rows")
async def v23_enrichment_rows(limit: int = 100, symbol: str | None = None) -> dict:
    return {"version": "2.3.0", "rows": enrichment_store.recent_rows(limit, symbol=symbol)}


@app.get("/v25/resilience/status")
async def v25_resilience_status() -> dict:
    return {
        "version": "2.5.0",
        "enabled": settings.v25_enabled,
        "policy": {
            "timeout_s": settings.v25_http_timeout_seconds,
            "max_attempts": settings.v25_http_max_attempts,
            "circuit_failure_threshold": settings.v25_circuit_failure_threshold,
            "circuit_open_seconds": settings.v25_circuit_open_seconds,
            "max_response_bytes": settings.v25_max_response_bytes,
            "max_concurrency": settings.v25_max_concurrency,
            "min_request_interval_ms": settings.v25_min_request_interval_ms,
            "fail_closed_external": settings.v25_fail_closed_external,
        },
        "providers": GLOBAL_PROVIDER_HEALTH_V25.status(),
    }


@app.post("/v25/resilience/reset")
async def v25_resilience_reset(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> dict:
    require_admin_key(settings.admin_api_key, x_admin_key)
    GLOBAL_PROVIDER_HEALTH_V25.reset()
    return {"status": "reset", "providers": GLOBAL_PROVIDER_HEALTH_V25.status()}


@app.get("/v24/intelligence/status")
async def v24_intelligence_status() -> dict:
    return {"version": "2.4.0", **(intelligence_runtime_v24.status() if intelligence_runtime_v24 is not None else {"schema_version": "intelligence_v24", "running": False, "configured": False}), "store": intelligence_store_v24.status()}

@app.get("/v24/intelligence/rows")
async def v24_intelligence_rows(limit: int = 100, symbol: str | None = None) -> dict:
    return {"version": "2.4.0", "rows": intelligence_store_v24.recent(limit, symbol=symbol)}

@app.get("/v24/consensus")
async def v24_consensus(limit: int = 100, symbol: str | None = None) -> dict:
    return {"version": "2.4.0", "rows": intelligence_store_v24.recent_consensus(limit, symbol=symbol)}

class TokenSecurityRequest(BaseModel):
    chain_id: str = Field(min_length=1, max_length=32)
    contract_addresses: str = Field(min_length=4, max_length=5000)

@app.post("/v24/security/token")
async def v24_token_security(req: TokenSecurityRequest) -> dict:
    if not settings.v24_intelligence_enabled or not settings.v24_goplus_enabled:
        raise HTTPException(503, "GoPlus v2.4 intelligence is disabled")
    client = GoPlusClientV24(settings.v24_goplus_api_key)
    rows = await fetch_token_security(client, chain_id=req.chain_id, contract_addresses=req.contract_addresses, store=intelligence_store_v24)
    return {"version": "2.4.0", "rows": rows, "count": len(rows), "scoring_injection": False}


@app.get("/v33/status")
async def v33_status() -> dict:
    storage = storage_health_v30.run()
    redis = (await redis_health_v30.check()).__dict__
    dualwrite = (v32_controller.status().__dict__ if v32_controller is not None else {"mode": "disabled", "ready": True})
    components = build_component_matrix(
        settings=settings,
        storage={**storage, "postgres_reachable": bool(storage.get("postgres_reachable", False))},
        redis=redis,
        dualwrite={"ready": (v32_controller.ready(settings.v32_max_pending_events, settings.v32_max_pending_age_ms)[0] if v32_controller is not None else True), **dualwrite},
        monitoring_quarantine=monitoring_store.is_quarantined(),
    )
    prod = production_gate_v30(app_env=settings.app_env, admin_api_key=settings.admin_api_key, require_admin_key=settings.v30_require_admin_key_in_prod)
    readiness = integration_readiness(components, production=bool(prod["is_production"]))
    return {"version": "3.3.0", "schema_version": "production_integration_v33", "readiness": readiness, "components": [c.as_dict() for c in components], "validation": (v33_validation_store.status() if v33_validation_store is not None else {"enabled": False})}


class V33ValidationRequest(BaseModel):
    cohort: str = "all"
    symbol: str | None = None
    quality_tier: str | None = None
    direction: str | None = None
    min_resolved: int = Field(default=100, gt=0, le=1_000_000)
    bootstrap_iters: int = Field(default=2000, ge=200, le=100_000)
    min_expectancy_r: float = 0.10


@app.post("/v33/validation/run")
async def v33_validation_run(req: V33ValidationRequest) -> dict:
    if v33_validation_harness is None:
        raise HTTPException(503, "v3.3 validation is disabled")
    cohort = CohortSpecV33(req.cohort, req.symbol, req.quality_tier, req.direction)
    policy = ValidationPolicy(
        min_resolved=req.min_resolved,
        bootstrap_iters=req.bootstrap_iters,
        min_expectancy_r=req.min_expectancy_r,
    )
    config_hash = public_config_hash(settings.model_dump())
    run = v33_validation_harness.run(
        cohort=cohort, policy=policy, config_hash=config_hash,
        model_context=observation_runtime.model_context(),
    )
    METRICS_V30.inc("cfr_v33_validation_runs_total", labels={"decision": run.decision})
    return run.as_dict()


@app.get("/v33/validation/recent")
async def v33_validation_recent(limit: int = 50, cohort: str | None = None) -> dict:
    if v33_validation_store is None:
        raise HTTPException(503, "v3.3 validation is disabled")
    return {"version": "3.3.0", "rows": v33_validation_store.recent(limit, cohort=cohort)}


@app.get("/v30/runtime")
async def v30_runtime() -> dict:
    ops_runtime_v30.heartbeat()
    return {"version": "3.3.0", "runtime": ops_runtime_v30.status()}


@app.get("/v30/readiness")
async def v30_readiness() -> dict:
    storage = storage_health_v30.run()
    redis = await redis_health_v30.check()
    ops = ops_runtime_v30.readiness(
        storage=storage,
        redis={"reachable": redis.reachable},
        require_redis=settings.v30_require_redis,
        production=production_gate_v30(app_env=settings.app_env, admin_api_key=settings.admin_api_key, require_admin_key=settings.v30_require_admin_key_in_prod)["is_production"],
        max_heartbeat_age_s=settings.v30_max_heartbeat_age_seconds,
    )
    prod = production_gate_v30(app_env=settings.app_env, admin_api_key=settings.admin_api_key, require_admin_key=settings.v30_require_admin_key_in_prod)
    v32_ready, v32_reasons = (v32_controller.ready(settings.v32_max_pending_events, settings.v32_max_pending_age_ms) if v32_controller is not None else (True, []))
    if settings.v32_enabled and settings.v32_dualwrite_mode == "required" and not v32_ready:
        ops["ready"] = False
        ops.setdefault("reasons", []).extend(v32_reasons)
    ready = bool(ops["ready"] and prod["ready"])
    return {"version": "3.3.0", "ready": ready, "runtime": ops, "production_gate": prod, "storage": storage, "redis": redis.__dict__, "v32_dualwrite": {"ready": v32_ready, "reasons": v32_reasons, "status": (v32_controller.status().__dict__ if v32_controller is not None else {"mode": "disabled"})}}


@app.get("/v30/metrics")
async def v30_metrics() -> PlainTextResponse:
    return PlainTextResponse(METRICS_V30.prometheus(), media_type="text/plain; version=0.0.4")


@app.post("/v30/kill-switch")
async def v30_kill_switch(enabled: bool = True, reason: str = "manual", x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> dict:
    require_admin_key(settings.admin_api_key, x_admin_key)
    ops_runtime_v30.set_kill(enabled, reason)
    production_runtime.set_paused(enabled, reason)
    METRICS_V30.set_gauge("cfr_kill_switch", 1 if enabled else 0)
    return {"version": "3.3.0", "kill_switch": enabled, "reason": reason}


@app.get("/v30/storage")
async def v30_storage() -> dict:
    return {"version": "3.3.0", "storage": storage_health_v30.run(), "redis": (await redis_health_v30.check()).__dict__, "v32_dualwrite": (v32_controller.status().__dict__ if v32_controller is not None else {"mode": "disabled"})}


@app.get("/v32/storage")
async def v32_storage() -> dict:
    if v32_controller is None:
        return {"version": "3.3.0", "enabled": False, "mode": "disabled"}
    status = v32_controller.status()
    ready, reasons = v32_controller.ready(settings.v32_max_pending_events, settings.v32_max_pending_age_ms)
    return {"version": "3.3.0", "enabled": True, "ready": ready, "reasons": reasons, "status": status.__dict__, "tables": list(_v32_table_names)}


@app.post("/v32/storage/snapshot")
async def v32_snapshot(x_admin_key: str | None = Header(default=None, alias="X-Admin-Key")) -> dict:
    require_admin_key(settings.admin_api_key, x_admin_key)
    if v32_outbox is None or v32_controller is None:
        raise HTTPException(503, "v3.2 dual-write is disabled")
    if v32_postgres is None:
        raise HTTPException(503, "v3.2 PostgreSQL ledger is not configured or reachable")
    result = v32_postgres.apply_snapshot_from_sqlite(v32_outbox, _v32_table_names)
    return {"version": "3.3.0", "result": result}
