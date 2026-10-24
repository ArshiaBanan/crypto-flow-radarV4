"""
tests.py — automated test suite for the Pump/Dump Detector.

Run this after ANY change to signal_engine.py, storage.py, or main.py's
scan logic, before trusting the change:

    python tests.py

No test framework needed (plain assertions, stdlib only) — consistent with
the rest of the project's "minimal dependencies" approach. Fully offline:
ccxt is stubbed out and every exchange/API call is mocked, so this is safe
to run anytime, including with no network at all. Uses a temporary SQLite
file that's deleted automatically, so it never touches your real
pump_dump_detector.db.

This exists because every phase of this project was manually tested with
throwaway scripts before being shipped — this file is those tests, kept
instead of thrown away, so the NEXT change can be checked the same way
without re-inventing the test data each time.
"""

import os
import sys
import types
import random
import traceback

# --- Stub ccxt before anything imports it. data_fetcher.py only touches
# ccxt attributes inside function bodies (ccxt.binance(...) etc.), which
# the integration test monkey-patches over anyway, so a bare dummy module
# satisfies every `import ccxt` in the codebase. ---
_fake_ccxt = types.ModuleType("ccxt")
for _name in ("binance", "binanceusdm", "bybit", "okx"):
    setattr(_fake_ccxt, _name, lambda *a, **k: None)
sys.modules.setdefault("ccxt", _fake_ccxt)

TEST_DB_PATH = "_test_pump_dump_detector.db"

import config
config.DB_PATH = TEST_DB_PATH

import signal_engine as se
import storage

_failures = []
_passed = 0


def check(name, condition, detail=""):
    global _passed
    if condition:
        _passed += 1
    else:
        _failures.append(f"{name}  {detail}")
        print(f"  ✗ FAIL: {name}  {detail}")


# ======================================================================
# signal_engine.py — pure functions, no I/O
# ======================================================================

def test_volume_zscore_signal():
    print("\n--- volume_zscore_signal ---")
    random.seed(1)
    baseline = [[i, 1, 1, 1, 1, 100 + random.uniform(-5, 5)] for i in range(config.VOLUME_LOOKBACK_CANDLES)]
    spike = baseline + [[999, 1, 1, 1, 1, 1000]]
    result = se.volume_zscore_signal(spike)
    check("volume spike returns a signal", result is not None)
    if result:
        score, z = result
        check("volume spike z-score is large and positive", z > 3, f"z={z}")
        check("volume spike score is in [0,1]", 0 <= score <= 1, f"score={score}")

    check("too-short ohlcv returns None", se.volume_zscore_signal(spike[:3]) is None)
    check("empty ohlcv returns None", se.volume_zscore_signal([]) is None)
    check("None ohlcv returns None", se.volume_zscore_signal(None) is None)

    flat = [[i, 1, 1, 1, 1, 100] for i in range(config.VOLUME_LOOKBACK_CANDLES + 1)]
    check("zero-variance baseline returns None (no div-by-zero)", se.volume_zscore_signal(flat) is None)


def test_rush_order_signal():
    """
    signal_engine.rush_order_signal — the trade-count/taker-buy-ratio
    signal added after the research review (arXiv:2005.06610) found trade
    COUNT, not volume, is the strongest real-time pump predictor.
    """
    print("--- rush_order_signal ---")
    random.seed(2)
    n = config.VOLUME_LOOKBACK_CANDLES

    def row(open_time, close, volume, num_trades, taker_buy_vol):
        return [open_time, 1, 1, 1, close, volume, num_trades, taker_buy_vol]

    # deterministic trade-count pattern (48,50,52,48,50,52,...) — mean is
    # exactly 50 by construction, with small nonzero variance (avoids the
    # zero-variance guard) but no dependence on shared `random` state, so
    # the "at the mean -> no signal" case below is exact, not probabilistic
    baseline = [row(i, 1, 100, 50 + (i % 3 - 1) * 2, 50) for i in range(n)]

    # a genuine trade-count spike, taker-buy dominated -> bullish
    spike_buy = baseline + [row(n, 1.1, 600, 400, 350)]
    result = se.rush_order_signal(spike_buy)
    check("trade-count spike returns a signal", result is not None)
    if result:
        score, raw = result
        check("spike z-score is large and positive", raw["trade_count_z"] > 3, str(raw))
        check("spike score is in [0,1]", 0 <= score <= 1, f"score={score}")
        check("taker-buy ratio correctly read as buy-dominated",
              raw["taker_buy_ratio"] > 0.5, str(raw))

    # same trade count spike, taker-SELL dominated -> should still fire, ratio < 0.5
    spike_sell = baseline + [row(n, 0.9, 600, 400, 50)]
    result_sell = se.rush_order_signal(spike_sell)
    check("sell-dominated spike still returns a signal with score",
          result_sell is not None and result_sell[0] > 0)
    if result_sell:
        check("taker-buy ratio correctly read as sell-dominated",
              result_sell[1]["taker_buy_ratio"] < 0.5, str(result_sell[1]))

    # a volume spike with a trade count AT the baseline mean (z=0, few large
    # trades like wash trading, not a crowd) must NOT fire — this is the
    # whole point of the signal
    normal_count = baseline + [row(n, 1.0, 600, 50, 300)]
    check("volume spike with at-mean trade count does not fire",
          se.rush_order_signal(normal_count) is None)

    check("too-short input returns None", se.rush_order_signal(spike_buy[:3]) is None)
    check("empty input returns None", se.rush_order_signal([]) is None)
    check("None input returns None", se.rush_order_signal(None) is None)

    # rows shaped like unified fetch_ohlcv (6 fields, no trade-count/taker
    # columns) must degrade to None, not crash with an IndexError
    unified_shaped = [[i, 1, 1, 1, 1, 100] for i in range(n + 1)]
    check("6-field unified-shaped rows return None instead of crashing",
          se.rush_order_signal(unified_shaped) is None)

    flat_trades = [row(i, 1, 100, 50, 50) for i in range(n)] + [row(n, 1, 600, 50, 300)]
    check("zero-variance trade-count baseline returns None (no div-by-zero)",
          se.rush_order_signal(flat_trades) is None)


