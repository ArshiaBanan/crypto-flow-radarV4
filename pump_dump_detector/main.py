"""
Pump/Dump Detector — Phase 1 through Phase 7

Scans all liquid USDT pairs on Binance for:
  - abnormal volume spikes (spot)
  - order book imbalance (spot)
  - open interest changes (USDT-M futures, where available)
  - extreme funding rates (USDT-M futures, where available)
  - independent cross-checks from LiveCoinWatch (BTC-volume-band movers)
    and CryptoMeter (rapid PUMP/DUMP flags)
  - on-chain whale flow through known exchange hot wallets (Etherscan API
    V2, Ethereum + BSC)
  - social attention: CoinGecko trending searches, Reddit mention-velocity
    spikes, and mentions in Telegram channels you choose to monitor
and combines them into a composite "money flow" score, fully automatically.

Phase 7 adds a precision-filter LAYER on top of that same score (it never
changes the score itself, so backtests stay comparable across phases):
  - wash-trading guard: flags a volume spike with no matching price move
  - cross-exchange confirmation: for candidates that clear
    CONFIRMATION_CANDIDATE_MIN_SCORE, checks Bybit/OKX for the same
    directional move before spending the extra API calls on the rest
  - a confluence tier (CONFIRMED / WATCH / LOW_CONFIDENCE) requiring the
    above plus agreement across independent signal FAMILIES, not just a
    high weighted average — see signal_engine.classify_confluence
  - a market-wide funding-rate leverage bias, aggregated once per scan at
    zero extra API cost, as a free substitute for paid liquidation feeds

Every scan is persisted to SQLite (storage.py) — open interest and
Reddit-mention history survive restarts instead of living in memory, and
every symbol's score/signals/confluence are logged so backtest.py (and the
Phase 6 dashboard) can answer "when the score was high, what actually
happened next?"

Run:
    python main.py              # single scan, prints table + saves CSV + DB
    python main.py --loop       # repeats every SCAN_INTERVAL_MINUTES, forever
    python backtest.py          # after some history has built up — see backtest.py
    python dashboard.py         # alongside --loop — see dashboard.py
"""

import sys
import time
import csv
from datetime import datetime, timezone

import config
import data_fetcher as df
import signal_engine as se
import livecoinwatch_source as lcw
import cryptometer_source as cm
import onchain_source as onchain
import social_source as social
import storage
from telegram_alert import send_telegram_alert


