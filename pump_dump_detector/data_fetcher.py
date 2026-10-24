"""
Handles all data retrieval from exchanges via ccxt.
Spot data (OHLCV, order book) comes from Binance spot.
Futures data (open interest, funding rate) comes from Binance USDT-M futures.
"""

import time
import ccxt
import config


def _retry(fn, *args, max_attempts=3, base_delay=2, label="request", **kwargs):
    """
    Retries a callable with exponential backoff (2s, 4s, 8s, ...) for the
    handful of calls whose failure would waste an ENTIRE scan cycle, not
    just one symbol's worth of data. Every fetch_* function below this one
    deliberately does NOT retry — a single symbol failing is cheap to skip,
    and retrying it would slow down every other symbol queued behind it in
    the scan loop. This is only for calls with no such cheap fallback.
    """
    last_exc = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            last_exc = e
            if attempt < max_attempts:
                delay = base_delay * (2 ** (attempt - 1))
                print(f"  [warn] {label} failed (attempt {attempt}/{max_attempts}): {e} — retrying in {delay}s")
                time.sleep(delay)
    print(f"  [error] {label} failed after {max_attempts} attempts: {last_exc}")
    raise last_exc


def get_spot_exchange():
    return ccxt.binance({"enableRateLimit": True})


def get_futures_exchange():
    return ccxt.binanceusdm({"enableRateLimit": True})


def load_futures_markets(futures_exchange):
    """
    Loads the futures market list once per scan — every symbol's
    symbol_has_futures() check depends on this, so a transient failure here
    would otherwise silently turn OI/funding off for the whole scan rather
    than failing loudly or recovering.
    """
    return _retry(futures_exchange.load_markets, label="futures load_markets")


def get_usdt_symbols_and_prices(exchange):
    """
    Returns (symbols, price_map):
      symbols   — active USDT spot pairs above the minimum 24h quote volume
      price_map — {base_asset: last_price_usdt} for every symbol in the
                  full active-market list (not just the filtered symbols),
                  reused by the on-chain whale-flow module to convert raw
                  token amounts into USD without a second price source.
                  USDT is treated as ~USD, which is accurate enough here.

    This is the one call whose failure means the ENTIRE scan produces
    nothing — every symbol's loop depends on its output — so it's retried
    with backoff rather than left to fail a whole 15-minute scan cycle over
    a transient blip.
    """
    markets = _retry(exchange.load_markets, label="spot load_markets")
    tickers = _retry(exchange.fetch_tickers, label="spot fetch_tickers")

    symbols = []
    price_map = {}

    for symbol, market in markets.items():
        if market.get("quote") != config.QUOTE_CURRENCY:
            continue
        ticker = tickers.get(symbol)
        if not ticker:
            continue

        last = ticker.get("last")
        if last:
            price_map[market.get("base")] = last

        if not market.get("active"):
            continue
        if symbol.replace("/", "") in config.EXCLUDE_SYMBOLS:
            continue
        quote_volume = ticker.get("quoteVolume") or 0
        if quote_volume < config.MIN_24H_QUOTE_VOLUME:
            continue

        symbols.append(symbol)

    return symbols, price_map


def fetch_ohlcv(exchange, symbol, timeframe, limit):
    """Fetch OHLCV candles. Returns None on failure (symbol may be thin or delisted mid-scan)."""
    try:
        return exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
    except Exception as e:
        print(f"  [warn] OHLCV fetch failed for {symbol}: {e}")
        return None