def test_fetch_ohlcv_raw_cleaning():
    """data_fetcher.fetch_ohlcv_raw's row-cleaning — Binance returns every kline field as a string."""
    print("--- data_fetcher.fetch_ohlcv_raw (row cleaning) ---")
    import sys as _sys, types as _types
    if "ccxt" not in _sys.modules:
        _fake = _types.ModuleType("ccxt")
        _sys.modules["ccxt"] = _fake
    import data_fetcher as df

    class FakeMarketExchange:
        def market(self, symbol):
            return {"id": symbol.replace("/", "")}

        def publicGetKlines(self, params):
            # Binance's real klines response: every field is a STRING
            return [
                ["1700000000000", "1.0", "1.1", "0.9", "1.05", "1000",
                 "1700000059999", "1050.0", "42", "600.0", "630.0", "0"],
            ]

    result = df.fetch_ohlcv_raw(FakeMarketExchange(), "BTC/USDT", "1h", 1)
    check("raw klines produce exactly one cleaned row", result is not None and len(result) == 1, str(result))
    if result:
        r = result[0]
        check("timestamp cast to int", r[0] == 1700000000000 and isinstance(r[0], int))
        check("close price cast to float", r[4] == 1.05)
        check("volume cast to float", r[5] == 1000.0)
        check("trade count cast to int", r[6] == 42 and isinstance(r[6], int))
        check("taker-buy volume cast to float", r[7] == 600.0)

    class BrokenExchange:
        def market(self, symbol):
            raise KeyError("symbol not found")

    check("a market() failure returns None instead of crashing",
          df.fetch_ohlcv_raw(BrokenExchange(), "BTC/USDT", "1h", 1) is None)

    class MalformedRowExchange:
        def market(self, symbol):
            return {"id": "BTCUSDT"}

        def publicGetKlines(self, params):
            return [["not", "a", "valid", "row"]]  # too short, will IndexError internally

    check("a malformed row is skipped rather than crashing the whole fetch",
          df.fetch_ohlcv_raw(MalformedRowExchange(), "BTC/USDT", "1h", 1) is None)


def test_order_book_imbalance_signal():
    print("--- order_book_imbalance_signal ---")
    buy_heavy = {"bids": [[1, 100]], "asks": [[1, 10]]}
    result = se.order_book_imbalance_signal(buy_heavy)
    check("buy-heavy book returns positive imbalance", result is not None and result[1] > 0, str(result))

    sell_heavy = {"bids": [[1, 10]], "asks": [[1, 100]]}
    result2 = se.order_book_imbalance_signal(sell_heavy)
    check("sell-heavy book returns negative imbalance", result2 is not None and result2[1] < 0, str(result2))

    check("empty book returns None", se.order_book_imbalance_signal({}) is None)
    check("None book returns None", se.order_book_imbalance_signal(None) is None)


def test_oi_change_signal():
    print("--- oi_change_signal ---")
    result = se.oi_change_signal(1100, 1000)
    check("10% OI increase detected", result is not None and abs(result[1] - 10.0) < 0.01, str(result))
    check("None oi_now returns None", se.oi_change_signal(None, 1000) is None)
    check("None oi_before returns None", se.oi_change_signal(1000, None) is None)
    check("zero oi_before returns None (no div-by-zero)", se.oi_change_signal(1000, 0) is None)


def test_funding_rate_signal():
    print("--- funding_rate_signal ---")
    result = se.funding_rate_signal(config.FUNDING_RATE_EXTREME * 2)
    check("extreme funding rate scores at cap", result is not None and result[0] == 1.0, str(result))
    check("None funding returns None", se.funding_rate_signal(None) is None)


def test_candle_price_change_pct():
    print("--- candle_price_change_pct ---")
    ohlcv = [[0, 100, 110, 95, 105, 1000]]
    pct = se.candle_price_change_pct(ohlcv)
    check("5% price gain computed correctly", abs(pct - 5.0) < 0.001, f"pct={pct}")
    check("None ohlcv returns None", se.candle_price_change_pct(None) is None)
    check("empty ohlcv returns None", se.candle_price_change_pct([]) is None)


def test_is_wash_trading_suspected():
    print("--- is_wash_trading_suspected ---")
    check("big volume + no price move = wash suspected", se.is_wash_trading_suspected(3.0, 0.2) is True)
    check("big volume + real price move = not wash", se.is_wash_trading_suspected(3.0, 5.0) is False)
    check("normal volume = not wash regardless of price", se.is_wash_trading_suspected(1.0, 0.1) is False)
    check("None volume_z = not wash (safe default)", se.is_wash_trading_suspected(None, 0.1) is False)
    check("None price_change = not wash (safe default)", se.is_wash_trading_suspected(3.0, None) is False)


