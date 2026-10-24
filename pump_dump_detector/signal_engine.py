"""
Turns raw market data into normalized signals and a single composite score
per symbol. Each signal function returns (score_0_to_1, raw_value) or None
if it couldn't be computed, so signals can be combined with weights even
when some data (e.g. futures) isn't available for a given symbol.
"""

import statistics
import config


def volume_zscore_signal(ohlcv):
    """
    ohlcv: list of [timestamp, open, high, low, close, volume], oldest first.
    Compares the latest candle's volume to the mean/stdev of the preceding
    candles. Returns (normalized score, raw z-score).
    """
    if not ohlcv or len(ohlcv) < config.VOLUME_LOOKBACK_CANDLES + 1:
        return None

    volumes = [c[5] for c in ohlcv]
    baseline = volumes[-(config.VOLUME_LOOKBACK_CANDLES + 1):-1]
    latest = volumes[-1]

    mean = statistics.mean(baseline)
    stdev = statistics.pstdev(baseline)
    if stdev == 0:
        return None

    z = (latest - mean) / stdev
    score = max(0.0, z / config.VOLUME_ZSCORE_THRESHOLD)
    return min(score, 2.0) / 2.0, round(z, 2)


def rush_order_signal(ohlcv_raw):
    """
    "Rush orders" — trades that hit the market immediately rather than
    resting in the order book — are, per peer-reviewed research on
    real-time crypto pump-and-dump detection (Nizzoli, La Morgia et al.,
    ICCCN 2020, arXiv:2005.06610), the single strongest predictor of a
    pump's START: a classifier built mainly on this feature reached
    F1=92% and detected pumps within ~25 seconds, versus 60.5% F1 and
    ~30 minutes for prior price/volume-only approaches. This project had
    nothing like it before volume_zscore_signal sees total volume, which
    doesn't distinguish one whale placing one large resting order from a
    crowd of people all rush-buying at once — the actual signature of a
    coordinated pump call, and the one this function looks for instead.

    ohlcv_raw: rows from data_fetcher.fetch_ohlcv_raw — [open_time, open,
    high, low, close, volume, num_trades, taker_buy_base_volume]. Compares
    the latest candle's TRADE COUNT (not volume) to its own recent
    baseline, the same z-score approach as volume_zscore_signal, and reads
    the taker-buy ratio for direction.

    Returns (score 0-1, {"trade_count_z", "taker_buy_ratio"}) or None.
    """
    if not ohlcv_raw or len(ohlcv_raw) < config.VOLUME_LOOKBACK_CANDLES + 1:
        return None

    try:
        trade_counts = [row[6] for row in ohlcv_raw]
        volumes = [row[5] for row in ohlcv_raw]
        taker_buy_volumes = [row[7] for row in ohlcv_raw]
    except IndexError:
        return None  # rows don't carry the raw-klines fields (e.g. came from unified fetch_ohlcv)

    baseline = trade_counts[-(config.VOLUME_LOOKBACK_CANDLES + 1):-1]
    latest_count = trade_counts[-1]

    mean = statistics.mean(baseline)
    stdev = statistics.pstdev(baseline)
    if stdev == 0:
        return None

    z = (latest_count - mean) / stdev
    score = max(0.0, z / config.RUSH_ORDER_ZSCORE_THRESHOLD)
    score = min(score, 2.0) / 2.0
    if score == 0:
        return None

    latest_volume = volumes[-1]
    taker_buy_ratio = (taker_buy_volumes[-1] / latest_volume) if latest_volume else 0.5

    return score, {"trade_count_z": round(z, 2), "taker_buy_ratio": round(taker_buy_ratio, 3)}


def order_book_imbalance_signal(order_book):
    """
    Positive imbalance = more buy-side depth (bid volume > ask volume) -> pump lean.
    Negative imbalance = more sell-side depth -> dump lean.
    Returns (score 0-1, raw imbalance -1..1).
    """
    if not order_book:
        return None

    bids = order_book.get("bids", [])[: config.ORDER_BOOK_DEPTH]
    asks = order_book.get("asks", [])[: config.ORDER_BOOK_DEPTH]
    if not bids or not asks:
        return None

    bid_vol = sum(price * amount for price, amount in bids)
    ask_vol = sum(price * amount for price, amount in asks)
    total = bid_vol + ask_vol
    if total == 0:
        return None

    imbalance = (bid_vol - ask_vol) / total
    score = min(abs(imbalance) / config.IMBALANCE_THRESHOLD, 1.0)
    return score, round(imbalance, 3)


def oi_change_signal(oi_now, oi_before):
    """Percent change in open interest since the previous scan."""
    if oi_now is None or oi_before is None or oi_before == 0:
        return None

    pct_change = (oi_now - oi_before) / oi_before * 100
    score = min(abs(pct_change) / config.OI_CHANGE_THRESHOLD_PCT, 1.0)
    return score, round(pct_change, 2)