def fetch_ohlcv_raw(exchange, symbol, timeframe, limit):
    """
    Same candles as fetch_ohlcv, but via Binance's raw klines endpoint
    (ccxt's "implicit API" passthrough — publicGetKlines) instead of
    ccxt's unified fetch_ohlcv. The unified method truncates Binance's
    12-field kline response down to [time, open, high, low, close,
    volume] and drops the two fields signal_engine.rush_order_signal
    needs: number of trades, and taker-buy base volume. Same underlying
    HTTP request, same rate-limit weight as fetch_ohlcv — this is a free
    upgrade, not an extra call.

    Returns rows shaped [open_time, open, high, low, close, volume,
    num_trades, taker_buy_base_volume] — indices 0-5 line up exactly with
    the unified OHLCV format, so volume_zscore_signal and
    candle_price_change_pct work on this unchanged; indices 6-7 are new.
    Returns None on failure.
    """
    try:
        market_id = exchange.market(symbol)["id"]
        raw = exchange.publicGetKlines({"symbol": market_id, "interval": timeframe, "limit": limit})
    except Exception as e:
        print(f"  [warn] raw klines fetch failed for {symbol}: {e}")
        return None

    cleaned = []
    for row in raw:
        try:
            cleaned.append([
                int(row[0]), float(row[1]), float(row[2]), float(row[3]),
                float(row[4]), float(row[5]), int(row[8]), float(row[9]),
            ])
        except (IndexError, TypeError, ValueError):
            continue  # one malformed row shouldn't void the whole candle set
    return cleaned or None


def fetch_order_book(exchange, symbol, depth):
    try:
        return exchange.fetch_order_book(symbol, limit=depth)
    except Exception as e:
        print(f"  [warn] Order book fetch failed for {symbol}: {e}")
        return None


def fetch_open_interest(futures_exchange, symbol):
    """Current open interest for a USDT-M perpetual, in contracts."""
    try:
        oi = futures_exchange.fetch_open_interest(symbol)
        return oi.get("openInterestAmount") or oi.get("info", {}).get("openInterest")
    except Exception:
        return None


def fetch_funding_rate(futures_exchange, symbol):
    try:
        fr = futures_exchange.fetch_funding_rate(symbol)
        return fr.get("fundingRate")
    except Exception:
        return None


def symbol_has_futures(futures_exchange, symbol):
    """Check whether a spot symbol also trades as a USDT-M perpetual."""
    try:
        markets = futures_exchange.markets or futures_exchange.load_markets()
        return symbol in markets
    except Exception:
        return False


# ---------------------------------------------------- Phase 7: cross-exchange

_cross_exchanges = {}  # ccxt id -> exchange instance, built lazily and reused all scan


def _get_cross_exchange(exchange_id):
    if exchange_id not in _cross_exchanges:
        _cross_exchanges[exchange_id] = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    return _cross_exchanges[exchange_id]


def check_cross_exchange_confirmation(base_asset, direction):
    """
    Checks whether OTHER exchanges (config.CROSS_EXCHANGE_IDS — Bybit, OKX
    by default) show the same directional move for this symbol. A real
    pump/dump usually shows up on multiple venues; a spike confined to one
    exchange only is a wash-trading/manipulation red flag.

    Returns one of:
      "CONFIRMED"     — at least one other exchange shows a same-direction
                         move of at least CROSS_EXCHANGE_MIN_AGREE_PCT
      "CONTRADICTED"  — every other exchange that lists it shows a flat or
                         opposite move
      "NOT_LISTED"    — the symbol isn't listed as a USDT pair on any of
                         the checked exchanges (neutral, not penalized —
                         plenty of legitimate small-caps are Binance-only)
    """
    want_positive = direction == "PUMP WATCH"
    found_listed = False
    any_confirmed = False

    for exchange_id in config.CROSS_EXCHANGE_IDS:
        try:
            exchange = _get_cross_exchange(exchange_id)
            ticker = exchange.fetch_ticker(f"{base_asset}/USDT")
        except Exception:
            continue  # not listed there, or a transient error — either way, skip it

        pct = ticker.get("percentage")
        if pct is None:
            continue

        found_listed = True
        if want_positive and pct >= config.CROSS_EXCHANGE_MIN_AGREE_PCT:
            any_confirmed = True
        elif not want_positive and pct <= -config.CROSS_EXCHANGE_MIN_AGREE_PCT:
            any_confirmed = True

    if not found_listed:
        return "NOT_LISTED"
    return "CONFIRMED" if any_confirmed else "CONTRADICTED"