def test_external_signal():
    print("--- external_signal ---")
    gainers = [{"code": "PEPE", "change_24h_pct": 15.0, "volume_btc": 300}]
    losers = [{"code": "XRP", "change_24h_pct": -12.0, "volume_btc": 250}]
    rapid = {"ARB": {"side": "DUMP", "change_detected": 9, "exchange": "binance"}}

    r1 = se.external_signal("PEPE", gainers, losers, rapid)
    check("LCW gainer hit has positive bias", r1 is not None and r1[1]["bias"] > 0, str(r1))

    r2 = se.external_signal("XRP", gainers, losers, rapid)
    check("LCW loser hit has negative bias", r2 is not None and r2[1]["bias"] < 0, str(r2))

    r3 = se.external_signal("ARB", gainers, losers, rapid)
    check("CryptoMeter DUMP hit has negative bias", r3 is not None and r3[1]["bias"] < 0, str(r3))

    r4 = se.external_signal("NOTHING", gainers, losers, rapid)
    check("no hit anywhere returns None", r4 is None)


def test_compute_market_funding_bias():
    print("--- compute_market_funding_bias ---")
    r1 = se.compute_market_funding_bias([0.0001] * 5)
    check("too few samples -> NEUTRAL", r1["bias"] == "NEUTRAL" and r1["sample_size"] == 5, str(r1))

    crowded_long = [0.001] * 10 + [0.0001] * 30
    r2 = se.compute_market_funding_bias(crowded_long)
    check("crowded longs -> DUMP_CASCADE_RISK", r2["bias"] == "DUMP_CASCADE_RISK", str(r2))

    crowded_short = [-0.001] * 10 + [0.0001] * 30
    r3 = se.compute_market_funding_bias(crowded_short)
    check("crowded shorts -> PUMP_SQUEEZE_RISK", r3["bias"] == "PUMP_SQUEEZE_RISK", str(r3))

    balanced = [0.0001] * 40
    r4 = se.compute_market_funding_bias(balanced)
    check("balanced funding -> NEUTRAL", r4["bias"] == "NEUTRAL", str(r4))

    with_nones = [None] * 15 + [0.0001] * 10
    r5 = se.compute_market_funding_bias(with_nones)
    check("None values filtered from sample count", r5["sample_size"] == 10, str(r5))


def test_classify_confluence():
    print("--- classify_confluence ---")
    strong = {
        "volume_zscore": (0.9, 3.5),
        "external_signal": (0.8, {"text": "x", "bias": 1}),
        "whale_flow": (0.7, 600000),
    }
    check("strong + confirmed cross-exchange -> CONFIRMED",
          se.classify_confluence(strong, False, "CONFIRMED") == "CONFIRMED")
    check("wash-trading vetoes even strong signals",
          se.classify_confluence(strong, True, "CONFIRMED") == "LOW_CONFIDENCE")

    weak = {"volume_zscore": (0.5, 2.0)}
    check("weak signal, no market context -> WATCH (backward compatible)",
          se.classify_confluence(weak, False, "SKIPPED") == "WATCH")
    check("weak signal + agreeing market bias -> promoted to CONFIRMED",
          se.classify_confluence(weak, False, "SKIPPED", direction="DUMP WATCH",
                                  market_bias="DUMP_CASCADE_RISK") == "CONFIRMED")
    check("weak signal + disagreeing market bias -> stays WATCH",
          se.classify_confluence(weak, False, "SKIPPED", direction="PUMP WATCH",
                                  market_bias="DUMP_CASCADE_RISK") == "WATCH")
    check("weak signal + NEUTRAL market bias -> stays WATCH",
          se.classify_confluence(weak, False, "SKIPPED", direction="DUMP WATCH",
                                  market_bias="NEUTRAL") == "WATCH")
    check("wash-trading veto beats market-bias promotion",
          se.classify_confluence(weak, True, "SKIPPED", direction="DUMP WATCH",
                                  market_bias="DUMP_CASCADE_RISK") == "LOW_CONFIDENCE")
    check("CONTRADICTED cross-exchange blocks market-bias promotion",
          se.classify_confluence(weak, False, "CONTRADICTED", direction="DUMP WATCH",
                                  market_bias="DUMP_CASCADE_RISK") == "WATCH")
    check("already-CONFIRMED unaffected by disagreeing market bias",
          se.classify_confluence(strong, False, "CONFIRMED", direction="PUMP WATCH",
                                  market_bias="DUMP_CASCADE_RISK") == "CONFIRMED")

    empty = {}
    check("no signals at all -> LOW_CONFIDENCE", se.classify_confluence(empty, False, "SKIPPED") == "LOW_CONFIDENCE")


def test_composite_score_and_direction():
    print("--- composite_score / direction_label ---")
    signals = {
        "volume_zscore": (0.8, 3.2),
        "order_book_imbalance": (0.5, 0.2),
        "whale_flow": (0.9, 750000),
        "social_signal": (0.75, {"text": "x", "bias": 3}),
    }
    score = se.composite_score(signals)
    check("composite score is in (0,1]", 0 < score <= 1, f"score={score}")
    check("bullish signals -> PUMP WATCH direction", se.direction_label(signals) == "PUMP WATCH")

    bearish = {
        "order_book_imbalance": (0.4, -0.15),
        "whale_flow": (0.6, -300000),
        "social_signal": (0.5, {"text": "x", "bias": -1}),
    }
    check("bearish signals -> DUMP WATCH direction", se.direction_label(bearish) == "DUMP WATCH")
    check("empty signals -> composite score is 0", se.composite_score({}) == 0.0)
    check("empty signals -> NEUTRAL direction", se.direction_label({}) == "NEUTRAL")


