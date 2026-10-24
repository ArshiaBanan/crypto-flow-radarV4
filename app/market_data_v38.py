from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from typing import Any

from app.market_data_v37 import (
    AssetSpecV37,
    CORE_ASSETS as CORE_ASSETS_V37,
    MarketDataFederationV37,
)

SCHEMA_VERSION = "market_data_v38"


@dataclass(frozen=True)
class AssetSpecV38(AssetSpecV37):
    quote_unit: str = "USD"
    aliases: tuple[str, ...] = ()
    live_source_priority: tuple[str, ...] = ()


# Canonical additions requested by the product scope.
# The registry deliberately separates the user's canonical symbol from each provider's native symbol.
MACRO_ASSETS_V38: tuple[AssetSpecV38, ...] = (
    AssetSpecV38(
        "index.us30", "INDEX", "US30", "Dow Jones Industrial Average",
        "FXCM:US30", ("oanda", "fxcm"),
        {"oanda": "US30_USD", "fxcm": "US30"}, "US Equity Index",
        quote_unit="points", aliases=("DJI", "DOW", "DOWJONES"), live_source_priority=("oanda", "fxcm"),
    ),
    AssetSpecV38(
        "index.spx500", "INDEX", "SPX500", "S&P 500 Index",
        "FXCM:SPX500", ("oanda", "fxcm"),
        {"oanda": "SPX500_USD", "fxcm": "SPX500"}, "US Equity Index",
        quote_unit="points", aliases=("SPX", "S&P500", "SP500", "US500"), live_source_priority=("oanda", "fxcm"),
    ),
    AssetSpecV38(
        "index.us100", "INDEX", "US100", "NASDAQ-100",
        "FXCM:NAS100", ("oanda", "fxcm"),
        {"oanda": "NAS100_USD", "fxcm": "NAS100"}, "US Equity Index",
        quote_unit="points", aliases=("NAS100", "NASDAQ", "NASDAQ100"), live_source_priority=("oanda", "fxcm"),
    ),
    AssetSpecV38(
        "energy.usoil", "ENERGY", "USOIL", "WTI Crude Oil",
        "TVC:USOIL", ("oanda", "fxcm"),
        {"oanda": "WTICO_USD", "fxcm": "USOilSpot"}, "Energy",
        quote_unit="USD/barrel", aliases=("WTI", "WTIUSD", "USOILSPOT", "USOIL"), live_source_priority=("oanda", "fxcm"),
    ),
    AssetSpecV38(
        "energy.ukoil", "ENERGY", "UKOIL", "Brent Crude Oil",
        "TVC:UKOIL", ("oanda", "fxcm"),
        {"oanda": "BCO_USD", "fxcm": "UKOilSpot"}, "Energy",
        quote_unit="USD/barrel", aliases=("BRENT", "BRENTUSD", "UKOILSPOT", "UKOIL"), live_source_priority=("oanda", "fxcm"),
    ),
    AssetSpecV38(
        "macro.dxy", "MACRO", "DXY", "U.S. Dollar Index",
        "TVC:DXY", ("tradingeconomics",),
        {"tradingeconomics": "DXY:CUR"}, "Dollar Index",
        quote_unit="index", aliases=("USDX", "DOLLARINDEX"), live_source_priority=("tradingeconomics",),
    ),
    AssetSpecV38(
        "dominance.btc", "DOMINANCE", "BTC.D", "Bitcoin Dominance",
        "CRYPTOCAP:BTC.D", ("coinmarketcap",),
        {"coinmarketcap": "BTC.D"}, "Crypto Dominance",
        quote_unit="%", aliases=("BTCD", "BTC_DOMINANCE"), live_source_priority=("coinmarketcap",),
    ),
    AssetSpecV38(
        "dominance.usdt", "DOMINANCE", "USDT.D", "Tether Dominance",
        "CRYPTOCAP:USDT.D", ("coinmarketcap",),
        {"coinmarketcap": "USDT.D"}, "Crypto Dominance",
        quote_unit="%", aliases=("USDTD", "USDT_DOMINANCE"), live_source_priority=("coinmarketcap",),
    ),
)

CORE_ASSETS_V38: tuple[AssetSpecV37 | AssetSpecV38, ...] = CORE_ASSETS_V37 + MACRO_ASSETS_V38
ASSET_INDEX_V38 = {a.symbol.upper(): a for a in CORE_ASSETS_V38}
ASSET_ALIAS_INDEX_V38 = {
    alias.upper().replace(" ", ""): a for a in MACRO_ASSETS_V38 for alias in (a.aliases + (a.symbol,))
}


