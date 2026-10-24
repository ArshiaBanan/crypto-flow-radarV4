"""
Phase 4 — social attention signals, combined from three independent sources:

  1. CoinGecko Trending — free, no key needed. What people are actively
     searching for on CoinGecko right now (refreshed roughly hourly, top 15).

  2. Reddit mention velocity — free read-only "script" app (PRAW). Counts
     ticker mentions across r/CryptoCurrency and similar subreddits in the
     current scan window, then compares that count to the coin's OWN
     rolling history (same z-score idea as the exchange-side volume
     signal) — so a coin that's always chatted about a lot doesn't
     permanently look "spiking"; only a genuine jump above its own normal
     does.

  3. Telegram channel monitor — free (Telethon/MTProto, official Telegram
     API). Watches specific public channels YOU choose — the signal
     channels and analysts this whole project was inspired by — for
     ticker mentions, with a lightweight bullish/bearish keyword read on
     each matched message (English + Persian keywords).

Each sub-source is independently optional: leave its config keys blank and
it's skipped, no crash. The three are combined exactly the way
signal_engine.external_signal() combines LiveCoinWatch + CryptoMeter — more
independent sources agreeing on the same coin means a higher score, not
just a longer text description.

Honest limitation (Phase 4): Reddit's z-score baseline used to be built only
from scans where a coin was mentioned at least once (a zero-mention scan
never wrote a "0" into memory). Phase 5 fixes this — history now comes from
`storage`, which has a row for every scan regardless, so zero-mention scans
correctly count as zeros in the baseline.
"""

import re
import time
import asyncio
import statistics
import requests
import config
import storage

# ---------------------------------------------------------------- matching

_CASHTAG_RE = re.compile(r"\$([A-Za-z]{2,10})\b")
_WORD_RE = re.compile(r"\b([A-Z]{3,10})\b")


def extract_ticker_mentions(text, known_symbols):
    """Returns the set of known Binance base-asset symbols mentioned in `text`."""
    if not text:
        return set()

    found = set()
    for m in _CASHTAG_RE.findall(text):
        sym = m.upper()
        if sym in known_symbols:
            found.add(sym)
    for m in _WORD_RE.findall(text):
        if m in known_symbols and m not in config.SOCIAL_TICKER_STOPLIST:
            found.add(m)
    return found


def estimate_sentiment_bias(text):
    """Lightweight keyword lean: +1 bullish, -1 bearish, 0 neutral/mixed."""
    if not text:
        return 0
    lowered = text.lower()
    bull = sum(1 for kw in config.SOCIAL_BULLISH_KEYWORDS if kw in lowered)
    bear = sum(1 for kw in config.SOCIAL_BEARISH_KEYWORDS if kw in lowered)
    if bull > bear:
        return 1
    if bear > bull:
        return -1
    return 0


# ------------------------------------------------------------- coingecko

COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"