def test_config_validate():
    """
    Runs config.validate() for real rather than just reading it — this is
    exactly the kind of function that looks fine on inspection but breaks
    on its first real call (a NameError from a variable rename was caught
    this way during Phase 8 and would have crashed main.py on startup).
    """
    print("--- config.validate() ---")
    original_weights = dict(config.WEIGHTS)
    try:
        config.WEIGHTS = dict(original_weights)
        check("shipped WEIGHTS passes validate() with no problems", config.validate() is True)

        config.WEIGHTS = {"volume_zscore": 0.5, "whale_flow": 0.9}
        check("bad weight sum is caught", config.validate() is False)

        config.WEIGHTS = {"volume_zscore": 0.5, "made_up_key": 0.5}
        check("unknown weight key is caught", config.validate() is False)
    finally:
        config.WEIGHTS = original_weights


def test_retry_helper():
    """data_fetcher._retry — used for the two calls whose failure would waste a whole scan cycle."""
    print("--- data_fetcher._retry ---")
    import data_fetcher as df

    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("simulated network blip")
        return "success"

    result = df._retry(flaky, max_attempts=3, base_delay=0.01, label="test-flaky")
    check("retry succeeds after transient failures", result == "success" and calls["n"] == 3, f"n={calls['n']}")

    calls2 = {"n": 0}

    def always_fails():
        calls2["n"] += 1
        raise TimeoutError("persistent failure")

    raised = False
    try:
        df._retry(always_fails, max_attempts=3, base_delay=0.01, label="test-always-fails")
    except TimeoutError:
        raised = True
    check("retry raises the original exception type after exhausting attempts",
          raised and calls2["n"] == 3, f"n={calls2['n']}")

    calls3 = {"n": 0}

    def immediate_success():
        calls3["n"] += 1
        return "ok"

    result3 = df._retry(immediate_success, max_attempts=3, base_delay=0.01, label="test-immediate")
    check("retry adds no overhead on first-try success", result3 == "ok" and calls3["n"] == 1)


def test_whale_alert_parser():
    """
    onchain_source.parse_whale_alert_message — every case here is a REAL
    message captured from the live t.me/s/whale_alert_io channel while
    building this, not invented text, so this locks in the exact format
    Whale Alert actually posts in.
    """
    print("--- onchain_source.parse_whale_alert_message ---")
    import onchain_source as onchain

    known = {"BTC", "ETH", "USDC", "USDT", "SOL", "HYPE"}

    cases = [
        ("1,825 $BTC (153,215,249 USD) transferred from unknown wallet to unknown wallet",
         None, "neither side tagged as exchange"),
        ("250,000,000 $USDC (250,000,375 USD) minted at USDC Treasury",
         None, "mint event, not a transfer"),
        ("187,000,000 $USDC (186,992,333 USD) transferred from #Aave to Unknown Whale 1",
         ("USDC", 186992333.0, "OUTFLOW"), "tagged exchange -> untagged"),
        ("187,000,000 $USDC (186,974,007 USD) transferred from Unknown Whale 1 to #Aave",
         ("USDC", 186974007.0, "INFLOW"), "untagged -> tagged exchange"),
        ("500,000 $SOL (60,474,006 USD) transferred from unknown wallet to #Binance",
         ("SOL", 60474006.0, "INFLOW"), "unknown wallet -> tagged exchange"),
        ("2,400 $BTC (201,828,855 USD) transferred from unknown wallet to Coinbase Institutional",
         None, "unlabeled institutional entity, not hashtagged"),
        ("616 $BTC (51,869,430 USD) transferred from Coinbase Institutional to unknown wallet",
         None, "unlabeled institutional entity on the from-side"),
        ("160,000,000 $USDT (159,954,400 USD) transferred from #Bitfinex to Tether Treasury",
         ("USDT", 159954400.0, "OUTFLOW"), "tagged exchange -> unlabeled treasury"),
        ("1,698 $BTC (142,588,803 USD) transferred from #Kraken to unknown wallet",
         ("BTC", 142588803.0, "OUTFLOW"), "tagged exchange -> unknown wallet"),
        ("256,221 $HYPE (23,577,850 USD) transferred from #Kinetiq to unknown wallet",
         ("HYPE", 23577850.0, "OUTFLOW"), "tagged protocol -> unknown wallet"),
    ]

    for text, expected, label in cases:
        result = onchain.parse_whale_alert_message(text, known)
        check(f"whale alert parse: {label}", result == expected, f"got={result} expected={expected}")

    check("unknown ticker not in known_symbols is skipped",
          onchain.parse_whale_alert_message(
              "1,000 $NOTATRACKEDCOIN (500,000 USD) transferred from unknown wallet to #Binance", known
          ) is None)
    check("empty text returns None", onchain.parse_whale_alert_message("", known) is None)
    check("None text returns None", onchain.parse_whale_alert_message(None, known) is None)


