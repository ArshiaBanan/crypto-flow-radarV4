from __future__ import annotations

import asyncio
import hashlib
import json as jsonlib
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

import httpx


class ProviderUnavailable(RuntimeError):
    """Raised when a provider is intentionally unavailable by resilience policy."""


class SchemaValidationError(ValueError):
    """Raised when a provider returns an unexpected JSON shape."""


@dataclass(slots=True)
class ResilienceConfigV25:
    timeout_s: float = 15.0
    max_attempts: int = 3
    base_backoff_s: float = 0.5
    max_backoff_s: float = 8.0
    retryable_statuses: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})
    circuit_failure_threshold: int = 3
    circuit_open_seconds: float = 30.0
    max_response_bytes: int = 4_000_000
    max_concurrency: int = 4
    min_request_interval_ms: int = 0

    def normalized(self) -> "ResilienceConfigV25":
        return ResilienceConfigV25(
            timeout_s=max(0.1, float(self.timeout_s)),
            max_attempts=max(1, int(self.max_attempts)),
            base_backoff_s=max(0.0, float(self.base_backoff_s)),
            max_backoff_s=max(0.0, float(self.max_backoff_s)),
            retryable_statuses=frozenset(int(x) for x in self.retryable_statuses),
            circuit_failure_threshold=max(1, int(self.circuit_failure_threshold)),
            circuit_open_seconds=max(0.0, float(self.circuit_open_seconds)),
            max_response_bytes=max(1_024, int(self.max_response_bytes)),
            max_concurrency=max(1, int(self.max_concurrency)),
            min_request_interval_ms=max(0, int(self.min_request_interval_ms)),
        )


@dataclass(slots=True)
class ProviderStateV25:
    provider: str
    state: str = "CLOSED"
    consecutive_failures: int = 0
    total_requests: int = 0
    successes: int = 0
    failures: int = 0
    retry_count: int = 0
    last_status_code: int | None = None
    last_latency_ms: int | None = None
    last_success_ms: int | None = None
    last_failure_ms: int | None = None
    opened_ms: int | None = None
    last_error: str | None = None


class ProviderHealthRegistryV25:
    def __init__(self, config: ResilienceConfigV25 | None = None, clock: Callable[[], int] | None = None):
        self.config = (config or ResilienceConfigV25()).normalized()
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._states: dict[str, ProviderStateV25] = {}

    def _state(self, provider: str) -> ProviderStateV25:
        provider = str(provider).strip().lower()
        if not provider:
            raise ValueError("provider is required")
        return self._states.setdefault(provider, ProviderStateV25(provider=provider))

    def allow(self, provider: str) -> bool:
        st = self._state(provider)
        now = self._clock()
        if st.state == "OPEN":
            if st.opened_ms is None or now - st.opened_ms < int(self.config.circuit_open_seconds * 1000):
                return False
            st.state = "HALF_OPEN"
        return True

    def begin(self, provider: str) -> None:
        st = self._state(provider)
        st.total_requests += 1

    def success(self, provider: str, latency_ms: int, status_code: int | None = None) -> None:
        st = self._state(provider)
        st.state = "CLOSED"
        st.consecutive_failures = 0
        st.successes += 1
        st.last_success_ms = self._clock()
        st.last_latency_ms = int(latency_ms)
        st.last_status_code = status_code
        st.last_error = None
        st.opened_ms = None

    def retry(self, provider: str) -> None:
        self._state(provider).retry_count += 1

    def failure(self, provider: str, error: str, latency_ms: int, status_code: int | None = None) -> None:
        st = self._state(provider)
        st.failures += 1
        st.consecutive_failures += 1
        st.last_failure_ms = self._clock()
        st.last_latency_ms = int(latency_ms)
        st.last_status_code = status_code
        st.last_error = str(error)[:500]
        if st.consecutive_failures >= self.config.circuit_failure_threshold:
            st.state = "OPEN"
            st.opened_ms = self._clock()
        elif st.state == "HALF_OPEN":
            st.state = "OPEN"
            st.opened_ms = self._clock()

    def reset(self, provider: str | None = None) -> None:
        if provider is None:
            for st in self._states.values():
                st.state = "CLOSED"; st.consecutive_failures = 0; st.opened_ms = None; st.last_error = None
            return
        self._states.pop(str(provider).strip().lower(), None)

    def status(self) -> list[dict[str, Any]]:
        return [
            {
                "provider": st.provider,
                "state": st.state,
                "consecutive_failures": st.consecutive_failures,
                "total_requests": st.total_requests,
                "successes": st.successes,
                "failures": st.failures,
                "retry_count": st.retry_count,
                "last_status_code": st.last_status_code,
                "last_latency_ms": st.last_latency_ms,
                "last_success_ms": st.last_success_ms,
                "last_failure_ms": st.last_failure_ms,
                "opened_ms": st.opened_ms,
                "last_error": st.last_error,
            }
            for st in sorted(self._states.values(), key=lambda x: x.provider)
        ]

    def get(self, provider: str) -> dict[str, Any]:
        st = self._state(provider)
        return next(x for x in self.status() if x["provider"] == st.provider)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        val = float(raw)
        return max(0.0, val)
    except (TypeError, ValueError):
        return None


