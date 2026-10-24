"""
Configuration for the Pump/Dump Detector.
Adjust thresholds here as you tune the system — nothing else needs to change.
"""

# --- Exchange settings ---
EXCHANGE_ID = "binance"              # ccxt exchange id (spot)
FUTURES_EXCHANGE_ID = "binanceusdm"  # ccxt exchange id for USDT-M futures (OI, funding)
QUOTE_CURRENCY = "USDT"

# --- Universe filter ---
MIN_24H_QUOTE_VOLUME = 5_000_000     # ignore illiquid coins (in USDT)
EXCLUDE_SYMBOLS = ["USDCUSDT", "FDUSDUSDT", "TUSDUSDT", "BUSDUSDT", "DAIUSDT"]

# --- Volume spike detection ---
OHLCV_TIMEFRAME = "1h"
VOLUME_LOOKBACK_CANDLES = 24         # how many past candles form the baseline
VOLUME_ZSCORE_THRESHOLD = 2.5        # candle volume this many std-devs above mean = spike

# --- Order book imbalance ---
ORDER_BOOK_DEPTH = 50                # top N bid/ask levels to sum
IMBALANCE_THRESHOLD = 0.35           # |bid-ask| / (bid+ask) above this = notable

# --- Futures signals ---
OI_CHANGE_THRESHOLD_PCT = 8.0        # % change in open interest (scan-to-scan) considered significant
FUNDING_RATE_EXTREME = 0.0008        # 0.08% per 8h funding considered "hot"

# --- Composite scoring weights (should sum to 1.0) ---
# external_signal, whale_flow, and social_signal carry the most weight on
# purpose: they're the only signals that come from sources OTHER than
# Binance itself, so when they agree with the exchange-side signals, that's
# real cross-validation, not just more of the same data restated.
WEIGHTS = {
    "volume_zscore": 0.10,
    "order_book_imbalance": 0.06,
    "rush_order": 0.17,
    "oi_change": 0.10,
    "funding_rate": 0.06,
    "external_signal": 0.15,
    "whale_flow": 0.20,
    "social_signal": 0.16,
}

# rush_order_signal (signal_engine.py) — z-score threshold for the latest
# candle's TRADE COUNT vs its own baseline. Deliberately separate from
# VOLUME_ZSCORE_THRESHOLD: trade count and trade volume are different
# distributions (a handful of large orders vs many small ones can produce
# the same volume with very different trade counts), so they earn their
# own threshold rather than sharing one tuned for volume.
RUSH_ORDER_ZSCORE_THRESHOLD = 3.0

# --- LiveCoinWatch — replicates the manual workflow (pair=BTC, filter by
# BTC-denominated volume band, rank by 24h change) fully automatically ---
LCW_API_KEY = ""  # free key: https://www.livecoinwatch.com/tools/api
LCW_MIN_VOLUME_BTC = 200
LCW_MAX_VOLUME_BTC = 500
LCW_TOP_N = 10  # how many top gainers / top losers to keep from the band

# --- CryptoMeter.io — free-tier endpoints only (10,000 calls/month quota) ---
# /rapid-movements/ independently flags PUMP/DUMP assets over the last 4h.
# /trend-indicator-v3/ gives overall market buy/sell pressure as context.
CRYPTOMETER_API_KEY = ""  # key: https://www.cryptometer.io/api-manage

# --- On-chain whale tracking (Etherscan API V2 — one key, 60+ EVM chains) ---
# free key: https://etherscan.io/apis — 5 req/sec, 100k req/day, and the
# same key works on Ethereum, BSC, and every other chain via chainid=.
#
# Whale Alert (the obvious "official" choice) has NO free API tier as of
# 2026 — cheapest is $29.95/mo for a rate-capped WebSocket feed, $699/mo
# for full REST access. Rather than build Phase 3 around a paid-only
# dependency by default, this does the same job directly against a known
# exchange wallet using Etherscan's free tier. Trade-off: only ERC-20/
# BEP-20 tokens are visible this way — native-chain-only assets (SOL, TRX,
# native BTC movements, etc.) aren't covered by this method at all.
ETHERSCAN_API_KEY = ""