def funding_rate_signal(funding_rate):
    if funding_rate is None:
        return None

    score = min(abs(funding_rate) / config.FUNDING_RATE_EXTREME, 1.0)
    return score, funding_rate


def external_signal(base_asset, lcw_gainers, lcw_losers, rapid_movements_by_symbol):
    """
    Cross-checks a coin against LiveCoinWatch's BTC-volume-band movers and
    CryptoMeter's rapid-movements feed. Either source flagging the same
    asset as our own exchange-side signals is real cross-validation —
    it's independent of anything Binance itself reports.
    Returns (score 0-1, {"text": description, "bias": int}) or None if no hit.
    """
    hits = []
    bias = 0  # positive = pump-leaning, negative = dump-leaning

    for g in lcw_gainers:
        if g["code"] == base_asset:
            hits.append(f"LCW +{g['change_24h_pct']}% (vol {g['volume_btc']:.0f} BTC)")
            bias += 1
    for l in lcw_losers:
        if l["code"] == base_asset:
            hits.append(f"LCW {l['change_24h_pct']}% (vol {l['volume_btc']:.0f} BTC)")
            bias -= 1

    rm = rapid_movements_by_symbol.get(base_asset)
    if rm:
        side = str(rm.get("side", "")).upper()
        hits.append(f"CM {side} {rm.get('change_detected')}% on {rm.get('exchange')}")
        bias += 1 if side == "PUMP" else (-1 if side == "DUMP" else 0)

    if not hits:
        return None

    # Two independent sources agreeing on the same coin is meaningfully
    # stronger evidence than either one alone.
    score = min(0.5 * len(hits), 1.0)
    return score, {"text": "; ".join(hits), "bias": bias}


def candle_price_change_pct(ohlcv):
    """
    % price change of the latest candle (close vs open). Paired with
    volume_zscore_signal to catch wash trading: a genuine pump or dump
    moves price along with volume. Volume spiking with price barely moving
    is the classic wash-trading signature — round-trip trades that inflate
    volume stats without any real net buying or selling pressure.
    """
    if not ohlcv:
        return None
    latest = ohlcv[-1]
    open_price, close_price = latest[1], latest[4]
    if not open_price:
        return None
    return (close_price - open_price) / open_price * 100


def is_wash_trading_suspected(volume_z, price_change_pct):
    """True if volume spiked without a matching price move."""
    if volume_z is None or price_change_pct is None:
        return False
    return volume_z >= config.VOLUME_ZSCORE_THRESHOLD and abs(price_change_pct) < config.WASH_TRADING_MIN_PRICE_MOVE_PCT


def classify_confluence(signals, wash_trading_suspected, cross_exchange_status,
                         direction=None, market_bias=None):
    """
    A stricter, confluence-based tier layered ON TOP OF the composite score
    — modeled on how professional alert services cut false positives:
    require several independent things to agree at once, not just one
    weighted number crossing a line. This never changes `score` itself
    (so existing backtests stay comparable); it's an additional, pass/fail
    read of the same signals.

    CONFIRMED requires ALL of:
      - composite score >= config.CONFLUENCE_CONFIRMED_MIN_SCORE
      - at least config.CONFLUENCE_MIN_INDEPENDENT_FAMILIES of the three
        non-Binance signal families (external_signal, whale_flow,
        social_signal) present — agreement ACROSS independent sources,
        not just a lot of weight piled onto one of them
      - no wash-trading suspicion
      - cross-exchange status isn't CONTRADICTED (NOT_LISTED/SKIPPED stay
        neutral: a small-cap coin simply not being on Bybit/OKX isn't
        itself suspicious)

    WATCH: above the lower candidate floor but missing one of the above.
    LOW_CONFIDENCE: everything else, including anything wash-trading-flagged.

    direction/market_bias (both optional, Phase 7 follow-up): when given,
    a WATCH-tier symbol whose own direction agrees with the market-wide
    leverage bias (compute_market_funding_bias) is promoted to CONFIRMED —
    crowded market-wide leverage in the same direction as this specific
    call is real independent confirmation, not noise. This is intentionally
    ONE-WAY: it can only promote WATCH -> CONFIRMED, never demote CONFIRMED
    or override the wash-trading veto above, so callers that don't pass
    these two params (or pass None) get identical behavior to before.
    """
    if wash_trading_suspected:
        return "LOW_CONFIDENCE"

    score = composite_score(signals)
    independent_families = sum(
        1 for key in ("external_signal", "whale_flow", "social_signal") if signals.get(key)
    )

    if (score >= config.CONFLUENCE_CONFIRMED_MIN_SCORE
            and independent_families >= config.CONFLUENCE_MIN_INDEPENDENT_FAMILIES
            and cross_exchange_status != "CONTRADICTED"):
        return "CONFIRMED"

    if score >= config.CONFIRMATION_CANDIDATE_MIN_SCORE:
        market_agrees = (
            (direction == "DUMP WATCH" and market_bias == "DUMP_CASCADE_RISK") or
            (direction == "PUMP WATCH" and market_bias == "PUMP_SQUEEZE_RISK")
        )
        if market_agrees and cross_exchange_status != "CONTRADICTED":
            return "CONFIRMED"
        return "WATCH"

    return "LOW_CONFIDENCE"