async def _sleep_backoff(seconds: float) -> None:
    await asyncio.sleep(max(0.0, seconds))


class ResilientHTTPTransportV25:
    def __init__(self, registry: ProviderHealthRegistryV25, config: ResilienceConfigV25 | None = None, sleep_fn: Callable[[float], Any] | None = None):
        self.registry = registry
        self.config = (config or registry.config).normalized()
        self.sleep_fn = sleep_fn or _sleep_backoff
        self._semaphore = asyncio.Semaphore(self.config.max_concurrency)
        self._rate_lock = asyncio.Lock()
        self._last_start_monotonic = 0.0

    async def _pace(self) -> None:
        interval = self.config.min_request_interval_ms / 1000.0
        if interval <= 0:
            return
        async with self._rate_lock:
            now = time.monotonic()
            wait = interval - (now - self._last_start_monotonic)
            if wait > 0:
                await self.sleep_fn(wait)
            self._last_start_monotonic = time.monotonic()


    async def request_json(
        self,
        provider: str,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        expected_types: tuple[type, ...] = (dict, list),
        idempotent: bool = True,
    ) -> Any:
        method = method.upper()
        attempts = self.config.max_attempts if idempotent else 1
        if not self.registry.allow(provider):
            raise ProviderUnavailable(f"circuit_open:{provider}")
        last_exc: Exception | None = None
        request_headers = dict(headers or {})
        request_fingerprint = hashlib.sha256((provider + "|" + method + "|" + url + "|" + jsonlib.dumps(dict(params or {}), sort_keys=True, default=str) + "|" + jsonlib.dumps(json, sort_keys=True, default=str)).encode()).hexdigest()[:24]
        request_headers.setdefault("X-CFR-Request-ID", request_fingerprint)
        for attempt in range(1, attempts + 1):
            async with self._semaphore:
                await self._pace()
                self.registry.begin(provider)
                started = time.perf_counter()
                response = None
                try:
                    async with httpx.AsyncClient(timeout=self.config.timeout_s) as client:
                        response = await client.request(method, url, headers=request_headers, params=dict(params or {}), json=json)
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    length_header = response.headers.get("content-length")
                    if length_header:
                        try:
                            if int(length_header) > self.config.max_response_bytes:
                                raise SchemaValidationError(f"response_too_large:{length_header}")
                        except ValueError:
                            pass
                    if response.status_code in self.config.retryable_statuses and attempt < attempts:
                        self.registry.retry(provider)
                        delay = _retry_after(response.headers)
                        if delay is None:
                            delay = min(self.config.max_backoff_s, self.config.base_backoff_s * (2 ** (attempt - 1)))
                        await self.sleep_fn(delay)
                        continue
                    response.raise_for_status()
                    if len(response.content) > self.config.max_response_bytes:
                        raise SchemaValidationError(f"response_too_large:{len(response.content)}")
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        raise SchemaValidationError("invalid_json") from exc
                    if not isinstance(payload, expected_types):
                        raise SchemaValidationError(f"unexpected_json_type:{type(payload).__name__}")
                    self.registry.success(provider, latency_ms, response.status_code)
                    return payload
                except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    last_exc = exc
                    if attempt < attempts:
                        self.registry.retry(provider)
                        delay = min(self.config.max_backoff_s, self.config.base_backoff_s * (2 ** (attempt - 1)))
                        await self.sleep_fn(delay)
                        continue
                    self.registry.failure(provider, str(exc), latency_ms)
                    raise
                except Exception as exc:
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    last_exc = exc
                    status_code = response.status_code if response is not None else None
                    self.registry.failure(provider, str(exc), latency_ms, status_code)
                    raise
        raise ProviderUnavailable(f"request_exhausted:{provider}:{last_exc}")


class Schema:
    @staticmethod
    def require_mapping(payload: Any, *, name: str = "payload") -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise SchemaValidationError(f"{name}_must_be_object")
        return payload

    @staticmethod
    def require_list(payload: Any, *, name: str = "payload") -> list[Any]:
        if not isinstance(payload, list):
            raise SchemaValidationError(f"{name}_must_be_array")
        return payload

    @staticmethod
    def optional_dict_field(payload: dict[str, Any], key: str) -> dict[str, Any] | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise SchemaValidationError(f"{key}_must_be_object")
        return value

# Process-wide registry: all provider clients share one resilience state per provider.
GLOBAL_PROVIDER_HEALTH_V25 = ProviderHealthRegistryV25()