# Ships with two wallets from two different exchanges — both verified
# directly on their own etherscan.io page, not guessed. Still deliberately
# short: a WRONG address here would silently corrupt every downstream score
# with no error to catch it. To add more, open etherscan.io or bscscan.com,
# search the exchange's name, open any address tagged "Exchanges", confirm
# the label shown directly on ITS OWN page, then paste it in below.
KNOWN_EXCHANGE_WALLETS = [
    {"label": "Binance 14", "address": "0x28C6c06298d514Db089934071355E5743bf21d60"},
    {"label": "OKX Hot Wallet 3", "address": "0xa9ac43f5b5e38155a288d1a01d2cbc4478e14573"},
]

WHALE_CHAINS = [
    {"name": "Ethereum", "chain_id": 1},
    {"name": "BNB Smart Chain", "chain_id": 56},
]

WHALE_WINDOW_MINUTES = 240             # 4h — matches CryptoMeter's rapid-movements window
WHALE_MAX_TRANSFERS_PER_WALLET = 200   # most recent N token transfers pulled per wallet/chain
WHALE_MIN_TRANSFER_USD = 50_000        # ignore dust transfers below this
WHALE_FLOW_THRESHOLD_USD = 500_000     # |net flow| at/above this scores as a full-strength signal

# --- Whale Alert's public Telegram channel (free — no API needed) ---
# Whale Alert's own API starts at $29.95/mo with no free tier (see the note
# in onchain_source.py). But Whale Alert ALSO posts every alert it detects,
# for free, to a public Telegram channel — 300K+ subscribers, no signup
# needed beyond joining. Reuses the exact same Telethon credentials as
# Phase 4's channel monitoring (TELEGRAM_API_ID/HASH above), just pointed
# at a different channel with different parsing (structured transfer
# messages, not ticker mentions). This covers far more chains/tokens than
# our own two watched wallets (BTC, ETH, XRP, USDT, TRX, and 100+ ERC-20s),
# at the cost of only seeing transfers Whale Alert itself flags as large
# enough to post — their threshold, not ours.
WHALE_ALERT_CHANNEL = "whale_alert_io"
WHALE_ALERT_WINDOW_MINUTES = 240        # matches WHALE_WINDOW_MINUTES's 4h horizon
# Whale Alert posts many alerts per hour on busy days, and the scan stops
# reading at whichever comes first: this many messages, or the window
# above. If the limit is hit first, the EFFECTIVE window quietly shrinks
# to less than 4h — raised from 200 for that reason. Telethon pages through
# Telegram's 100-per-request cap automatically, so this costs a few extra
# requests, not a rate-limit risk.
WHALE_ALERT_MESSAGE_LIMIT = 400

# --- Solscan (Solana) — free API key, extends whale tracking past
# ERC-20/BEP-20 to Solana's SPL tokens, a real gap before this: Solana is
# one of the most pump-and-dump-prone ecosystems in crypto and this project
# previously couldn't see on-chain activity there at all. ---
# Free key: https://solscan.io/apis (lower rate limit than a paid Pro key,
# but covers the core account/transfer endpoint used here).
# CONFIDENCE NOTE — read before relying on this in production: every other
# integration in this project was checked against a primary source (an
# official doc page fetched directly, or a real function call this project
# executed). This one wasn't — sandbox network access couldn't reach
# Solscan directly, so the endpoint path, header name, and response field
# names below are reconstructed from several consistent third-party code
# examples, not from Solscan's own docs page or a live test call. It's
# written defensively (multiple field-name fallbacks, all wrapped in
# try/except) and should degrade to "no Solana signal" rather than crash
# if a field name is off — but if it silently returns nothing, check the
# field names in onchain_source.py against a real response first.
SOLSCAN_API_KEY = ""
SOLSCAN_BASE_URL = "https://pro-api.solscan.io/v2.0"
# Verified directly on solscan.io (labeled "Binance 2" on its own account
# page), not guessed — same "small, verified list" principle as
# KNOWN_EXCHANGE_WALLETS above.
SOLANA_EXCHANGE_WALLETS = [
    {"label": "Binance 2", "address": "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"},
]
SOLSCAN_MAX_TRANSFERS_PER_WALLET = 100
SOLSCAN_WINDOW_MINUTES = 240

