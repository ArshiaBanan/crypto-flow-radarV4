from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class _Counter:
    value: int = 0


@dataclass
class _Gauge:
    value: float = 0.0


class MetricsRegistryV30:
    """Small dependency-free metrics registry with Prometheus-compatible text output."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], _Counter] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], _Gauge] = {}
        self._hist_sum: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._hist_count: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}

    @staticmethod
    def _key(labels: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
        return tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))

    @staticmethod
    def _labels_text(labels: tuple[tuple[str, str], ...]) -> str:
        if not labels:
            return ""
        escaped = []
        for k, v in labels:
            v = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
            escaped.append(f'{k}="{v}"')
        return "{" + ",".join(escaped) + "}"

    def inc(self, name: str, amount: int = 1, labels: dict[str, Any] | None = None) -> None:
        key = (name, self._key(labels))
        with self._lock:
            item = self._counters.setdefault(key, _Counter())
            item.value += int(amount)

    def set_gauge(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        key = (name, self._key(labels))
        with self._lock:
            self._gauges[key] = _Gauge(float(value))

    def observe(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        key = (name, self._key(labels))
        with self._lock:
            self._hist_sum[key] = self._hist_sum.get(key, 0.0) + float(value)
            self._hist_count[key] = self._hist_count.get(key, 0) + 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "counters": [
                    {"name": n, "labels": dict(labels), "value": c.value}
                    for (n, labels), c in self._counters.items()
                ],
                "gauges": [
                    {"name": n, "labels": dict(labels), "value": g.value}
                    for (n, labels), g in self._gauges.items()
                ],
                "histograms": [
                    {"name": n, "labels": dict(labels), "sum": total, "count": self._hist_count[(n, labels)]}
                    for (n, labels), total in self._hist_sum.items()
                ],
            }

    def prometheus(self) -> str:
        lines: list[str] = []
        with self._lock:
            names = {n for n, _ in self._counters} | {n for n, _ in self._gauges} | {n for n, _ in self._hist_sum}
            for name in sorted(names):
                has_counter = any(n == name for n, _ in self._counters)
                has_gauge = any(n == name for n, _ in self._gauges)
                has_summary = any(n == name for n, _ in self._hist_sum)
                safe = name.replace("-", "_")
                kind = "summary" if has_summary else ("counter" if has_counter else "gauge")
                lines.append(f"# TYPE {safe} {kind}")
                for (n, labels), c in self._counters.items():
                    if n == name:
                        lines.append(f"{safe}{self._labels_text(labels)} {c.value}")
                for (n, labels), g in self._gauges.items():
                    if n == name:
                        lines.append(f"{safe}{self._labels_text(labels)} {g.value}")
                for (n, labels), total in self._hist_sum.items():
                    if n == name:
                        lines.append(f"{safe}_sum{self._labels_text(labels)} {total}")
                        lines.append(f"{safe}_count{self._labels_text(labels)} {self._hist_count[(n, labels)]}")
        return "\n".join(lines) + "\n"


METRICS_V30 = MetricsRegistryV30()


class JsonFormatterV30(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("request_id", "path", "method", "status_code", "duration_ms", "provider"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging_v30(level: str = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatterV30())
        root.addHandler(handler)
    else:
        for handler in root.handlers:
            handler.setFormatter(JsonFormatterV30())
