from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import quote

from app.resilience_v25 import GLOBAL_PROVIDER_HEALTH_V25, ResilientHTTPTransportV25


SCHEMA_VERSION = "market_data_v37"


@dataclass(frozen=True)
class AssetSpecV37:
    asset_id: str
    asset_class: str
    symbol: str
    display_name: str
    tradingview_symbol: str
    providers: tuple[str, ...]
    provider_symbols: dict[str, str]
    sector: str
    enabled: bool = True


CORE_ASSETS: tuple[AssetSpecV37, ...] = (
    AssetSpecV37("crypto.btcusdt", "CRYPTO", "BTCUSDT", "Bitcoin / Tether", "BINANCE:BTCUSDT", ("binance", "coinmarketcap", "bybit", "okx", "coinbase", "kraken"), {"binance": "BTCUSDT", "coinmarketcap": "BTC", "bybit": "BTCUSDT", "okx": "BTC-USDT", "coinbase": "BTC-USD", "kraken": "XBTUSDT"}, "Layer 1"),
    AssetSpecV37("crypto.ethusdt", "CRYPTO", "ETHUSDT", "Ethereum / Tether", "BINANCE:ETHUSDT", ("binance", "coinmarketcap", "bybit", "okx", "coinbase", "kraken"), {"binance": "ETHUSDT", "coinmarketcap": "ETH", "bybit": "ETHUSDT", "okx": "ETH-USDT", "coinbase": "ETH-USD", "kraken": "ETHUSDT"}, "Layer 1"),
    AssetSpecV37("crypto.bnbusdt", "CRYPTO", "BNB / Tether", "BNBUSDT", "BINANCE:BNBUSDT", ("binance", "coinmarketcap", "bybit", "okx"), {"binance": "BNBUSDT", "coinmarketcap": "BNB", "bybit": "BNBUSDT", "okx": "BNB-USDT"}, "Layer 1"),
    AssetSpecV37("crypto.solusdt", "CRYPTO", "SOLUSDT", "Solana / Tether", "BINANCE:SOLUSDT", ("binance", "coinmarketcap", "bybit", "okx"), {"binance": "SOLUSDT", "coinmarketcap": "SOL", "bybit": "SOLUSDT", "okx": "SOL-USDT"}, "Layer 1"),
    AssetSpecV37("crypto.xrpusdt", "CRYPTO", "XRP / Tether", "XRPUSDT", "BINANCE:XRPUSDT", ("binance", "coinmarketcap", "bybit", "okx"), {"binance": "XRPUSDT", "coinmarketcap": "XRP", "bybit": "XRPUSDT", "okx": "XRP-USDT"}, "Payments"),
    AssetSpecV37("crypto.adausdt", "CRYPTO", "Cardano / Tether", "ADAUSDT", "BINANCE:ADAUSDT", ("binance", "coinmarketcap", "bybit", "okx"), {"binance": "ADAUSDT", "coinmarketcap": "ADA", "bybit": "ADAUSDT", "okx": "ADA-USDT"}, "Layer 1"),
    AssetSpecV37("crypto.dogeusdt", "CRYPTO", "Dogecoin / Tether", "DOGEUSDT", "BINANCE:DOGEUSDT", ("binance", "coinmarketcap", "bybit", "okx"), {"binance": "DOGEUSDT", "coinmarketcap": "DOGE", "bybit": "DOGEUSDT", "okx": "DOGE-USDT"}, "Meme"),
    AssetSpecV37("crypto.avaxusdt", "CRYPTO", "Avalanche / Tether", "AVAXUSDT", "BINANCE:AVAXUSDT", ("binance", "coinmarketcap", "bybit", "okx"), {"binance": "AVAXUSDT", "coinmarketcap": "AVAX", "bybit": "AVAXUSDT", "okx": "AVAX-USDT"}, "Layer 1"),
    AssetSpecV37("crypto.linkusdt", "CRYPTO", "Chainlink / Tether", "LINKUSDT", "BINANCE:LINKUSDT", ("binance", "coinmarketcap", "bybit", "okx"), {"binance": "LINKUSDT", "coinmarketcap": "LINK", "bybit": "LINKUSDT", "okx": "LINK-USDT"}, "Oracle"),
    AssetSpecV37("crypto.shibusdt", "CRYPTO", "Shiba Inu / Tether", "SHIBUSDT", "BINANCE:SHIBUSDT", ("binance", "coinmarketcap", "bybit", "okx"), {"binance": "SHIBUSDT", "coinmarketcap": "SHIB", "bybit": "SHIBUSDT", "okx": "SHIB-USDT"}, "Meme"),
    AssetSpecV37("fx.eurusd", "FOREX", "EURUSD", "Euro / U.S. Dollar", "OANDA:EURUSD", ("oanda", "fxcm"), {"oanda": "EUR_USD", "fxcm": "EUR/USD"}, "G10"),
    AssetSpecV37("fx.gbpusd", "FOREX", "GBPUSD", "British Pound / U.S. Dollar", "OANDA:GBPUSD", ("oanda", "fxcm"), {"oanda": "GBP_USD", "fxcm": "GBP/USD"}, "G10"),
    AssetSpecV37("fx.usdchf", "FOREX", "USDCHF", "U.S. Dollar / Swiss Franc", "OANDA:USDCHF", ("oanda", "fxcm"), {"oanda": "USD_CHF", "fxcm": "USD/CHF"}, "G10"),
    AssetSpecV37("fx.usdcad", "FOREX", "USDCAD", "U.S. Dollar / Canadian Dollar", "OANDA:USDCAD", ("oanda", "fxcm"), {"oanda": "USD_CAD", "fxcm": "USD/CAD"}, "G10"),
    AssetSpecV37("fx.audusd", "FOREX", "AUDUSD", "Australian Dollar / U.S. Dollar", "OANDA:AUDUSD", ("oanda", "fxcm"), {"oanda": "AUD_USD", "fxcm": "AUD/USD"}, "G10"),
    AssetSpecV37("fx.nzdusd", "FOREX", "NZDUSD", "New Zealand Dollar / U.S. Dollar", "OANDA:NZDUSD", ("oanda", "fxcm"), {"oanda": "NZD_USD", "fxcm": "NZD/USD"}, "G10"),
    AssetSpecV37("fx.usdjpy", "FOREX", "USDJPY", "U.S. Dollar / Japanese Yen", "OANDA:USDJPY", ("oanda", "fxcm"), {"oanda": "USD_JPY", "fxcm": "USD/JPY"}, "G10"),
    AssetSpecV37("metal.xauusd", "METALS", "XAUUSD", "Gold Spot / U.S. Dollar", "OANDA:XAUUSD", ("oanda", "fxcm"), {"oanda": "XAU_USD", "fxcm": "XAU/USD"}, "Precious Metals"),
    AssetSpecV37("metal.xagusd", "METALS", "XAGUSD", "Silver Spot / U.S. Dollar", "OANDA:XAGUSD", ("oanda", "fxcm"), {"oanda": "XAG_USD", "fxcm": "XAG/USD"}, "Precious Metals"),
    AssetSpecV37("metal.xcuusd", "METALS", "XCUUSD", "Copper / U.S. Dollar", "OANDA:XCUUSD", ("oanda", "fxcm"), {"oanda": "XCU_USD", "fxcm": "Copper"}, "Industrial Metals"),
)

