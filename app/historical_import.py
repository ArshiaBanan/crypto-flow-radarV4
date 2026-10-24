from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from datetime import datetime, timezone

from app.collectors.binance_history import BinanceHistoricalClient, ImportStats, funding_to_sample, kline_to_bar
from app.storage.sqlite import SQLiteStore


def parse_time(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raw = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)


async def import_symbol(db: SQLiteStore, client: BinanceHistoricalClient, symbol: str, start_ms: int, end_ms: int, interval: str, oi_period: str | None, sleep_seconds: float = 0.0) -> dict:
    if interval != "1m":
        raise ValueError("v0.4 uses 1m as the canonical historical series; derive 5m/15m/1h from 1m instead of mixing timeframes in bars_1m")
    rows = await client.klines(symbol, interval, start_ms, end_ms, sleep_seconds=sleep_seconds)
    stats = ImportStats(symbol=symbol.upper(), interval=interval, start_ms=start_ms, end_ms=end_ms, rows=len(rows), pages=1 if rows else 0)
    for raw in rows:
        bar = kline_to_bar(raw)
        bar["symbol"] = symbol.lower()
        db.upsert_bar(bar)
        db.write_raw(int(raw[0]), "binance_historical", symbol.lower(), "kline", {"interval": interval, "row": raw})

    if interval == "1m":
        funding = await client.funding_history(symbol, start_ms, end_ms, sleep_seconds=sleep_seconds)
        for row in funding:
            ts_ms, rate, mark_price, rate_type = funding_to_sample(row)
            db.write_funding(symbol.lower(), ts_ms, rate, mark_price, rate_type, "binance_historical")
            db.write_raw(ts_ms, "binance_historical", symbol.lower(), "fundingRate", row)
        if oi_period:
            oi_rows = await client.open_interest_history(symbol, oi_period, start_ms, end_ms, sleep_seconds=sleep_seconds)
            for row in oi_rows:
                # sumOpenInterest is the OI quantity used by the existing feature engine.
                db.write_oi(symbol.lower(), int(row["timestamp"]), float(row["sumOpenInterest"]))
                db.write_raw(int(row["timestamp"]), "binance_historical", symbol.lower(), "openInterestHist", row)
        stats.rows += 0
    return asdict(stats) | {"funding_rows": len(funding) if interval == "1m" else 0, "oi_rows": len(oi_rows) if interval == "1m" and oi_period else 0}


async def main_async(args: argparse.Namespace) -> None:
    db = SQLiteStore(args.db)
    client = BinanceHistoricalClient(args.rest_url)
    start_ms, end_ms = parse_time(args.start), parse_time(args.end)
    reports = []
    for symbol in args.symbols:
        reports.append(await import_symbol(db, client, symbol, start_ms, end_ms, args.interval, args.oi_period, args.sleep))
        quality = db.bar_coverage(symbol.lower(), start_ms, end_ms, interval_ms=60_000)
        db.record_quality_run(quality)
        reports[-1]["quality"] = quality
    print(json.dumps(reports, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description="Import Binance USDⓈ-M Futures historical market data")
    p.add_argument("--db", default="data/radar.db")
    p.add_argument("--symbols", nargs="+", required=True, help="e.g. BTCUSDT ETHUSDT SOLUSDT")
    p.add_argument("--start", required=True, help="epoch ms or ISO-8601 UTC")
    p.add_argument("--end", required=True, help="epoch ms or ISO-8601 UTC")
    p.add_argument("--interval", default="1m", choices=["1m"], help="canonical storage interval; higher windows are derived from 1m")
    p.add_argument("--oi-period", default="5m", choices=["5m","15m","30m","1h","2h","4h","6h","12h","1d"], help="historical OI sampling period; only used with 1m import")
    p.add_argument("--sleep", type=float, default=0.0, help="pause between paginated API requests")
    p.add_argument("--rest-url", default="https://fapi.binance.com")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
