from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from app.collectors.binance_futures import BinanceFuturesClient
from app.features.engine import OneMinuteAggregator, build_features
from app.anomaly import anomaly_features
from app.signals.scorer import score_features
from app.production_v20 import ProductionRuntimeV20, CorrelationExposure
from app.storage.in_memory import InMemoryStore
from app.storage.sqlite import SQLiteStore

logger = logging.getLogger(__name__)


class BinanceDataEngine:
    """Live ingestion -> raw provenance -> 1m bars -> features -> transparent score."""

    def __init__(self, client: BinanceFuturesClient, memory: InMemoryStore, durable: SQLiteStore, max_symbols: int = 80, paper_engine=None, monitoring_runtime=None, production_runtime: ProductionRuntimeV20 | None = None, correlation_exposure: CorrelationExposure | None = None, observation_runtime=None, enrichment_runtime=None):
        self.client = client
        self.memory = memory
        self.db = durable
        self.max_symbols = max_symbols
        self.paper_engine = paper_engine
        self.monitoring_runtime = monitoring_runtime
        self.production_runtime = production_runtime
        self.correlation_exposure = correlation_exposure
        self.observation_runtime = observation_runtime
        self.enrichment_runtime = enrichment_runtime
        self.agg = OneMinuteAggregator()
        self.depth: dict[str, tuple[float, float]] = {}
        self.best_book: dict[str, tuple[float, float]] = {}
        self.oi: dict[str, tuple[float, int]] = {}
        self.funding: dict[str, float] = {}
        self.liq: dict[str, dict[str, float]] = defaultdict(lambda: {"long": 0.0, "short": 0.0})
        self.running = False
        self.symbols: list[str] = []

    async def refresh_derivatives_snapshot(self) -> None:
        if not self.symbols:
            return
        sem = asyncio.Semaphore(8)

        async def one(symbol: str) -> None:
            async with sem:
                try:
                    oi = await self.client.open_interest(symbol)
                    prem = await self.client.premium_index(symbol)
                    now = int(time.time() * 1000)
                    if oi is not None:
                        self.oi[symbol] = (oi, now)
                        self.db.write_oi(symbol, now, oi)
                    self.funding[symbol] = float(prem.get("lastFundingRate", 0.0))
                except Exception as exc:
                    logger.debug("snapshot failed %s: %s", symbol, exc)

        await asyncio.gather(*(one(s) for s in self.symbols))

    async def _on_event(self, envelope: dict) -> None:
        data = envelope.get("data", envelope)
        event = data.get("e")
        symbol = data.get("s", "").lower()
        ts = int(data.get("E") or time.time() * 1000)
        if not symbol:
            return
        self.db.write_raw(ts, "binance_futures", symbol, event or "unknown", data)
        self.memory.add_event("binance_futures", data)

        if event == "aggTrade":
            price = float(data["p"])
            qty = float(data["q"])
            quote = price * qty
            self.memory.upsert_live_quote(symbol, {"symbol": symbol, "price": price, "quantity": qty, "quote_value": quote, "trade_count": 1, "ts_ms": ts, "source": "binance_futures_ws"})
            finished = self.agg.trade(symbol, ts, price, quote, bool(data.get("m", False)))
            if finished:
                row = {
                    "symbol": symbol,
                    "bucket_ms": finished.bucket_ms,
                    "open": finished.open,
                    "high": finished.high,
                    "low": finished.low,
                    "close": finished.close,
                    "volume_quote": finished.volume_quote,
                    "buy_quote": finished.buy_quote,
                    "sell_quote": finished.sell_quote,
                    "trades": finished.trades,
                    "source": "binance_live",
                }
                self.db.upsert_bar(row)
                bars = self.db.recent_bars(symbol, 120)
                oi_change = self._oi_change(symbol, finished.bucket_ms + 59_999)
                features = build_features(
                    bars,
                    oi_pct=oi_change,
                    funding=self.funding.get(symbol) if self.funding.get(symbol) is not None else self.db.funding_at(symbol, finished.bucket_ms + 59_999),
                    bid_value=self.depth.get(symbol, (None, None))[0],
                    ask_value=self.depth.get(symbol, (None, None))[1],
                    long_liq=self.liq[symbol]["long"],
                    short_liq=self.liq[symbol]["short"],
                )
                recent = bars[-61:]
                if len(recent) >= 2:
                    closes = [float(x["close"]) for x in recent]
                    highs = [float(x["high"]) for x in recent]
                    lows = [float(x["low"]) for x in recent]
                    volumes = [float(x["volume_quote"]) for x in recent]
                    from app.regime import advanced_features
                    features.update({
                        "ret_5m": advanced_features(closes, volumes, highs, lows)["ret_5m"],
                        "ret_15m": advanced_features(closes, volumes, highs, lows)["ret_15m"],
                        "ret_1h": advanced_features(closes, volumes, highs, lows)["ret_1h"],
                    })
                    adv = advanced_features(closes, volumes, highs, lows)
                    features.update({
                        "realized_vol_20": adv["realized_vol_20"],
                        "atr20": adv["atr20"],
                        "range_pct": adv["range_pct"],
                        "volume_ratio_20": adv["volume_ratio_20"],
                    })
                bid_ask = self.best_book.get(symbol)
                if bid_ask and bid_ask[0] > 0 and bid_ask[1] > 0:
                    bid_px, ask_px = bid_ask
                    mid = (bid_px + ask_px) / 2.0
                    features["spread_bps"] = (ask_px - bid_px) / mid * 10_000.0 if mid else 0.0
                    depth_usd = sum(self.depth.get(symbol, (0.0, 0.0)))
                    features["depth_usd"] = depth_usd
                    v = max(float(finished.volume_quote), 1.0)
                    ratio = depth_usd / v
                    features["liquidity_regime"] = "deep" if ratio >= 1.0 else "normal" if ratio >= 0.30 else "thin" if ratio >= 0.10 else "very_thin"
                else:
                    features["liquidity_regime"] = "unknown"
                features["symbol"] = symbol
                features["ts_ms"] = finished.bucket_ms + 59_999
                anomaly = anomaly_features(features)
                features.update(anomaly)
                score = score_features(features)
                row_out = {"symbol": symbol, "ts_ms": finished.bucket_ms + 59_999, **features, **score}
                self.memory.upsert_latest(symbol, row_out)
                self.db.write_features(row_out["ts_ms"], symbol, "1m", features, score["pump_score"], score["dump_score"], score["stage"], anomaly["anomaly_score"], anomaly["anomaly_classification"], anomaly["anomaly_flags"])
                observation_id = None
                if self.observation_runtime is not None:
                    try:
                        observation_id = self.observation_runtime.record_snapshot(
                            ts_ms=row_out["ts_ms"], symbol=symbol, price=float(row_out["close"]),
                            source="binance_live", features=features,
                            pump_score=score.get("pump_score"), dump_score=score.get("dump_score"),
                            model_context={**self.observation_runtime.model_context(), "source_context": self.enrichment_runtime.feature_context(symbol) if self.enrichment_runtime is not None else {}},
                            config_hash=self.observation_runtime.config_hash,
                        )
                    except Exception:
                        logger.exception("observation recorder failed for %s", symbol)
                if self.monitoring_runtime is not None:
                    try:
                        self.monitoring_runtime.observe_features(symbol, row_out["ts_ms"], features)
                    except Exception:
                        logger.exception("monitoring runtime failed for %s", symbol)
                if self.paper_engine is not None:
                    try:
                        finished_results = self.paper_engine.on_bar(
                            symbol,
                            (finished.bucket_ms, finished.open, finished.high, finished.low, finished.close),
                        )
                        if observation_id and self.observation_runtime is not None:
                            for result in finished_results:
                                self.observation_runtime.record_paper_resolved(
                                    observation_id, result.signal_id, result.as_dict() if hasattr(result, "as_dict") else result.__dict__, int(row_out["ts_ms"])
                                )
                    except Exception:
                        logger.exception("paper engine bar processing failed for %s", symbol)
                if self.production_runtime is not None:
                    try:
                        active = self.production_runtime.ledger.active_alerts(int(row_out["ts_ms"]))
                        corr = 0.0
                        corr_details = []
                        if self.correlation_exposure is not None:
                            corr, corr_details = self.correlation_exposure.exposure(symbol, active)
                        result = await self.production_runtime.consider(
                            row_out, score["pump_score"], score["dump_score"],
                            correlated_exposure_pct=corr, correlation_details=corr_details,
                        )
                        if observation_id and self.observation_runtime is not None and result is not None:
                            event, risk = result
                            self.observation_runtime.record_alert_created(
                                observation_id, event.alert_id,
                                {"action": event.action, "quality_tier": event.quality_tier,
                                 "fusion_score": event.fusion_score, "risk_score": event.risk_score,
                                 "expected_value_r": event.expected_value_r, "net_rr": event.net_rr,
                                 "direction": event.direction},
                                int(row_out["ts_ms"])
                            )
                        self.production_runtime.ledger.heartbeat("binance", int(row_out["ts_ms"]), "OK", {"symbol": symbol, "observation_id": observation_id})
                    except Exception:
                        logger.exception("production v2 pipeline failed for %s", symbol)
                # Liquidation buckets are event-flow measures; reset after snapshot to avoid double counting.
                self.liq[symbol] = {"long": 0.0, "short": 0.0}

        elif event == "depthUpdate":
            bids = data.get("b", [])[: self.client.depth_levels]
            asks = data.get("a", [])[: self.client.depth_levels]
            bid_value = sum(float(p) * float(q) for p, q in bids)
            ask_value = sum(float(p) * float(q) for p, q in asks)
            self.depth[symbol] = (bid_value, ask_value)
            if bids and asks:
                self.best_book[symbol] = (float(bids[0][0]), float(asks[0][0]))

        elif event == "markPriceUpdate":
            self.funding[symbol] = float(data.get("r", 0.0))

        elif event == "forceOrder":
            order = data.get("o", {})
            side = order.get("S")
            qty = float(order.get("q", 0.0))
            price = float(order.get("ap") or order.get("p") or 0.0)
            usd = qty * price
            if side == "SELL":
                self.liq[symbol]["long"] += usd
            elif side == "BUY":
                self.liq[symbol]["short"] += usd

    def _oi_change(self, symbol: str, ts_ms: int | None = None) -> float | None:
        if ts_ms is None:
            return self.db.oi_change_pct(symbol, lookback_ms=5 * 60_000)
        return self.db.oi_change_pct_at(symbol, ts_ms, lookback_ms=5 * 60_000)

    async def run(self) -> None:
        self.running = True
        self.symbols = await self.client.top_usdt_symbols(self.max_symbols)
        logger.info("tracking %d symbols", len(self.symbols))
        snapshot_task = asyncio.create_task(self._snapshot_loop())
        try:
            await self.client.stream(self.symbols, self._on_event)
        finally:
            snapshot_task.cancel()
            self.running = False

    async def _snapshot_loop(self) -> None:
        while True:
            try:
                await self.refresh_derivatives_snapshot()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("derivatives snapshot loop failed")
            await asyncio.sleep(30)