def test_solscan_flows():
    """
    onchain_source._get_solscan_flows — written defensively since the exact
    response schema isn't independently confirmed (see the CONFIDENCE NOTE
    in config.py). These tests exercise several plausible response shapes
    to prove the defensive fallbacks actually work, not just that they
    exist in the source.
    """
    print("--- onchain_source._get_solscan_flows (defensive parsing) ---")
    import onchain_source as onchain

    original_get = onchain.requests.get
    original_key = config.SOLSCAN_API_KEY
    original_window = config.SOLSCAN_WINDOW_MINUTES
    config.SOLSCAN_API_KEY = "test-key"
    config.SOLSCAN_WINDOW_MINUTES = 999999999  # keep any far-future fake timestamp in-window

    class FakeResp:
        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            pass

        def json(self):
            return self._data

    try:
        onchain.requests.get = lambda url, headers=None, params=None, timeout=None: FakeResp({"data": [
            {"block_time": 9999999999, "token_symbol": "SOL", "value": 700000,
             "from_address": params["address"], "to_address": "somewhere_else"},
        ]})
        result_a = onchain._get_solscan_flows({"SOL"})
        check("solscan scenario A (token_symbol/value/from_address) -> outflow positive",
              result_a.get("SOL", 0) > 0, str(result_a))

        onchain.requests.get = lambda url, headers=None, params=None, timeout=None: FakeResp([
            {"blockTime": 9999999999, "symbol": "SOL", "amount_usd": 800000, "flow": "in"},
        ])
        result_b = onchain._get_solscan_flows({"SOL"})
        check("solscan scenario B (bare list, symbol/amount_usd/flow=in) -> inflow negative",
              result_b.get("SOL", 0) < 0, str(result_b))

        onchain.requests.get = lambda url, headers=None, params=None, timeout=None: FakeResp(
            {"success": False, "message": "invalid API key"}
        )
        result_c = onchain._get_solscan_flows({"SOL"})
        check("solscan API error -> empty dict, no crash", result_c == {})

        onchain.requests.get = lambda url, headers=None, params=None, timeout=None: FakeResp({"data": [
            {"block_time": 9999999999, "token_symbol": "SOMESCAMTOKEN", "value": 999999, "flow": "out"},
        ]})
        result_d = onchain._get_solscan_flows({"SOL"})
        check("solscan untracked symbol -> skipped", result_d == {})

        onchain.requests.get = lambda url, headers=None, params=None, timeout=None: FakeResp({"data": [
            {"token_symbol": "SOL", "value": 700000, "flow": "out"},  # no timestamp at all
        ]})
        result_e = onchain._get_solscan_flows({"SOL"})
        check("solscan missing timestamp -> skipped gracefully, no crash", result_e == {})
    finally:
        onchain.requests.get = original_get
        config.SOLSCAN_API_KEY = original_key
        config.SOLSCAN_WINDOW_MINUTES = original_window


def test_whale_flows_merge():
    """onchain_source.get_whale_flows — merges 3 independent sources into one net-flow-per-symbol dict."""
    print("--- onchain_source.get_whale_flows (3-source merge) ---")
    import onchain_source as onchain

    original_etherscan = onchain._get_etherscan_flows
    original_whale_alert = onchain._get_whale_alert_flows
    original_solscan = onchain._get_solscan_flows

    try:
        # each source alone is below WHALE_MIN_TRANSFER_USD (default $50,000), combined crosses it
        onchain._get_etherscan_flows = lambda price_map: {"PEPE": 30000}
        onchain._get_whale_alert_flows = lambda known_symbols: {"PEPE": 30000}
        onchain._get_solscan_flows = lambda known_symbols: {"SOL": -60000}

        flows = onchain.get_whale_flows({"PEPE": 0.001}, {"PEPE", "SOL"})
        check("merge sums contributions from all 3 sources",
              "PEPE" in flows and abs(flows["PEPE"][1] - 60000) < 0.01, str(flows))
        check("a single source already above threshold also appears",
              "SOL" in flows and flows["SOL"][1] == -60000, str(flows))

        onchain._get_etherscan_flows = lambda price_map: {"BTC": 30000}
        onchain._get_whale_alert_flows = lambda known_symbols: {}
        onchain._get_solscan_flows = lambda known_symbols: {}
        flows2 = onchain.get_whale_flows({}, {"BTC"})
        check("a single sub-threshold contribution alone is excluded", "BTC" not in flows2, str(flows2))
    finally:
        onchain._get_etherscan_flows = original_etherscan
        onchain._get_whale_alert_flows = original_whale_alert
        onchain._get_solscan_flows = original_solscan


# ======================================================================
# storage.py — SQLite persistence
# ======================================================================