# --- Named but not integrated — with the specific reason why ---
# BscScan: NOT a gap — already covered. Etherscan API V2 (ETHERSCAN_API_KEY
#   above) reaches BSC via chainid=56 in the same calls that reach Ethereum;
#   see WHALE_CHAINS. No separate BscScan key needed or used.
# Arkham Intelligence: API access is application-gated (a request-access
#   form + approval, not instant self-serve signup like every other key in
#   this file) and billed per-credit rather than offering a flat free tier
#   — a different shape of access than this project builds around. Worth
#   revisiting if API access is ever granted.
# CoinGlass: confirmed directly against their own pricing page — no free
#   API tier at all, $29/mo minimum (Hobbyist), $299/mo minimum for any
#   commercial use. Not integrated.
# X (Twitter): confirmed directly against X's current pricing — the free
#   tier was discontinued in February 2026; reading posts now costs
#   $5-10 per 1,000 with no free allowance at any volume. Not integrated,
#   and a scraped/unofficial workaround isn't a safe substitute (X's ToS,
#   and reliability). The Telegram channel monitoring above already covers
#   "watch specific influential accounts" for anyone who also posts to a
#   Telegram channel, which many crypto callers do.

# --- Social attention signals (three independent sub-sources, combined) ---

# CoinGecko Trending — free, no key required on the public tier; a free
# Demo key raises the rate limit (we only need 1 call/scan either way).
# Dashboard: https://www.coingecko.com/en/developers/dashboard
COINGECKO_API_KEY = ""

# Reddit — free "script" app, read-only, no username/password needed:
# https://www.reddit.com/prefs/apps -> create app -> type "script"
REDDIT_CLIENT_ID = ""
REDDIT_CLIENT_SECRET = ""
REDDIT_USER_AGENT = "pump-dump-detector/1.0"
REDDIT_SUBREDDITS = ["CryptoCurrency", "CryptoMoonShots", "SatoshiStreetBets", "altcoin"]
REDDIT_POST_LIMIT = 100              # most recent posts scanned per subreddit per scan
REDDIT_COMMENT_LIMIT = 100           # most recent comments scanned per subreddit per scan
REDDIT_MENTION_HISTORY_LENGTH = 12   # scans kept as baseline (12 * 15min = 3h with --loop)
REDDIT_MENTION_ZSCORE_THRESHOLD = 2.5

# Telegram — free api_id/api_hash from https://my.telegram.org. First run
# needs an interactive login (phone number + code) to create a local
# session file; after that it runs headless. Point this at channels YOU
# already follow and trust — the signal-caller channels this whole project
# was inspired by — by their @username.
TELEGRAM_API_ID = ""
TELEGRAM_API_HASH = ""
TELEGRAM_SESSION_NAME = "pump_dump_detector"
TELEGRAM_MONITORED_CHANNELS = []     # e.g. ["some_channel_username"]
TELEGRAM_WINDOW_MINUTES = 60
TELEGRAM_MESSAGE_LIMIT_PER_CHANNEL = 200
TELEGRAM_MIN_MENTIONS = 1            # even 1 mention in a trusted channel is meaningful