def get_coingecko_trending():
    """Returns {symbol: rank} for CoinGecko's current top trending searches (1 = hottest)."""
    headers = {"x-cg-demo-api-key": config.COINGECKO_API_KEY} if config.COINGECKO_API_KEY else {}
    try:
        resp = requests.get(COINGECKO_TRENDING_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [warn] CoinGecko trending fetch failed: {e}")
        return {}

    trending = {}
    for i, entry in enumerate(data.get("coins", []), 1):
        symbol = (entry.get("item") or {}).get("symbol", "").upper()
        if symbol:
            trending[symbol] = i
    return trending


# ---------------------------------------------------------------- reddit

def get_reddit_mention_signals(known_symbols, scan_id):
    """
    Returns {symbol: (score 0-1, {"count", "z", "avg_bias"})} for symbols
    whose mention count this scan is a statistically unusual spike versus
    their own rolling history (stored in `storage`, zero-padded — see the
    module docstring) — not just "a lot of mentions" for a coin that's
    always chatted about a lot.
    """
    if not config.REDDIT_CLIENT_ID or not config.REDDIT_CLIENT_SECRET:
        return {}

    try:
        import praw
    except ImportError:
        print("  [warn] praw not installed — run: pip install praw")
        return {}

    try:
        reddit = praw.Reddit(
            client_id=config.REDDIT_CLIENT_ID,
            client_secret=config.REDDIT_CLIENT_SECRET,
            user_agent=config.REDDIT_USER_AGENT,
        )
    except Exception as e:
        print(f"  [warn] Reddit auth failed: {e}")
        return {}

    counts = {}
    bias_sum = {}

    for sub_name in config.REDDIT_SUBREDDITS:
        try:
            sub = reddit.subreddit(sub_name)
            texts = [f"{p.title} {p.selftext or ''}" for p in sub.new(limit=config.REDDIT_POST_LIMIT)]
            texts += [c.body or "" for c in sub.comments(limit=config.REDDIT_COMMENT_LIMIT)]
        except Exception as e:
            print(f"  [warn] Reddit fetch failed for r/{sub_name}: {e}")
            continue

        for text in texts:
            mentions = extract_ticker_mentions(text, known_symbols)
            if not mentions:
                continue
            bias = estimate_sentiment_bias(text)
            for sym in mentions:
                counts[sym] = counts.get(sym, 0) + 1
                bias_sum[sym] = bias_sum.get(sym, 0) + bias

    # Persist this scan's raw counts BEFORE reading history back, so the
    # history query (which looks strictly before scan_id) stays clean —
    # this scan's own numbers never leak into its own baseline.
    storage.record_reddit_mentions(scan_id, counts)

    signals = {}
    for sym, count in counts.items():
        history = storage.get_reddit_mention_history(sym, scan_id, config.REDDIT_MENTION_HISTORY_LENGTH)
        if len(history) < 3:
            continue  # not enough scan history yet to judge what's "normal" for this coin

        mean = statistics.mean(history)
        stdev = statistics.pstdev(history)
        if stdev == 0:
            continue

        z = (count - mean) / stdev
        if z < config.REDDIT_MENTION_ZSCORE_THRESHOLD:
            continue

        avg_bias = bias_sum[sym] / count
        score = min(z / (config.REDDIT_MENTION_ZSCORE_THRESHOLD * 2), 1.0)
        signals[sym] = (score, {"count": count, "z": round(z, 2), "avg_bias": round(avg_bias, 2)})

    return signals


# -------------------------------------------------------------- telegram

async def _telegram_scan_async(known_symbols):
    from telethon import TelegramClient

    client = TelegramClient(
        config.TELEGRAM_SESSION_NAME,
        int(config.TELEGRAM_API_ID),
        config.TELEGRAM_API_HASH,
    )
    await client.start()

    cutoff = time.time() - config.TELEGRAM_WINDOW_MINUTES * 60
    counts, bias_sum, channels_touched = {}, {}, {}

    for channel in config.TELEGRAM_MONITORED_CHANNELS:
        try:
            entity = await client.get_entity(channel)
            async for message in client.iter_messages(
                entity, limit=config.TELEGRAM_MESSAGE_LIMIT_PER_CHANNEL
            ):
                if message.date.timestamp() < cutoff:
                    break  # messages come back newest-first — everything after is older
                text = message.message or ""
                mentions = extract_ticker_mentions(text, known_symbols)
                if not mentions:
                    continue
                bias = estimate_sentiment_bias(text)
                for sym in mentions:
                    counts[sym] = counts.get(sym, 0) + 1
                    bias_sum[sym] = bias_sum.get(sym, 0) + bias
                    channels_touched.setdefault(sym, set()).add(str(channel))
        except Exception as e:
            print(f"  [warn] Telegram fetch failed for {channel}: {e}")
            continue

    await client.disconnect()
    return counts, bias_sum, channels_touched


def get_telegram_mention_signals(known_symbols):
    """
    Returns {symbol: (score 0-1, {"count", "avg_bias", "channels"})} for
    tickers mentioned at least TELEGRAM_MIN_MENTIONS times across your
    monitored channels in the last TELEGRAM_WINDOW_MINUTES.
    """
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH \
            or not config.TELEGRAM_MONITORED_CHANNELS:
        return {}

    try:
        import telethon  # noqa: F401  (import check only)
    except ImportError:
        print("  [warn] telethon not installed — run: pip install telethon")
        return {}

    try:
        counts, bias_sum, channels_touched = asyncio.run(_telegram_scan_async(known_symbols))
    except Exception as e:
        print(f"  [warn] Telegram scan failed: {e}")
        return {}

    signals = {}
    for sym, count in counts.items():
        if count < config.TELEGRAM_MIN_MENTIONS:
            continue
        avg_bias = bias_sum[sym] / count
        score = min(count / (config.TELEGRAM_MIN_MENTIONS * 3), 1.0)
        signals[sym] = (score, {
            "count": count,
            "avg_bias": round(avg_bias, 2),
            "channels": sorted(channels_touched.get(sym, [])),
        })
    return signals


# --------------------------------------------------------------- combine

def get_social_signals(known_symbols, scan_id):
    """
    Runs all three sub-sources and combines them into one social_signal per
    symbol — {symbol: (score 0-1, {"text": description, "bias": int})} —
    the same shape signal_engine.external_signal() already produces, so
    main.py can drop it straight into a symbol's signals dict.
    """
    trending = get_coingecko_trending()
    reddit_signals = get_reddit_mention_signals(known_symbols, scan_id)
    telegram_signals = get_telegram_mention_signals(known_symbols)

    all_symbols = set(trending) | set(reddit_signals) | set(telegram_signals)
    combined = {}

    for sym in all_symbols:
        hits = []
        bias = 0

        rank = trending.get(sym)
        if rank:
            hits.append(f"CG trending #{rank}")
            bias += 1  # showing up in trending searches leans toward attention/inflow

        if sym in reddit_signals:
            _, raw = reddit_signals[sym]
            hits.append(f"Reddit {raw['count']}x mentions (z={raw['z']})")
            bias += 1 if raw["avg_bias"] > 0 else (-1 if raw["avg_bias"] < 0 else 0)

        if sym in telegram_signals:
            _, raw = telegram_signals[sym]
            hits.append(f"TG {raw['count']}x on {'/'.join(raw['channels'])}")
            bias += 1 if raw["avg_bias"] > 0 else (-1 if raw["avg_bias"] < 0 else 0)

        if not hits:
            continue

        score = min(0.5 * len(hits), 1.0)
        combined[sym] = (score, {"text": "; ".join(hits), "bias": bias})

    return combined