def test_storage():
    print("\n--- storage.py ---")
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    storage.init_db()

    import time
    base_ts = int(time.time()) - 3600 * 30
    scan_ids = [storage.start_scan(f"scan-{i}", base_ts + i * 900) for i in range(6)]

    # OI round-trip
    check("OI starts as None for unknown symbol", storage.get_oi("BTC") is None)
    storage.set_oi("BTC", 1000.0, scan_ids[0])
    storage.set_oi("BTC", 1080.0, scan_ids[1])
    check("OI round-trip returns latest value", storage.get_oi("BTC") == 1080.0)

    # Reddit zero-padded history
    storage.record_reddit_mentions(scan_ids[0], {"PEPE": 5})
    storage.record_reddit_mentions(scan_ids[2], {"PEPE": 4})
    storage.record_reddit_mentions(scan_ids[4], {"PEPE": 6})
    history = storage.get_reddit_mention_history("PEPE", scan_ids[5], lookback=10)
    check("Reddit history zero-pads unmentioned scans", history == [5, 0, 4, 0, 6], str(history))

    # symbol_snapshot + price_snapshot + Phase 7 fields, round-trip
    row_pump = {"symbol": "PEPE/USDT", "price": 0.0010, "score": 0.85, "direction": "PUMP WATCH",
                "volume_z": 3.1, "ob_imbalance": 0.3, "oi_change_pct": None, "funding_rate": None,
                "external": "LCW +12%", "whale_net_flow_usd": 600000, "social": "CG trending #2",
                "confluence": "CONFIRMED", "wash_trading_flag": 0, "cross_exchange_status": "CONFIRMED"}
    storage.record_symbol_snapshot(scan_ids[0], row_pump)
    storage.record_price_snapshots(scan_ids[0], {"PEPE": 0.0010, "BTC": 65000})

    row_dump = {"symbol": "XRP/USDT", "price": 2.00, "score": 0.75, "direction": "DUMP WATCH",
                "volume_z": 2.8, "ob_imbalance": -0.3, "oi_change_pct": -9.0, "funding_rate": -0.001,
                "external": "LCW -15%", "whale_net_flow_usd": -700000, "social": "",
                "confluence": "LOW_CONFIDENCE", "wash_trading_flag": 1, "cross_exchange_status": "CONTRADICTED"}
    storage.record_symbol_snapshot(scan_ids[0], row_dump)
    storage.record_price_snapshots(scan_ids[0], {"XRP": 2.00})

    rows = storage.get_scan_rows(scan_ids[0])
    by_symbol = {r["symbol"]: r for r in rows}
    check("symbol_snapshot normalizes PEPE/USDT -> PEPE", "PEPE" in by_symbol, str(list(by_symbol)))
    check("Phase 7 fields round-trip correctly",
          by_symbol["PEPE"]["confluence"] == "CONFIRMED" and by_symbol["XRP"]["wash_trading_flag"] == 1)

    # market_context round-trip
    storage.record_market_context(scan_ids[0], 25.0, 3.0, "DUMP_CASCADE_RISK")
    ctx = storage.get_latest_market_context()
    check("market_context round-trips", ctx is not None and ctx["bias"] == "DUMP_CASCADE_RISK", str(ctx))

    # backtest: forward return calculation, direction filter, confluence filter,
    # and correct exclusion of signals too recent to have a forward price yet
    future_scan = storage.start_scan("future", base_ts + 25 * 3600)
    storage.record_price_snapshots(future_scan, {"PEPE": 0.0016, "XRP": 1.80})

    results = storage.backtest(min_score=0.5, forward_hours=24)
    by_sym = {r["symbol"]: r for r in results}
    check("backtest computes PEPE forward return correctly",
          "PEPE" in by_sym and abs(by_sym["PEPE"]["forward_return_pct"] - 60.0) < 0.01, str(by_sym.get("PEPE")))
    check("backtest computes XRP forward return correctly",
          "XRP" in by_sym and abs(by_sym["XRP"]["forward_return_pct"] - (-10.0)) < 0.01, str(by_sym.get("XRP")))

    pump_only = storage.backtest(min_score=0.5, forward_hours=24, direction="PUMP WATCH")
    check("backtest direction filter works", len(pump_only) == 1 and pump_only[0]["symbol"] == "PEPE")

    confirmed_only = storage.backtest(min_score=0.5, forward_hours=24, confluence="CONFIRMED")
    check("backtest confluence filter works", len(confirmed_only) == 1 and confirmed_only[0]["symbol"] == "PEPE")

    recent_scan = storage.start_scan("recent", int(time.time()) - 3600)
    storage.record_symbol_snapshot(recent_scan, {
        "symbol": "DOGE/USDT", "price": 0.5, "score": 0.9, "direction": "PUMP WATCH",
        "volume_z": None, "ob_imbalance": None, "oi_change_pct": None, "funding_rate": None,
        "external": "", "whale_net_flow_usd": None, "social": "",
        "confluence": "WATCH", "wash_trading_flag": 0, "cross_exchange_status": "SKIPPED",
    })
    storage.record_price_snapshots(recent_scan, {"DOGE": 0.5})
    results2 = storage.backtest(min_score=0.5, forward_hours=24)
    check("backtest excludes signals too recent for a forward price",
          "DOGE" not in [r["symbol"] for r in results2])

    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)