ASSET_INDEX = {a.symbol: a for a in CORE_ASSETS}


class MarketDataStoreV37:
    def __init__(self, sqlite_path: str):
        self.sqlite_path = sqlite_path
        self._init()

    def _conn(self):
        conn = sqlite3.connect(self.sqlite_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self):
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS market_quotes_v37 (
                asset_id TEXT NOT NULL, provider TEXT NOT NULL, symbol TEXT NOT NULL,
                price REAL, bid REAL, ask REAL, change_24h_pct REAL, volume_24h REAL,
                market_cap REAL, ts_ms INTEGER NOT NULL, raw_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY(asset_id, provider)
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS market_provider_snapshots_v37 (
                provider TEXT PRIMARY KEY, status TEXT NOT NULL, ts_ms INTEGER NOT NULL,
                latency_ms INTEGER, error TEXT
            )""")
            conn.execute("""CREATE TABLE IF NOT EXISTS market_crypto_universe_v37 (
                symbol TEXT PRIMARY KEY, price REAL, change_24h_pct REAL, volume_24h REAL,
                bid REAL, ask REAL, ts_ms INTEGER NOT NULL, tradingview_symbol TEXT NOT NULL, raw_json TEXT NOT NULL DEFAULT '{}'
            )""")
            conn.execute("INSERT OR REPLACE INTO market_provider_snapshots_v37(provider,status,ts_ms,latency_ms,error) VALUES(?,?,?,?,?)", ("__schema__", SCHEMA_VERSION, int(time.time()*1000), 0, None))

    def upsert(self, asset_id: str, provider: str, symbol: str, **kwargs: Any):
        row = {
            "price": kwargs.get("price"), "bid": kwargs.get("bid"), "ask": kwargs.get("ask"),
            "change_24h_pct": kwargs.get("change_24h_pct"), "volume_24h": kwargs.get("volume_24h"),
            "market_cap": kwargs.get("market_cap"), "ts_ms": int(kwargs.get("ts_ms") or time.time()*1000),
            "raw_json": json.dumps(kwargs.get("raw") or {}, separators=(",", ":"), ensure_ascii=False, default=str),
        }
        with self._conn() as conn:
            conn.execute("""INSERT INTO market_quotes_v37(asset_id,provider,symbol,price,bid,ask,change_24h_pct,volume_24h,market_cap,ts_ms,raw_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(asset_id,provider) DO UPDATE SET symbol=excluded.symbol,price=excluded.price,bid=excluded.bid,ask=excluded.ask,
                change_24h_pct=excluded.change_24h_pct,volume_24h=excluded.volume_24h,market_cap=excluded.market_cap,ts_ms=excluded.ts_ms,raw_json=excluded.raw_json""",
                (asset_id, provider, symbol, row["price"], row["bid"], row["ask"], row["change_24h_pct"], row["volume_24h"], row["market_cap"], row["ts_ms"], row["raw_json"]))

    def upsert_crypto_universe(self, row: dict[str, Any]):
        symbol=str(row.get("symbol", "")).upper()
        if not symbol or not symbol.endswith("USDT"): return
        with self._conn() as conn:
            conn.execute("""INSERT INTO market_crypto_universe_v37(symbol,price,change_24h_pct,volume_24h,bid,ask,ts_ms,tradingview_symbol,raw_json)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET price=excluded.price,change_24h_pct=excluded.change_24h_pct,volume_24h=excluded.volume_24h,bid=excluded.bid,ask=excluded.ask,ts_ms=excluded.ts_ms,raw_json=excluded.raw_json""",
                (symbol, float(row["lastPrice"]) if row.get("lastPrice") else None, float(row["priceChangePercent"]) if row.get("priceChangePercent") else None, float(row["quoteVolume"]) if row.get("quoteVolume") else None, None, None, int(time.time()*1000), f"BINANCE:{symbol}", json.dumps(row,separators=(",",":"),default=str)))

    def crypto_universe(self, limit: int = 200):
        with self._conn() as conn:
            rows=conn.execute("SELECT * FROM market_crypto_universe_v37 ORDER BY volume_24h DESC LIMIT ?",(max(1,min(int(limit),2000)),)).fetchall()
        return [dict(r) for r in rows]

    def provider_snapshot(self, provider: str, status: str, latency_ms: int | None = None, error: str | None = None):
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO market_provider_snapshots_v37(provider,status,ts_ms,latency_ms,error) VALUES(?,?,?,?,?)", (provider, status, int(time.time()*1000), latency_ms, error))

    def latest(self, asset_class: str = "ALL") -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM market_quotes_v37 ORDER BY ts_ms DESC").fetchall()
        result=[]
        for r in rows:
            spec = next((a for a in CORE_ASSETS if a.asset_id==r["asset_id"]), None)
            if not spec or (asset_class != "ALL" and spec.asset_class != asset_class.upper()):
                continue
            result.append({"asset_id":r["asset_id"],"asset_class":spec.asset_class,"symbol":spec.symbol,"display_name":spec.display_name,
                           "tradingview_symbol":spec.tradingview_symbol,"provider":r["provider"],"price":r["price"],"bid":r["bid"],"ask":r["ask"],
                           "change_24h_pct":r["change_24h_pct"],"volume_24h":r["volume_24h"],"market_cap":r["market_cap"],"ts_ms":r["ts_ms"]})
        return result

    def provider_status(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows=conn.execute("SELECT * FROM market_provider_snapshots_v37 WHERE provider != '__schema__' ORDER BY provider").fetchall()
        return [dict(r) for r in rows]


class BinancePublicV37:
    def __init__(self, transport: ResilientHTTPTransportV25, base_url: str = "https://api.binance.com"):
        self.transport=transport; self.base=base_url.rstrip("/")

    async def all_tickers(self):
        return await self.transport.request_json("binance_spot_v37", "GET", f"{self.base}/api/v3/ticker/24hr", expected_types=(list,))

    async def book_ticker(self, symbol: str):
        return await self.transport.request_json("binance_spot_v37", "GET", f"{self.base}/api/v3/ticker/bookTicker", params={"symbol":symbol}, expected_types=(dict,))


class CoinMarketCapV37:
    def __init__(self, transport: ResilientHTTPTransportV25, api_key: str | None = None, base_url: str = "https://pro-api.coinmarketcap.com"):
        self.transport=transport; self.api_key=api_key; self.base=base_url.rstrip("/")

    async def listings(self, limit: int = 200):
        headers={"accept":"application/json"}
        path=f"{self.base}/v3/cryptocurrency/listings/latest"
        if not self.api_key:
            path=f"{self.base}/public-api/v3/cryptocurrency/listings/latest"
        else:
            headers["X-CMC_PRO_API_KEY"]=self.api_key
        return await self.transport.request_json("coinmarketcap_v37", "GET", path, headers=headers, params={"start":1,"limit":max(1,min(limit,500)),"convert":"USD"}, expected_types=(dict,))


class PublicCryptoExchangeV37:
    def __init__(self, transport: ResilientHTTPTransportV25):
        self.transport=transport

    async def bybit(self, symbol: str):
        return await self.transport.request_json("bybit_v37", "GET", "https://api.bybit.com/v5/market/tickers", params={"category":"spot","symbol":symbol}, expected_types=(dict,))

    async def okx(self, inst_id: str):
        return await self.transport.request_json("okx_v37", "GET", "https://www.okx.com/api/v5/market/ticker", params={"instId":inst_id}, expected_types=(dict,))

    async def coinbase(self, product_id: str):
        return await self.transport.request_json("coinbase_v37", "GET", f"https://api.exchange.coinbase.com/products/{quote(product_id,safe='')}/ticker", expected_types=(dict,))

    async def kraken(self, pair: str):
        return await self.transport.request_json("kraken_v37", "GET", "https://api.kraken.com/0/public/Ticker", params={"pair":pair}, expected_types=(dict,))


class OandaV37:
    def __init__(self, transport: ResilientHTTPTransportV25, token: str | None, account_id: str | None, base_url: str = "https://api-fxtrade.oanda.com"):
        self.transport=transport; self.token=token; self.account_id=account_id; self.base=base_url.rstrip("/")

    @property
    def configured(self): return bool(self.token and self.account_id)

    async def pricing(self, instruments: list[str]):
        if not self.configured:
            raise RuntimeError("OANDA credentials not configured")
        return await self.transport.request_json("oanda_v37", "GET", f"{self.base}/v3/accounts/{self.account_id}/pricing", headers={"Authorization":f"Bearer {self.token}","Accept-Datetime-Format":"RFC3339"}, params={"instruments":",".join(instruments)}, expected_types=(dict,))


class FXCMInfoV37:
    def __init__(self, configured: bool, base_url: str):
        self.configured=configured; self.base_url=base_url

    def status(self):
        return {"configured":self.configured,"integration":"ForexConnect / FCLite gateway","base_url":self.base_url,
                "note":"FXCM exposes live/historical market data via FIX, Java and ForexConnect APIs; this adapter intentionally does not invent undocumented REST quote paths."}


class ArkhamV37:
    def __init__(self, transport: ResilientHTTPTransportV25, api_key: str | None, base_url: str = "https://api.arkm.com"):
        self.transport=transport; self.api_key=api_key; self.base=base_url.rstrip("/")

    @property
    def configured(self): return bool(self.api_key)

    async def transfers(self, limit: int = 20):
        if not self.configured: raise RuntimeError("Arkham API key not configured")
        return await self.transport.request_json("arkham_v37", "GET", f"{self.base}/transfers", headers={"API-Key":self.api_key,"accept":"application/json"}, params={"sortKey":"usd","sortDir":"desc","limit":max(1,min(limit,100))}, expected_types=(dict,))

    async def address_updates(self, limit: int = 50):
        if not self.configured: raise RuntimeError("Arkham API key not configured")
        return await self.transport.request_json("arkham_v37", "GET", f"{self.base}/intelligence/addresses/updates", headers={"API-Key":self.api_key,"accept":"application/json"}, params={"limit":max(1,min(limit,100))}, expected_types=(dict,))


class EtherscanV37:
    def __init__(self, transport: ResilientHTTPTransportV25, api_key: str | None, base_url: str = "https://api.etherscan.io/v2/api"):
        self.transport=transport; self.api_key=api_key; self.base=base_url

    @property
    def configured(self): return bool(self.api_key)

    async def latest_block(self, chain_id: int):
        if not self.configured: raise RuntimeError("Etherscan API key not configured")
        return await self.transport.request_json("etherscan_v37", "GET", self.base, params={"chainid":chain_id,"module":"proxy","action":"eth_blockNumber","apikey":self.api_key}, expected_types=(dict,))

    async def gas(self, chain_id: int):
        if not self.configured: raise RuntimeError("Etherscan API key not configured")
        return await self.transport.request_json("etherscan_v37", "GET", self.base, params={"chainid":chain_id,"module":"gastracker","action":"gasoracle","apikey":self.api_key}, expected_types=(dict,))


class MarketDataFederationV37:
    def __init__(self, sqlite_path: str, settings: Any):
        self.store=MarketDataStoreV37(sqlite_path)
        self.settings=settings
        self.transport=ResilientHTTPTransportV25(GLOBAL_PROVIDER_HEALTH_V25)
        self.binance=BinancePublicV37(self.transport, settings.v37_binance_spot_base_url)
        self.cmc=CoinMarketCapV37(self.transport, settings.v37_cmc_api_key, settings.v37_cmc_base_url)
        self.exchanges=PublicCryptoExchangeV37(self.transport)
        self.oanda=OandaV37(self.transport, settings.v37_oanda_api_token, settings.v37_oanda_account_id, settings.v37_oanda_base_url)
        self.fxcm=FXCMInfoV37(bool(settings.v37_fxcm_enabled), settings.v37_fxcm_base_url)
        self.arkham=ArkhamV37(self.transport, settings.v37_arkham_api_key, settings.v37_arkham_base_url)
        self.etherscan=EtherscanV37(self.transport, settings.v37_etherscan_api_key, settings.v37_etherscan_base_url)

    def assets(self) -> list[dict[str, Any]]:
        return [{**asdict(a), "providers":list(a.providers), "provider_symbols":dict(a.provider_symbols)} for a in CORE_ASSETS if a.enabled]

    def _assets_for_class(self, cls: str):
        return [a for a in CORE_ASSETS if a.asset_class==cls]

    async def refresh(self, asset_class: str = "ALL") -> dict[str, Any]:
        asset_class=asset_class.upper()
        stats={"ok":[],"errors":[]}
        started=time.perf_counter()

        if asset_class in {"ALL","CRYPTO"} and self.settings.v37_binance_enabled:
            t=time.perf_counter()
            try:
                rows=await self.binance.all_tickers()
                by_symbol={str(r.get("symbol","")):r for r in rows if isinstance(r,dict)}
                for raw_row in rows if isinstance(rows,list) else []:
                    if isinstance(raw_row,dict) and str(raw_row.get("symbol","" )).endswith("USDT"):
                        self.store.upsert_crypto_universe(raw_row)
                for spec in self._assets_for_class("CRYPTO"):
                    r=by_symbol.get(spec.provider_symbols["binance"])
                    if not r: continue
                    self.store.upsert(spec.asset_id,"binance",r.get("symbol",""),price=float(r.get("lastPrice")) if r.get("lastPrice") else None,
                                      change_24h_pct=float(r.get("priceChangePercent")) if r.get("priceChangePercent") else None,
                                      volume_24h=float(r.get("quoteVolume")) if r.get("quoteVolume") else None,ts_ms=int(time.time()*1000),raw=r)
                latency=int((time.perf_counter()-t)*1000); self.store.provider_snapshot("binance", "ONLINE", latency); stats["ok"].append("binance")
            except Exception as exc:
                self.store.provider_snapshot("binance", "ERROR", int((time.perf_counter()-t)*1000), str(exc)); stats["errors"].append({"provider":"binance","error":str(exc)})

        if asset_class in {"ALL","CRYPTO"} and self.settings.v37_cmc_enabled:
            t=time.perf_counter()
            try:
                payload=await self.cmc.listings(self.settings.v37_cmc_limit)
                for r in payload.get("data",[]) if isinstance(payload,dict) else []:
                    sym=str(r.get("symbol","" )).upper()
                    spec=next((a for a in self._assets_for_class("CRYPTO") if a.provider_symbols.get("coinmarketcap")==sym),None)
                    if not spec: continue
                    q=((r.get("quote") or {}).get("USD") or {})
                    self.store.upsert(spec.asset_id,"coinmarketcap",sym,price=q.get("price"),change_24h_pct=q.get("percent_change_24h"),volume_24h=q.get("volume_24h"),market_cap=q.get("market_cap"),ts_ms=int(time.time()*1000),raw=r)
                latency=int((time.perf_counter()-t)*1000); self.store.provider_snapshot("coinmarketcap", "ONLINE", latency); stats["ok"].append("coinmarketcap")
            except Exception as exc:
                self.store.provider_snapshot("coinmarketcap", "ERROR", int((time.perf_counter()-t)*1000), str(exc)); stats["errors"].append({"provider":"coinmarketcap","error":str(exc)})

        if asset_class in {"ALL","CRYPTO"} and self.settings.v37_exchange_fanout_enabled:
            major=[a for a in self._assets_for_class("CRYPTO") if a.symbol in {"BTCUSDT","ETHUSDT","BNBUSDT","SOLUSDT"}]
            async def fetch_ex(spec: AssetSpecV37):
                out=[]
                try:
                    r=await self.exchanges.bybit(spec.provider_symbols["bybit"]); rr=(r.get("result") or {}).get("list") or []; row=rr[0] if rr else {}
                    if row: self.store.upsert(spec.asset_id,"bybit",spec.provider_symbols["bybit"],price=row.get("lastPrice"),change_24h_pct=row.get("price24hPcnt"),volume_24h=row.get("turnover24h"),ts_ms=int(time.time()*1000),raw=row); out.append("bybit")
                except Exception as exc: out.append({"bybit":str(exc)})
                try:
                    r=await self.exchanges.okx(spec.provider_symbols["okx"]); row=(r.get("data") or [None])[0] or {}
                    if row: self.store.upsert(spec.asset_id,"okx",spec.provider_symbols["okx"],price=row.get("last"),change_24h_pct=((float(row.get("last"))-float(row.get("sod")))/float(row.get("sod"))*100 if row.get("sod") else None),volume_24h=row.get("volCcy24h"),ts_ms=int(time.time()*1000),raw=row); out.append("okx")
                except Exception as exc: out.append({"okx":str(exc)})
                try:
                    r=await self.exchanges.kraken(spec.provider_symbols["kraken"]); row=next(iter((r.get("result") or {}).values()),{}) if isinstance(r,dict) else {}
                    if row: self.store.upsert(spec.asset_id,"kraken",spec.provider_symbols["kraken"],price=(float(row.get("c",[0])[0]) if row.get("c") else None),change_24h_pct=None,volume_24h=(float(row.get("v",[0,0])[1]) if row.get("v") else None),ts_ms=int(time.time()*1000),raw=row); out.append("kraken")
                except Exception as exc: out.append({"kraken":str(exc)})
                try:
                    r=await self.exchanges.coinbase(spec.provider_symbols["coinbase"]); row=r if isinstance(r,dict) else {}
                    if row: self.store.upsert(spec.asset_id,"coinbase",spec.provider_symbols["coinbase"],price=row.get("price"),ts_ms=int(time.time()*1000),raw=row); out.append("coinbase")
                except Exception as exc: out.append({"coinbase":str(exc)})
                return out
            ex_results=await asyncio.gather(*(fetch_ex(s) for s in major))
            stats["ok"].append("exchange-fanout")

        if asset_class in {"ALL","FOREX","METALS"} and self.settings.v37_oanda_enabled:
            t=time.perf_counter()
            try:
                specs=[a for a in CORE_ASSETS if a.asset_class in {"FOREX","METALS"}]
                payload=await self.oanda.pricing([a.provider_symbols["oanda"] for a in specs])
                by_inst={str(p.get("instrument")):p for p in payload.get("prices",[]) if isinstance(payload,dict)}
                for spec in specs:
                    p=by_inst.get(spec.provider_symbols["oanda"])
                    if not p: continue
                    bid=float(((p.get("bids") or [{}])[0] or {}).get("price")) if p.get("bids") else None
                    ask=float(((p.get("asks") or [{}])[0] or {}).get("price")) if p.get("asks") else None
                    mid=((bid+ask)/2) if bid is not None and ask is not None else (bid or ask)
                    self.store.upsert(spec.asset_id,"oanda",spec.provider_symbols["oanda"],price=mid,bid=bid,ask=ask,ts_ms=int(time.time()*1000),raw=p)
                latency=int((time.perf_counter()-t)*1000); self.store.provider_snapshot("oanda", "ONLINE", latency); stats["ok"].append("oanda")
            except Exception as exc:
                self.store.provider_snapshot("oanda", "ERROR", int((time.perf_counter()-t)*1000), str(exc)); stats["errors"].append({"provider":"oanda","error":str(exc)})
        elif asset_class in {"ALL","FOREX","METALS"}:
            self.store.provider_snapshot("oanda", "NOT_CONFIGURED")

        if asset_class in {"ALL","FOREX","METALS"}:
            self.store.provider_snapshot("fxcm", "CONFIGURED" if self.fxcm.configured else "NOT_CONFIGURED")
        return {"version":SCHEMA_VERSION,"asset_class":asset_class,"duration_ms":int((time.perf_counter()-started)*1000),"stats":stats}

    def overview(self, asset_class: str="ALL"):
        rows=self.store.latest(asset_class)
        latest_by_asset={}
        for r in rows:
            current=latest_by_asset.get(r["asset_id"])
            if current is None or r["ts_ms"]>current["ts_ms"]: latest_by_asset[r["asset_id"]]=r
        return {"version":SCHEMA_VERSION,"rows":list(latest_by_asset.values()),"crypto_universe_count":len(self.store.crypto_universe(2000)) if asset_class in {"ALL","CRYPTO"} else 0,"providers":self.store.provider_status(),"assets":len(latest_by_asset)}

    async def onchain_live(self) -> dict[str, Any]:
        result: dict[str, Any] = {"version":SCHEMA_VERSION,"sources":{}}
        if self.settings.v24_coinglass_api_key:
            try:
                from app.collectors.coinglass_v24 import CoinGlassClientV24
                cg=CoinGlassClientV24(self.settings.v24_coinglass_api_key, self.settings.v24_coinglass_base_url, transport=self.transport)
                result["sources"]["coinglass"]={"ok":True,"liquidations":await cg.liquidation_orders()}
            except Exception as exc: result["sources"]["coinglass"]={"ok":False,"error":str(exc)}
        if self.arkham.configured:
            try: result["sources"]["arkham"]={"ok":True,"transfers":await self.arkham.transfers(25)}
            except Exception as exc: result["sources"]["arkham"]={"ok":False,"error":str(exc)}
        if self.etherscan.configured:
            try:
                result["sources"]["etherscan"]={"ok":True,"ethereum":{"block":await self.etherscan.latest_block(1),"gas":await self.etherscan.gas(1)},"bsc":{"block":await self.etherscan.latest_block(56),"gas":await self.etherscan.gas(56)}}
            except Exception as exc: result["sources"]["etherscan"]={"ok":False,"error":str(exc)}
        return result

    async def onchain_overview(self) -> dict[str, Any]:
        out={"coinglass":{"configured":bool(self.settings.v24_coinglass_api_key)},"arkham":{"configured":self.arkham.configured},"etherscan":{"configured":self.etherscan.configured,"chains":{"ethereum":1,"bsc":56}},"goplus":{"configured":bool(self.settings.v24_goplus_enabled)}}
        return out