def scan_once():
    now = datetime.now(timezone.utc)
    print(f"\n=== Scan started {now.isoformat()} ===")
    scan_id = storage.start_scan(now.isoformat(), int(now.timestamp()))

    spot = df.get_spot_exchange()
    futures = df.get_futures_exchange()
    df.load_futures_markets(futures)

    symbols, price_map = df.get_usdt_symbols_and_prices(spot)
    print(f"Scanning {len(symbols)} liquid USDT pairs...")

    # --- External cross-check sources (fetched once, not per-symbol) ---
    print("Fetching LiveCoinWatch BTC-volume-band movers...")
    lcw_gainers, lcw_losers = lcw.get_btc_volume_band_movers()
    print(f"  {len(lcw_gainers)} gainers / {len(lcw_losers)} losers in the "
          f"{config.LCW_MIN_VOLUME_BTC}-{config.LCW_MAX_VOLUME_BTC} BTC volume band")

    print("Fetching CryptoMeter rapid movements + market trend...")
    rapid_movements = cm.get_rapid_movements()
    rapid_by_symbol = {}
    for rm in rapid_movements:
        base = rm.get("pair", "").split("-")[0].upper()
        if base:
            rapid_by_symbol[base] = rm
    market_trend = cm.get_market_trend()
    if market_trend:
        print(f"  Market trend: {market_trend.get('trend_score')}/100 "
              f"(buy {market_trend.get('buy_pressure')}% / sell {market_trend.get('sell_pressure')}%)")

    known_symbols = {s.split("/")[0] for s in symbols}

    print("Fetching on-chain whale flows (exchange wallets + Whale Alert + Solscan)...")
    whale_flows = onchain.get_whale_flows(price_map, known_symbols)
    print(f"  {len(whale_flows)} token(s) with net flow above "
          f"${config.WHALE_MIN_TRANSFER_USD:,.0f} in the last "
          f"{config.WHALE_WINDOW_MINUTES} minutes")

    print("Fetching social signals (CoinGecko trending + Reddit + Telegram)...")
    social_signals = social.get_social_signals(known_symbols, scan_id)
    print(f"  {len(social_signals)} symbol(s) flagged by at least one social source")

    results = []
    pending_confluence = []  # (row, signals, wash_trading_suspected, cross_exchange_status)
    all_funding_rates = []  # every funding rate seen this scan, for the market-wide bias read

    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {symbol}" + " " * 10, end="\r")

        ohlcv = df.fetch_ohlcv_raw(spot, symbol, config.OHLCV_TIMEFRAME,
                                    config.VOLUME_LOOKBACK_CANDLES + 1)
        order_book = df.fetch_order_book(spot, symbol, config.ORDER_BOOK_DEPTH)

        signals = {}

        vol_signal = se.volume_zscore_signal(ohlcv) if ohlcv else None
        if vol_signal:
            signals["volume_zscore"] = vol_signal

        rush_signal = se.rush_order_signal(ohlcv) if ohlcv else None
        if rush_signal:
            signals["rush_order"] = rush_signal

        ob_signal = se.order_book_imbalance_signal(order_book) if order_book else None
        if ob_signal:
            signals["order_book_imbalance"] = ob_signal

        # Futures signals only if this symbol also trades as a perpetual
        if df.symbol_has_futures(futures, symbol):
            oi_now = df.fetch_open_interest(futures, symbol)
            oi_before = storage.get_oi(symbol)
            if oi_now is not None:
                storage.set_oi(symbol, oi_now, scan_id)
            oi_signal = se.oi_change_signal(oi_now, oi_before)
            if oi_signal:
                signals["oi_change"] = oi_signal

            funding = df.fetch_funding_rate(futures, symbol)
            all_funding_rates.append(funding)
            fr_signal = se.funding_rate_signal(funding)
            if fr_signal:
                signals["funding_rate"] = fr_signal

        base_asset = symbol.split("/")[0]

        ext_signal = se.external_signal(base_asset, lcw_gainers, lcw_losers, rapid_by_symbol)
        if ext_signal:
            signals["external_signal"] = ext_signal

        whale_signal = whale_flows.get(base_asset)
        if whale_signal:
            signals["whale_flow"] = whale_signal

        social_signal = social_signals.get(base_asset)
        if social_signal:
            signals["social_signal"] = social_signal

        if not signals:
            continue

        score = se.composite_score(signals)
        direction = se.direction_label(signals)

        # --- Phase 7: precision-filter layer (never changes score/direction above) ---
        volume_z_raw = signals["volume_zscore"][1] if "volume_zscore" in signals else None
        price_change_pct = se.candle_price_change_pct(ohlcv)
        wash_trading_suspected = se.is_wash_trading_suspected(volume_z_raw, price_change_pct)

        if score >= config.CONFIRMATION_CANDIDATE_MIN_SCORE:
            cross_exchange_status = df.check_cross_exchange_confirmation(base_asset, direction)
        else:
            cross_exchange_status = "SKIPPED"  # not worth the extra API calls below this score

        row = {
            "symbol": symbol,
            "price": price_map.get(base_asset),
            "score": round(score, 3),
            "direction": direction,
            "volume_z": volume_z_raw,
            "rush_order_z": signals["rush_order"][1]["trade_count_z"] if "rush_order" in signals else None,
            "rush_taker_ratio": signals["rush_order"][1]["taker_buy_ratio"] if "rush_order" in signals else None,
            "ob_imbalance": signals["order_book_imbalance"][1] if "order_book_imbalance" in signals else None,
            "oi_change_pct": signals["oi_change"][1] if "oi_change" in signals else None,
            "funding_rate": signals["funding_rate"][1] if "funding_rate" in signals else None,
            "external": signals["external_signal"][1]["text"] if "external_signal" in signals else "",
            "whale_net_flow_usd": signals["whale_flow"][1] if "whale_flow" in signals else None,
            "social": signals["social_signal"][1]["text"] if "social_signal" in signals else "",
            "confluence": None,  # filled in below, once the market-wide bias is known
            "wash_trading_flag": int(wash_trading_suspected),
            "cross_exchange_status": cross_exchange_status,
        }
        results.append(row)
        pending_confluence.append((row, signals, wash_trading_suspected, cross_exchange_status))

    print(" " * 40, end="\r")  # clear the progress line

    market_bias = se.compute_market_funding_bias(all_funding_rates)
    storage.record_market_context(scan_id, market_bias["long_share_pct"],
                                   market_bias["short_share_pct"], market_bias["bias"])
    if market_bias["bias"] != "NEUTRAL":
        print(f"⚠ Market-wide leverage bias: {market_bias['bias']} "
              f"(longs extreme: {market_bias['long_share_pct']}%, "
              f"shorts extreme: {market_bias['short_share_pct']}%, n={market_bias['sample_size']})")

    for row, signals, wash_trading_suspected, cross_exchange_status in pending_confluence:
        row["confluence"] = se.classify_confluence(
            signals, wash_trading_suspected, cross_exchange_status,
            direction=row["direction"], market_bias=market_bias["bias"],
        )

    for row in results:
        storage.record_symbol_snapshot(scan_id, row)
    storage.record_price_snapshots(scan_id, {s.split("/")[0]: price_map.get(s.split("/")[0]) for s in symbols})

    results.sort(key=lambda r: r["score"], reverse=True)
    top = results[: config.TOP_N_RESULTS]

    print_table(top)
    save_csv(results)
    maybe_alert(top)

    return top