def test_factor_lift():
    """
    storage.factor_lift — verified against a hand-built dataset where every
    expected number is worked out by hand below, so this checks the MATH
    (win definition, edge sign for DUMP calls, applicable-vs-not grouping),
    not merely that the function runs.
    """
    print("\n--- storage.factor_lift ---")
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)
    storage.init_db()

    import time
    base_ts = int(time.time()) - 3600 * 60
    entry_scan = storage.start_scan("entry", base_ts)
    future_scan = storage.start_scan("future", base_ts + 25 * 3600)

    def call(sym, direction, whale, fwd_price, external="", score=0.8):
        storage.record_symbol_snapshot(entry_scan, {
            "symbol": f"{sym}/USDT", "price": 100.0, "score": score, "direction": direction,
            "volume_z": None, "ob_imbalance": None, "oi_change_pct": None, "funding_rate": None,
            "external": external, "whale_net_flow_usd": whale, "social": "",
            "confluence": "WATCH", "wash_trading_flag": 0, "cross_exchange_status": "SKIPPED",
        })
        storage.record_price_snapshots(entry_scan, {sym: 100.0})
        storage.record_price_snapshots(future_scan, {sym: fwd_price})

    # PUMP calls: edge = forward return. DUMP calls: edge = -forward return.
    call("AAA", "PUMP WATCH", +1, 110)   # whale agrees,    edge +10 -> win
    call("BBB", "PUMP WATCH", +1, 105)   # whale agrees,    edge +5  -> win
    call("CCC", "PUMP WATCH", -1, 95)    # whale DISagrees, edge -5  -> loss
    call("DDD", "PUMP WATCH", -1, 90)    # whale DISagrees, edge -10 -> loss
    call("EEE", "DUMP WATCH", -1, 90)    # dump + outflow sign agrees, price fell -> edge +10 -> win
    call("FFF", "DUMP WATCH", +1, 110)   # dump + whale disagrees, price rose -> edge -10 -> loss
    call("GGG", "PUMP WATCH", None, 102, external="LCW +9%")  # no whale, edge +2 -> win
    call("HHH", "PUMP WATCH", None, 98)                        # no whale, edge -2 -> loss
    call("NNN", "NEUTRAL", +1, 200)      # NEUTRAL makes no call -> must be excluded entirely

    # a call too recent to have a forward price -> must be excluded
    recent_scan = storage.start_scan("recent", int(time.time()) - 3600)
    storage.record_symbol_snapshot(recent_scan, {
        "symbol": "RRR/USDT", "price": 100.0, "score": 0.9, "direction": "PUMP WATCH",
        "volume_z": None, "ob_imbalance": None, "oi_change_pct": None, "funding_rate": None,
        "external": "", "whale_net_flow_usd": +1, "social": "",
        "confluence": "WATCH", "wash_trading_flag": 0, "cross_exchange_status": "SKIPPED",
    })
    storage.record_price_snapshots(recent_scan, {"RRR": 100.0})

    result = storage.factor_lift(min_score=0.5, forward_hours=24)
    by_name = {f["factor"]: f for f in result["factors"]}

    check("NEUTRAL rows and too-recent calls are excluded from the total",
          result["total_calls"] == 8, f"total_calls={result['total_calls']}")
    # baseline wins: AAA, BBB, EEE, GGG = 4 of 8
    check("baseline win rate is 4/8 = 50%", result["baseline"]["win_rate"] == 50.0, str(result["baseline"]))

    agree = by_name["whale flow agrees with the call"]
    check("whale-agrees group: AAA, BBB, EEE -> n=3, 100% wins, avg edge +8.33",
          agree["with"]["n"] == 3 and agree["with"]["win_rate"] == 100.0
          and abs(agree["with"]["avg_edge"] - 8.33) < 0.01, str(agree["with"]))
    check("whale-disagrees group: CCC, DDD, FFF -> n=3, 0% wins, avg edge -8.33",
          agree["without"]["n"] == 3 and agree["without"]["win_rate"] == 0.0
          and abs(agree["without"]["avg_edge"] - (-8.33)) < 0.01, str(agree["without"]))

    present = by_name["whale_flow present"]
    check("whale-present split is 6 with (3 wins) vs 2 without (1 win)",
          present["with"]["n"] == 6 and present["with"]["win_rate"] == 50.0
          and present["without"]["n"] == 2 and present["without"]["win_rate"] == 50.0, str(present))

    external = by_name["external_signal present"]
    check("external-present split is 1 with (GGG wins) vs 7 without (3 wins = 42.9%)",
          external["with"]["n"] == 1 and external["with"]["win_rate"] == 100.0
          and external["without"]["n"] == 7 and external["without"]["win_rate"] == 42.9, str(external))

    funding = by_name["funding rate agrees with the call"]
    check("a factor with no applicable calls yields empty groups, not a crash",
          funding["with"]["n"] == 0 and funding["without"]["n"] == 0
          and funding["with"]["win_rate"] is None, str(funding))

    empty = storage.factor_lift(min_score=0.99, forward_hours=24)
    check("no qualifying calls -> total 0 and an empty baseline, no crash",
          empty["total_calls"] == 0 and empty["baseline"]["n"] == 0)

    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)


# ======================================================================
# Full mocked end-to-end scan (main.scan_once)
# ======================================================================