# Shared ticker-mention matching (used for both Reddit and Telegram text).
# Cashtags ($BTC) are always trusted. A bare uppercase word only counts if
# it's already one of our tracked Binance base assets AND not in this
# stoplist of common words that also happen to be real ticker symbols.
SOCIAL_TICKER_STOPLIST = {
    "ONE", "FOR", "ALL", "NEW", "CAN", "NOW", "TOP", "WIN", "BIG", "LOW",
    "ICE", "SUN", "FUN", "AI", "GO", "IT", "OK", "US", "SO", "ME", "MY",
    "UP", "IN", "ON", "AT", "BE", "DO", "IF", "NO", "OR", "TO", "BUY", "SELL",
    "GAS", "BAND", "REN", "SAFE", "MASK", "PEOPLE", "FLOW", "CTSI",
}
SOCIAL_BULLISH_KEYWORDS = [
    "buy", "long", "bullish", "pump", "moon", "accumulate", "breakout", "buying",
    "خرید", "صعودی", "پامپ", "رشد", "لانگ", "ورود پول",
]
SOCIAL_BEARISH_KEYWORDS = [
    "sell", "short", "bearish", "dump", "crash", "exit", "capitulation", "selling",
    "فروش", "نزولی", "دامپ", "افت", "شورت", "خروج پول",
]

# --- Phase 7: precision filter (confluence tier + wash-trading + cross-exchange) ---

# Wash-trading guard: a volume spike with (almost) no price movement is the
# classic signature of round-trip/self-trading rather than real buying or
# selling pressure. Reuses VOLUME_ZSCORE_THRESHOLD above as the "spike" bar.
WASH_TRADING_MIN_PRICE_MOVE_PCT = 1.0

# Cross-exchange confirmation — only run (it costs 2 extra API calls) on
# candidates that already cleared this score, not the whole scanned universe.
CONFIRMATION_CANDIDATE_MIN_SCORE = 0.4
CROSS_EXCHANGE_IDS = ["bybit", "okx"]       # ccxt ids, both free/keyless for public tickers
CROSS_EXCHANGE_MIN_AGREE_PCT = 1.0          # other exchange must show at least this much same-direction move

# The CONFIRMED tier itself — see signal_engine.classify_confluence.
CONFLUENCE_CONFIRMED_MIN_SCORE = 0.65
CONFLUENCE_MIN_INDEPENDENT_FAMILIES = 2     # of: external_signal, whale_flow, social_signal

# Market-wide leverage bias — a free substitute for liquidation-cascade
# data. CryptoMeter's liquidation endpoint (liquidation-data-v2) turned out,
# on checking their own /info/ endpoint response, to be listed under PAID
# extra_endpoints, not the free tier the rest of this project sticks to —
# so it's not used here, rather than quietly building on a source that
# isn't actually free. Instead, this reuses funding rates already being
# fetched per-symbol for FUNDING_RATE_EXTREME (Phase 1) and aggregates them
# across the whole scan at zero extra API cost: crowded one-sided leverage
# is what MAKES a liquidation cascade possible in the first place, whether
# or not a paid feed shows you the cascade itself firing.
#   - many perpetuals with sharply positive funding at once -> longs are
#     crowded and paying to stay in -> a drop would liquidate them in a
#     cascade -> market-wide DUMP risk
#   - many perpetuals with sharply negative funding at once -> shorts are
#     crowded -> a rally would squeeze them -> market-wide PUMP fuel
MARKET_BIAS_MIN_SAMPLE = 20          # need at least this many perpetuals checked to trust the read
MARKET_BIAS_SHARE_THRESHOLD_PCT = 15.0  # this % of perpetuals at an extreme funding rate = notable skew

# --- Output ---
TOP_N_RESULTS = 20
SCAN_INTERVAL_MINUTES = 15
RESULTS_CSV_PATH = "scan_results.csv"

# --- Persistent storage + backtesting (SQLite — a single local file, no
# server to install or run). Replaces the in-memory OI/Reddit history from
# earlier phases (which forgot everything on restart) and stores every
# scan so backtest.py can answer "when the score was high, what actually
# happened next?" ---
DB_PATH = "pump_dump_detector.db"
BACKTEST_DEFAULT_MIN_SCORE = 0.7
BACKTEST_DEFAULT_FORWARD_HOURS = 24
# backtest.py --lift compares win rates between calls WITH vs WITHOUT each
# factor. A group smaller than this is flagged as too small to trust —
# a 5-vs-3 sample can show a huge "lift" from pure luck.
LIFT_MIN_RELIABLE_SAMPLE = 30