def print_table(rows):
    if not rows:
        print("No signals found this scan.")
        return

    print(f"\n{'SYMBOL':<12}{'SCORE':<8}{'DIRECTION':<14}{'CONFLUENCE':<15}{'VOL_Z':<8}{'RUSH_Z':<8}{'OB_IMB':<9}{'OI_%':<8}{'FUNDING':<12}{'WHALE_USD':<14}SOURCES")
    print("-" * 143)
    for r in rows:
        whale = f"{r['whale_net_flow_usd']:+,.0f}" if r["whale_net_flow_usd"] is not None else ""
        sources = " | ".join(s for s in (r["external"], r["social"]) if s)
        wash = " ⚠WASH" if r.get("wash_trading_flag") else ""
        print(f"{r['symbol']:<12}{r['score']:<8}{r['direction']:<14}{r['confluence']:<15}"
              f"{str(r['volume_z']):<8}{str(r['rush_order_z']):<8}{str(r['ob_imbalance']):<9}"
              f"{str(r['oi_change_pct']):<8}{str(r['funding_rate']):<12}{whale:<14}{sources}{wash}")


def save_csv(rows):
    if not rows:
        return
    with open(config.RESULTS_CSV_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nSaved {len(rows)} results to {config.RESULTS_CSV_PATH}")


def maybe_alert(top_rows):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    hot = [r for r in top_rows if r["score"] >= config.TELEGRAM_ALERT_SCORE_THRESHOLD]
    if config.TELEGRAM_REQUIRE_CONFIRMED:
        hot = [r for r in hot if r["confluence"] == "CONFIRMED"]
    if not hot:
        return

    lines = [f"\U0001F6A8 Pump/Dump Watch — {datetime.now(timezone.utc).strftime('%H:%M UTC')}"]
    for r in hot:
        lines.append(f"{r['symbol']}: {r['direction']} · {r['confluence']} (score {r['score']})")

    send_telegram_alert("\n".join(lines))


if __name__ == "__main__":
    config.validate()
    storage.init_db()
    if "--loop" in sys.argv:
        while True:
            try:
                scan_once()
            except Exception as e:
                print(f"[error] scan failed: {e}")
            time.sleep(config.SCAN_INTERVAL_MINUTES * 60)
    else:
        scan_once()