def test_full_scan_integration():
    print("\n--- full scan integration (mocked exchanges/APIs) ---")
    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)

    import main
    import data_fetcher as df
    import livecoinwatch_source as lcw
    import cryptometer_source as cm
    import onchain_source as onchain
    import social_source as social

    storage.init_db()

    class FakeExchange:
        def __init__(self, markets=None):
            self.markets = markets or {}

        def load_markets(self):
            return self.markets

    symbols = ["PEPE/USDT", "XRP/USDT", "SOL/USDT"]
    price_map = {"PEPE": 0.001, "XRP": 2.0, "SOL": 150.0}
    random.seed(7)

    def fake_fetch_ohlcv_raw(exchange, symbol, timeframe, limit):
        base = symbol.split("/")[0]
        # [open_time, open, high, low, close, volume, num_trades, taker_buy_base_volume]
        # Trade count uses a deterministic pattern (mean exactly 50, small
        # nonzero variance) so XRP's "at the mean" case below is exact, not
        # dependent on where a shared random draw happens to land.
        baseline = [
            [i, 1, 1, 1, 1, 100 + random.uniform(-8, 8), 50 + (i % 3 - 1) * 2, 50]
            for i in range(limit - 1)
        ]
        if base == "PEPE":
            # real price move + volume AND a genuine crowd of separate buyers rushing in
            latest = [limit - 1, 1, 1.2, 0.9, 1.15, 600, 300, 500]
        elif base == "XRP":
            # same volume spike as PEPE, ~no price move, and trade count AT
            # the baseline mean (a handful of large wash trades, not a
            # crowd) -> wash-trading, no rush-order signal
            latest = [limit - 1, 1, 1.002, 0.998, 1.0005, 600, 50, 300]
        else:
            latest = [limit - 1, 1, 1.01, 0.99, 1.0, 102, 50, 51]  # unremarkable
        return baseline + [latest]

    df.get_spot_exchange = lambda: FakeExchange()
    df.get_futures_exchange = lambda: FakeExchange(markets={"PEPE/USDT": {}, "XRP/USDT": {}})
    df.get_usdt_symbols_and_prices = lambda spot: (symbols, dict(price_map))
    df.fetch_ohlcv_raw = fake_fetch_ohlcv_raw
    df.fetch_order_book = lambda ex, sym, depth: {"bids": [[1, 100]], "asks": [[1, 100]]}
    df.symbol_has_futures = lambda futures, sym: sym in futures.markets
    df.fetch_open_interest = lambda futures, sym: 1000.0
    df.fetch_funding_rate = lambda futures, sym: 0.001  # crowded-long market-wide
    df.check_cross_exchange_confirmation = lambda base, direction: (
        "CONFIRMED" if base == "PEPE" else "NOT_LISTED"
    )

    lcw.get_btc_volume_band_movers = lambda: ([{"code": "PEPE", "change_24h_pct": 20.0, "volume_btc": 300}], [])
    cm.get_rapid_movements = lambda: [{"pair": "PEPE-USD", "side": "PUMP", "change_detected": 18, "exchange": "binance"}]
    cm.get_market_trend = lambda: {"trend_score": 70, "buy_pressure": 60, "sell_pressure": 40}
    onchain.get_whale_flows = lambda price_map, known_symbols: {"PEPE": (0.8, 700000)}
    social.get_social_signals = lambda known_symbols, scan_id: {
        "PEPE": (0.9, {"text": "CG trending #1; Reddit 20x mentions", "bias": 2})
    }

    original_min_sample = config.MARKET_BIAS_MIN_SAMPLE
    original_share_threshold = config.MARKET_BIAS_SHARE_THRESHOLD_PCT
    config.MARKET_BIAS_MIN_SAMPLE = 2
    config.MARKET_BIAS_SHARE_THRESHOLD_PCT = 50.0

    try:
        top = main.scan_once()
    finally:
        config.MARKET_BIAS_MIN_SAMPLE = original_min_sample
        config.MARKET_BIAS_SHARE_THRESHOLD_PCT = original_share_threshold
        if os.path.exists(config.RESULTS_CSV_PATH):
            os.remove(config.RESULTS_CSV_PATH)

    by_symbol = {r["symbol"]: r for r in top}

    check("PEPE (genuine volume+price pump, confirmed elsewhere) -> CONFIRMED",
          by_symbol.get("PEPE/USDT", {}).get("confluence") == "CONFIRMED", str(by_symbol.get("PEPE/USDT")))
    check("PEPE has no wash-trading flag",
          by_symbol.get("PEPE/USDT", {}).get("wash_trading_flag") == 0)
    check("XRP (same volume spike, no price move) is flagged wash-trading",
          by_symbol.get("XRP/USDT", {}).get("wash_trading_flag") == 1, str(by_symbol.get("XRP/USDT")))
    check("XRP is demoted to LOW_CONFIDENCE despite a high raw score",
          by_symbol.get("XRP/USDT", {}).get("confluence") == "LOW_CONFIDENCE", str(by_symbol.get("XRP/USDT")))

    check("PEPE (many separate buyers) shows a rush-order trade-count spike",
          (by_symbol.get("PEPE/USDT", {}).get("rush_order_z") or 0) > 3,
          str(by_symbol.get("PEPE/USDT", {}).get("rush_order_z")))
    check("PEPE's rushed trades are taker-buy dominated (crowd buying, not selling)",
          (by_symbol.get("PEPE/USDT", {}).get("rush_taker_ratio") or 0) > 0.7,
          str(by_symbol.get("PEPE/USDT", {}).get("rush_taker_ratio")))
    check("XRP (few large wash trades, not a crowd) shows no rush-order spike",
          by_symbol.get("XRP/USDT", {}).get("rush_order_z") is None,
          str(by_symbol.get("XRP/USDT", {}).get("rush_order_z")))

    ctx = storage.get_latest_market_context()
    check("market-wide funding bias correctly read as DUMP_CASCADE_RISK",
          ctx is not None and ctx["bias"] == "DUMP_CASCADE_RISK", str(ctx))

    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)


# ======================================================================

def main_runner():
    tests = [
        test_volume_zscore_signal,
        test_rush_order_signal,
        test_fetch_ohlcv_raw_cleaning,
        test_order_book_imbalance_signal,
        test_oi_change_signal,
        test_funding_rate_signal,
        test_candle_price_change_pct,
        test_is_wash_trading_suspected,
        test_external_signal,
        test_compute_market_funding_bias,
        test_classify_confluence,
        test_composite_score_and_direction,
        test_config_validate,
        test_retry_helper,
        test_whale_alert_parser,
        test_solscan_flows,
        test_whale_flows_merge,
        test_storage,
        test_factor_lift,
        test_full_scan_integration,
    ]

    print("=" * 70)
    print("Pump/Dump Detector — automated test suite")
    print("=" * 70)

    crashed = []
    for test in tests:
        try:
            test()
        except Exception as e:
            crashed.append((test.__name__, e))
            print(f"  ✗ CRASHED: {test.__name__}: {e}")
            traceback.print_exc()

    print("\n" + "=" * 70)
    total = _passed + len(_failures)
    print(f"RESULT: {_passed}/{total} checks passed, {len(_failures)} failed, {len(crashed)} crashed")
    if _failures:
        print("\nFailed checks:")
        for f in _failures:
            print(f"  - {f}")
    if crashed:
        print("\nCrashed tests:")
        for name, e in crashed:
            print(f"  - {name}: {e}")
    print("=" * 70)

    if os.path.exists(TEST_DB_PATH):
        os.remove(TEST_DB_PATH)

    sys.exit(0 if (not _failures and not crashed) else 1)


if __name__ == "__main__":
    main_runner()