def compute_market_funding_bias(funding_rates):
    """
    Aggregates every funding rate collected this scan (regardless of
    whether that symbol's own funding_rate_signal fired) into one
    market-wide read. See the MARKET_BIAS_* comment in config.py for why
    this exists instead of a liquidation feed, and the reasoning behind it.

    Returns {"long_share_pct", "short_share_pct", "bias", "sample_size"}.
    bias is one of "DUMP_CASCADE_RISK", "PUMP_SQUEEZE_RISK", "NEUTRAL".
    """
    valid = [r for r in funding_rates if r is not None]
    n = len(valid)
    if n < config.MARKET_BIAS_MIN_SAMPLE:
        return {"long_share_pct": None, "short_share_pct": None, "bias": "NEUTRAL", "sample_size": n}

    long_biased = sum(1 for r in valid if r >= config.FUNDING_RATE_EXTREME)
    short_biased = sum(1 for r in valid if r <= -config.FUNDING_RATE_EXTREME)
    long_pct = long_biased / n * 100
    short_pct = short_biased / n * 100

    if long_pct >= config.MARKET_BIAS_SHARE_THRESHOLD_PCT:
        bias = "DUMP_CASCADE_RISK"
    elif short_pct >= config.MARKET_BIAS_SHARE_THRESHOLD_PCT:
        bias = "PUMP_SQUEEZE_RISK"
    else:
        bias = "NEUTRAL"

    return {"long_share_pct": round(long_pct, 1), "short_share_pct": round(short_pct, 1),
            "bias": bias, "sample_size": n}


def composite_score(signals):
    """
    signals: dict like {"volume_zscore": (score, raw), "oi_change": (score, raw), ...}
    Missing signals are excluded and weights are renormalized over what IS present,
    so a symbol with no futures market isn't unfairly penalized for missing OI/funding.
    """
    total_weight = 0.0
    weighted_sum = 0.0

    for key, weight in config.WEIGHTS.items():
        if key in signals and signals[key] is not None:
            score, _raw = signals[key]
            weighted_sum += score * weight
            total_weight += weight

    if total_weight == 0:
        return 0.0

    return weighted_sum / total_weight


def direction_label(signals):
    """
    Heuristic direction based on order-book skew and funding-rate sign.
    This flags LEAN, not a guaranteed outcome — treat it as a filter, not a signal to trade blindly.
    """
    bullish_votes = 0
    bearish_votes = 0

    if signals.get("order_book_imbalance"):
        _, raw = signals["order_book_imbalance"]
        bullish_votes += raw > 0
        bearish_votes += raw < 0

    if signals.get("rush_order"):
        # taker_buy_ratio > 0.5 means aggressive BUYING dominated the
        # rushed trades in that candle -> bullish; < 0.5 -> aggressive
        # selling -> bearish. Same 1-vote weight as order book imbalance:
        # both read Binance's own trade tape, not an independent source.
        _, raw = signals["rush_order"]
        ratio = raw.get("taker_buy_ratio", 0.5)
        bullish_votes += ratio > 0.5
        bearish_votes += ratio < 0.5

    if signals.get("funding_rate"):
        _, raw = signals["funding_rate"]
        bullish_votes += raw > 0
        bearish_votes += raw < 0

    if signals.get("external_signal"):
        # External sources get 2 votes each way — they're independent
        # confirmation, not just another read of the same exchange data.
        _, raw = signals["external_signal"]
        bias = raw.get("bias", 0)
        bullish_votes += 2 if bias > 0 else 0
        bearish_votes += 2 if bias < 0 else 0

    if signals.get("whale_flow"):
        # net_flow_usd > 0 means more left watched exchange wallets than
        # entered -> accumulation -> bullish lean. Same 2-vote weight as
        # external_signal: this is on-chain ground truth, not exchange data.
        _, net_flow_usd = signals["whale_flow"]
        bullish_votes += 2 if net_flow_usd > 0 else 0
        bearish_votes += 2 if net_flow_usd < 0 else 0

    if signals.get("social_signal"):
        # Same treatment as external_signal — attention/sentiment from
        # outside Binance entirely, worth 2 votes.
        _, raw = signals["social_signal"]
        bias = raw.get("bias", 0)
        bullish_votes += 2 if bias > 0 else 0
        bearish_votes += 2 if bias < 0 else 0

    if bullish_votes > bearish_votes:
        return "PUMP WATCH"
    elif bearish_votes > bullish_votes:
        return "DUMP WATCH"
    return "NEUTRAL"