class TradingEconomicsV38:
    def __init__(self, transport: Any, api_key: str | None, base_url: str = "https://api.tradingeconomics.com"):
        self.transport = transport
        self.api_key = api_key
        self.base = base_url.rstrip("/")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    async def market_quote(self, symbol: str) -> Any:
        if not self.configured:
            raise RuntimeError("Trading Economics API key not configured")
        path = f"{self.base}/markets/symbol/{symbol}"
        return await self.transport.request_json(
            "tradingeconomics_v38", "GET", path,
            params={"c": self.api_key}, expected_types=(list, dict)
        )


class MarketDataFederationV38(MarketDataFederationV37):
    """Extends v3.7 federation with index, energy, DXY and crypto-dominance context."""

    def __init__(self, sqlite_path: str, settings: Any):
        super().__init__(sqlite_path, settings)
        self.tradingeconomics = TradingEconomicsV38(
            self.transport,
            getattr(settings, "v38_tradingeconomics_api_key", None),
            getattr(settings, "v38_tradingeconomics_base_url", "https://api.tradingeconomics.com"),
        )

    def assets(self) -> list[dict[str, Any]]:
        return [
            {
                **asdict(a),
                "providers": list(a.providers),
                "provider_symbols": dict(a.provider_symbols),
            }
            for a in CORE_ASSETS_V38 if a.enabled
        ]

    def _assets_for_class(self, cls: str):
        return [a for a in CORE_ASSETS_V38 if a.asset_class == cls]

    def _all_latest(self, asset_class: str = "ALL") -> list[dict[str, Any]]:
        with self.store._conn() as conn:
            rows = conn.execute("SELECT * FROM market_quotes_v37 ORDER BY ts_ms DESC").fetchall()
        specs = {a.asset_id: a for a in CORE_ASSETS_V38}
        result: list[dict[str, Any]] = []
        cls = asset_class.upper()
        for r in rows:
            spec = specs.get(r["asset_id"])
            if spec is None or (cls != "ALL" and spec.asset_class != cls):
                continue
            result.append({
                "asset_id": r["asset_id"],
                "asset_class": spec.asset_class,
                "symbol": spec.symbol,
                "display_name": spec.display_name,
                "tradingview_symbol": spec.tradingview_symbol,
                "provider": r["provider"],
                "price": r["price"],
                "bid": r["bid"],
                "ask": r["ask"],
                "change_24h_pct": r["change_24h_pct"],
                "volume_24h": r["volume_24h"],
                "market_cap": r["market_cap"],
                "ts_ms": r["ts_ms"],
                "quote_unit": getattr(spec, "quote_unit", "USD"),
            })
        return result

    async def _refresh_oanda_new_assets(self, stats: dict[str, Any]) -> None:
        if not getattr(self.settings, "v37_oanda_enabled", False):
            self.store.provider_snapshot("oanda_v38", "NOT_CONFIGURED")
            return
        specs = [a for a in MACRO_ASSETS_V38 if a.asset_class in {"INDEX", "ENERGY"}]
        started = time.perf_counter()
        successes = 0
        errors: list[str] = []
        for spec in specs:
            # OANDA instrument identifiers vary slightly across divisions; allow environment overrides and fallbacks.
            candidates = {
                "US30": [getattr(self.settings, "v38_oanda_us30_symbol", spec.provider_symbols["oanda"]), "US30_USD", "US30"],
                "SPX500": [getattr(self.settings, "v38_oanda_spx500_symbol", spec.provider_symbols["oanda"]), "SPX500_USD", "US500"],
                "US100": [getattr(self.settings, "v38_oanda_us100_symbol", spec.provider_symbols["oanda"]), "NAS100_USD", "US100"],
                "USOIL": [getattr(self.settings, "v38_oanda_usoil_symbol", spec.provider_symbols["oanda"]), "WTICO_USD", "USOIL", "OILWTI"],
                "UKOIL": [getattr(self.settings, "v38_oanda_ukoil_symbol", spec.provider_symbols["oanda"]), "BCO_USD", "UKOIL", "OILBRNT"],
            }[spec.symbol]
            fetched = False
            for instrument in list(dict.fromkeys(candidates)):
                try:
                    payload = await self.oanda.pricing([instrument])
                    prices = payload.get("prices", []) if isinstance(payload, dict) else []
                    p = prices[0] if prices else None
                    if not isinstance(p, dict):
                        continue
                    bid = float(((p.get("bids") or [{}])[0] or {}).get("price")) if p.get("bids") else None
                    ask = float(((p.get("asks") or [{}])[0] or {}).get("price")) if p.get("asks") else None
                    mid = ((bid + ask) / 2) if bid is not None and ask is not None else (bid or ask)
                    self.store.upsert(
                        spec.asset_id, "oanda", instrument, price=mid, bid=bid, ask=ask,
                        ts_ms=int(time.time() * 1000), raw={"instrument": instrument, "payload": p}
                    )
                    successes += 1
                    fetched = True
                    break
                except Exception as exc:
                    errors.append(f"{spec.symbol}:{instrument}:{exc}")
            if not fetched:
                continue
        status = "ONLINE" if successes else ("ERROR" if errors else "NOT_CONFIGURED")
        self.store.provider_snapshot("oanda_v38", status, int((time.perf_counter() - started) * 1000), "; ".join(errors[-3:]) if errors else None)
        if successes:
            stats["ok"].append("oanda-v38")
        if errors:
            stats["errors"].extend({"provider": "oanda-v38", "error": e} for e in errors[-5:])

    @staticmethod
    def _te_row(payload: Any) -> dict[str, Any] | None:
        if isinstance(payload, list):
            return payload[0] if payload and isinstance(payload[0], dict) else None
        if isinstance(payload, dict):
            for key in ("data", "markets", "results"):
                val = payload.get(key)
                if isinstance(val, list) and val and isinstance(val[0], dict):
                    return val[0]
            return payload if any(k in payload for k in ("close", "price", "last", "Rate")) else None
        return None

    async def _refresh_dxy(self, stats: dict[str, Any]) -> None:
        if not getattr(self.settings, "v38_tradingeconomics_enabled", False) or not self.tradingeconomics.configured:
            self.store.provider_snapshot("tradingeconomics_v38", "NOT_CONFIGURED")
            return
        started = time.perf_counter()
        try:
            payload = await self.tradingeconomics.market_quote("dxy:cur")
            row = self._te_row(payload)
            if not row:
                raise RuntimeError("Trading Economics returned no DXY quote")
            price = row.get("close", row.get("price", row.get("last", row.get("Rate"))))
            change_pct = row.get("percentChange", row.get("changePercent", row.get("pctChange")))
            price_f = float(price) if price not in (None, "") else None
            change_f = float(change_pct) if change_pct not in (None, "") else None
            self.store.upsert("macro.dxy", "tradingeconomics", "DXY:CUR", price=price_f, change_24h_pct=change_f, ts_ms=int(time.time()*1000), raw=row)
            self.store.provider_snapshot("tradingeconomics_v38", "ONLINE", int((time.perf_counter() - started) * 1000))
            stats["ok"].append("tradingeconomics-dxy")
        except Exception as exc:
            self.store.provider_snapshot("tradingeconomics_v38", "ERROR", int((time.perf_counter() - started) * 1000), str(exc))
            stats["errors"].append({"provider": "tradingeconomics-dxy", "error": str(exc)})

    async def _refresh_dominance(self, stats: dict[str, Any]) -> None:
        if not getattr(self.settings, "v37_cmc_enabled", False) or not getattr(self.settings, "v37_cmc_api_key", None):
            self.store.provider_snapshot("coinmarketcap_dominance_v38", "NOT_CONFIGURED")
            return
        started = time.perf_counter()
        try:
            headers = {"accept": "application/json", "X-CMC_PRO_API_KEY": self.settings.v37_cmc_api_key}
            payload = await self.transport.request_json(
                "coinmarketcap_v38_global", "GET",
                f"{self.settings.v37_cmc_base_url.rstrip('/')}/v1/global-metrics/quotes/latest",
                headers=headers, params={"convert": "USD"}, expected_types=(dict,)
            )
            data = payload.get("data") if isinstance(payload, dict) else None
            btc_dom = data.get("btc_dominance") if isinstance(data, dict) else None
            if btc_dom is not None:
                self.store.upsert(
                    "dominance.btc", "coinmarketcap", "BTC.D", price=float(btc_dom),
                    change_24h_pct=None, ts_ms=int(time.time()*1000),
                    raw={"btc_dominance": btc_dom, "source": "CoinMarketCap global metrics"}
                )
            listings = await self.cmc.listings(getattr(self.settings, "v37_cmc_limit", 200))
            usdt = None
            for r in listings.get("data", []) if isinstance(listings, dict) else []:
                if str(r.get("symbol", "")).upper() == "USDT":
                    usdt = r
                    break
            total_market_cap = data.get("total_market_cap") if isinstance(data, dict) else None
            usdt_market_cap = (((usdt or {}).get("quote") or {}).get("USD") or {}).get("market_cap")
            usdt_dom = None
            if usdt_market_cap not in (None, "") and total_market_cap not in (None, "") and float(total_market_cap) > 0:
                usdt_dom = float(usdt_market_cap) / float(total_market_cap) * 100.0
                self.store.upsert(
                    "dominance.usdt", "coinmarketcap", "USDT.D", price=usdt_dom,
                    change_24h_pct=None, ts_ms=int(time.time()*1000),
                    raw={"usdt_market_cap": usdt_market_cap, "total_market_cap": total_market_cap, "formula": "USDT market cap / total crypto market cap * 100"}
                )
            self.store.provider_snapshot("coinmarketcap_dominance_v38", "ONLINE", int((time.perf_counter() - started) * 1000))
            stats["ok"].append("coinmarketcap-dominance")
            if btc_dom is None:
                stats["errors"].append({"provider": "coinmarketcap-dominance", "error": "btc_dominance missing"})
            if usdt_dom is None:
                stats["errors"].append({"provider": "coinmarketcap-dominance", "error": "unable to derive USDT.D from CMC totals/listings"})
        except Exception as exc:
            self.store.provider_snapshot("coinmarketcap_dominance_v38", "ERROR", int((time.perf_counter() - started) * 1000), str(exc))
            stats["errors"].append({"provider": "coinmarketcap-dominance", "error": str(exc)})

    async def refresh(self, asset_class: str = "ALL") -> dict[str, Any]:
        cls = asset_class.upper()
        stats = {"ok": [], "errors": []}
        started = time.perf_counter()
        # Preserve the entire, tested v3.7 pipeline for existing assets.
        base_classes = {"ALL", "CRYPTO", "FOREX", "METALS"}
        if cls in base_classes:
            base = await super().refresh(cls)
            stats = base.get("stats", stats)
        if cls in {"ALL", "INDEX", "ENERGY"}:
            await self._refresh_oanda_new_assets(stats)
            if getattr(self.settings, "v37_fxcm_enabled", False):
                self.store.provider_snapshot("fxcm_v38", "CONFIGURED")
            else:
                self.store.provider_snapshot("fxcm_v38", "NOT_CONFIGURED")
        if cls in {"ALL", "MACRO"}:
            await self._refresh_dxy(stats)
        if cls in {"ALL", "DOMINANCE"}:
            await self._refresh_dominance(stats)
        return {"version": SCHEMA_VERSION, "asset_class": cls, "duration_ms": int((time.perf_counter() - started) * 1000), "stats": stats}

    def overview(self, asset_class: str = "ALL") -> dict[str, Any]:
        rows = self._all_latest(asset_class)
        latest_by_asset: dict[str, dict[str, Any]] = {}
        for r in rows:
            cur = latest_by_asset.get(r["asset_id"])
            if cur is None or r["ts_ms"] > cur["ts_ms"]:
                latest_by_asset[r["asset_id"]] = r
        cls = asset_class.upper()
        return {
            "version": SCHEMA_VERSION,
            "rows": list(latest_by_asset.values()),
            "crypto_universe_count": len(self.store.crypto_universe(2000)) if cls in {"ALL", "CRYPTO"} else 0,
            "providers": self.store.provider_status(),
            "assets": len([a for a in CORE_ASSETS_V38 if a.enabled and (cls == "ALL" or a.asset_class == cls)]),
        }

    def resolve(self, symbol: str) -> dict[str, Any] | None:
        key = symbol.upper().replace(" ", "")
        spec = ASSET_INDEX_V38.get(key) or ASSET_ALIAS_INDEX_V38.get(key)
        return asdict(spec) if spec else None

    def status(self) -> dict[str, Any]:
        counts = {}
        for a in CORE_ASSETS_V38:
            counts[a.asset_class] = counts.get(a.asset_class, 0) + 1
        return {
            "version": "3.8.0",
            "assets": len(CORE_ASSETS_V38),
            "asset_classes": counts,
            "providers": self.store.provider_status(),
            "dxy_provider": {"configured": self.tradingeconomics.configured, "provider": "Trading Economics"},
            "oanda_extended": bool(getattr(self.settings, "v37_oanda_enabled", False)),
        }