# --- Telegram (optional — leave blank to disable) ---
TELEGRAM_BOT_TOKEN = ""
TELEGRAM_CHAT_ID = ""
TELEGRAM_ALERT_SCORE_THRESHOLD = 0.7   # only alert when composite score is at/above this
TELEGRAM_REQUIRE_CONFIRMED = True      # Phase 7: also require the CONFIRMED confluence tier,
                                        # not just a high score — this is the precision filter


def validate():
    """
    Sanity-checks this file at startup and prints clear warnings instead of
    letting a typo silently corrupt every score. Called once from main.py
    before the first scan. Never raises — a misconfigured optional key
    should degrade that one signal, not crash the whole scanner (the same
    philosophy every fetch function in this project already follows).
    """
    problems = []

    weight_sum = sum(WEIGHTS.values())
    if abs(weight_sum - 1.0) > 0.01:
        problems.append(
            f"WEIGHTS sums to {weight_sum:.3f}, not 1.0 — composite_score() renormalizes "
            f"over present signals regardless, but a sum this far off usually means a typo "
            f"when hand-editing a weight."
        )

    expected_signal_keys = {
        "volume_zscore", "order_book_imbalance", "rush_order", "oi_change", "funding_rate",
        "external_signal", "whale_flow", "social_signal",
    }
    unknown_keys = set(WEIGHTS) - expected_signal_keys
    if unknown_keys:
        problems.append(
            f"WEIGHTS has keys signal_engine.composite_score never produces: {sorted(unknown_keys)} "
            f"— they'll just sit unused. Check for a typo against signal_engine.py's signal names."
        )

    optional_sources = [
        ("LiveCoinWatch", LCW_API_KEY, "external_signal (LCW half)"),
        ("CryptoMeter", CRYPTOMETER_API_KEY, "external_signal (CryptoMeter half)"),
        ("Etherscan (on-chain)", ETHERSCAN_API_KEY, "whale_flow"),
        ("Whale Alert channel (needs Telegram API_ID/HASH)", TELEGRAM_API_ID and TELEGRAM_API_HASH,
         "whale_flow (Whale Alert half)"),
        ("Solscan (Solana)", SOLSCAN_API_KEY, "whale_flow (Solana half)"),
        ("Reddit", REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET, "social_signal (Reddit half)"),
        ("Telegram monitoring", TELEGRAM_API_ID and TELEGRAM_API_HASH and TELEGRAM_MONITORED_CHANNELS,
         "social_signal (Telegram half)"),
    ]
    missing = [name for name, configured, _ in optional_sources if not configured]
    if missing:
        print(f"[config] Not configured yet, running without them: {', '.join(missing)}. "
              f"The scanner still works — those signals just won't fire. See README.md to add keys.")

    if not problems:
        return True

    print("\n[config] Configuration warnings (scan will still run):")
    for p in problems:
        print(f"  - {p}")
    print()
    return False


# Environment overrides for the integrated web deployment. Empty values preserve upstream defaults.
import os as _os
for _name in (
    "LCW_API_KEY", "CRYPTOMETER_API_KEY", "ETHERSCAN_API_KEY", "SOLSCAN_API_KEY",
    "COINGECKO_API_KEY", "REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET",
    "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
):
    _value = _os.getenv(_name)
    if _value:
        globals()[_name] = _value
if _os.getenv("TELEGRAM_MONITORED_CHANNELS"):
    TELEGRAM_MONITORED_CHANNELS = [x.strip() for x in _os.getenv("TELEGRAM_MONITORED_CHANNELS", "").split(",") if x.strip()]
if _os.getenv("V40_SCAN_INTERVAL_MINUTES"):
    SCAN_INTERVAL_MINUTES = max(5, int(_os.getenv("V40_SCAN_INTERVAL_MINUTES", "15")))
