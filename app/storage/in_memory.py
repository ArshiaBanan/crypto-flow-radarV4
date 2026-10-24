from __future__ import annotations

from collections import deque
from typing import Any


class InMemoryStore:
    """Temporary store for v0. Replace with Redis/PostgreSQL adapters next."""

    def __init__(self, max_events: int = 20_000):
        self.events = deque(maxlen=max_events)
        self.latest: dict[str, dict[str, Any]] = {}
        self.live_quotes: dict[str, dict[str, Any]] = {}

    def add_event(self, source: str, payload: dict[str, Any]) -> None:
        self.events.append({"source": source, "payload": payload})

    def upsert_latest(self, symbol: str, row: dict[str, Any]) -> None:
        self.latest[symbol] = row

    def snapshot(self) -> list[dict[str, Any]]:
        return list(self.latest.values())

    def upsert_live_quote(self, symbol: str, quote: dict[str, Any]) -> None:
        self.live_quotes[symbol.lower()] = quote

    def live_quote_snapshot(self) -> list[dict[str, Any]]:
        return list(self.live_quotes.values())
