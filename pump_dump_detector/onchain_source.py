"""
On-chain whale flow tracking, from three independent sources merged into
one net_flow_usd per symbol:

  1. Etherscan API V2 (Phase 3) — Ethereum + BSC, via KNOWN_EXCHANGE_WALLETS.
  2. Whale Alert's public Telegram channel (Phase 9) — BTC, ETH, XRP, USDT,
     TRX, and 100+ ERC-20 tokens, far broader coverage than our own two
     watched wallets, for free (see the note in _get_whale_alert_flows).
  3. Solscan (Phase 9) — Solana, via SOLANA_EXCHANGE_WALLETS, filling a real
     gap: Solana is one of the most pump-and-dump-prone ecosystems in
     crypto and this project previously had zero on-chain visibility there.

Each source is independently optional (missing config -> that source
contributes nothing, no crash) and all three feed the SAME net_flow_usd
accumulator per symbol, so `whale_flow` in signal_engine.py stays one
signal with one weight — richer coverage, not a new signal to re-tune.

    net_flow_usd = (USD moved OUT of a watched exchange) - (USD moved IN)

  Positive -> more left exchanges than entered -> accumulation / bullish
  Negative -> more entered than left -> distribution / bearish

Known limitation, stated plainly: even combined, these three sources are a
SAMPLE of whale activity (a handful of watched wallets + whatever Whale
Alert itself chooses to flag as large enough to post), not exhaustive
coverage of any chain. Treat whale_flow as one more independent vote, not
ground truth.
"""

import re
import time
import asyncio
import requests
import config

ETHERSCAN_V2_URL = "https://api.etherscan.io/v2/api"


def _fetch_token_transfers(chain_id, address):
    """Most recent ERC-20/BEP-20 transfers for one wallet on one chain."""
    params = {
        "chainid": chain_id,
        "module": "account",
        "action": "tokentx",
        "address": address,
        "page": 1,
        "offset": config.WHALE_MAX_TRANSFERS_PER_WALLET,
        "sort": "desc",
        "apikey": config.ETHERSCAN_API_KEY,
    }
    try:
        resp = requests.get(ETHERSCAN_V2_URL, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"  [warn] on-chain fetch failed for {address} (chain {chain_id}): {e}")
        return []

    # Etherscan returns status "0" with message "No transactions found" for
    # an empty (but valid) result — that's not an error, just nothing to report.
    if payload.get("status") != "1":
        if payload.get("message") != "No transactions found":
            print(f"  [warn] on-chain API error (chain {chain_id}): {payload.get('message')}")
        return []

    return payload.get("result", [])


def _get_etherscan_flows(price_map):
    """
    Phase 3. Pulls each watched wallet's most recent ERC-20/BEP-20
    transfers on each watched chain, keeps only ones inside
    WHALE_WINDOW_MINUTES, and returns {symbol: net_usd} using Etherscan's
    own `tokenSymbol` field — no contract-address lookup needed.
    """
    if not config.ETHERSCAN_API_KEY:
        return {}

    cutoff_ts = time.time() - (config.WHALE_WINDOW_MINUTES * 60)
    net = {}

    for wallet in config.KNOWN_EXCHANGE_WALLETS:
        address_lower = wallet["address"].lower()

        for chain in config.WHALE_CHAINS:
            transfers = _fetch_token_transfers(chain["chain_id"], wallet["address"])

            for tx in transfers:
                try:
                    ts = int(tx.get("timeStamp", 0))
                except (TypeError, ValueError):
                    continue
                if ts < cutoff_ts:
                    break  # sorted desc by time — everything after this is older

                symbol = (tx.get("tokenSymbol") or "").upper()
                price = price_map.get(symbol) if symbol else None
                if not symbol or not price:
                    continue  # no real market price on Binance -> likely spam token, skip

                try:
                    decimals = int(tx.get("tokenDecimal", 18))
                    raw_value = int(tx.get("value", 0))
                except (TypeError, ValueError):
                    continue

                amount = raw_value / (10 ** decimals)
                usd_value = amount * price
                if usd_value < config.WHALE_MIN_TRANSFER_USD:
                    continue  # this specific transfer is too small to count as "whale"

                to_addr = (tx.get("to") or "").lower()
                from_addr = (tx.get("from") or "").lower()

                if to_addr == address_lower:
                    net[symbol] = net.get(symbol, 0) - usd_value  # deposit INTO exchange
                elif from_addr == address_lower:
                    net[symbol] = net.get(symbol, 0) + usd_value  # withdrawal FROM exchange

    return net


# ---------------------------------------------------------- whale alert

# Matches Whale Alert's consistent post format, e.g.:
#   "🚨🚨🚨 2,400 $BTC (201,828,855 USD) transferred from unknown wallet to Coinbase Institutional"
#   "🚨 187,000,000 $USDC (186,992,333 USD) transferred from #Aave to Unknown Whale 1"
# Verified against live channel content before writing this regex, not guessed.
_WHALE_ALERT_RE = re.compile(
    r"[\d,]+\s+\$([A-Za-z0-9]+)\s+\(([\d,]+)\s*USD\)\s+transferred from\s+(.+?)\s+to\s+(.+?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def parse_whale_alert_message(text, known_symbols):
    """
    Parses one Whale Alert channel message. Returns (symbol, usd_value,
    direction) or None if the message doesn't match the transfer pattern,
    isn't a token we track, or isn't a clean exchange-in/exchange-out case.

    Direction relies on Whale Alert's OWN "#ExchangeName" hashtag
    convention — its posts tag the side that's a known exchange, e.g.
    "transferred from unknown wallet to #Kraken". Only hashtag-vs-not
    cases are used (one side tagged, the other not); wallet-to-wallet
    transfers (neither side tagged) and treasury/institutional transfers
    (unlabeled named entities like "Coinbase Institutional" or "Tether
    Treasury") are deliberately skipped rather than guessed at — those
    don't map cleanly onto the same "exchange in/out" interpretation as a
    hashtagged exchange does.
    """
    if not text:
        return None

    match = _WHALE_ALERT_RE.search(text)
    if not match:
        return None

    symbol = match.group(1).upper()
    if symbol not in known_symbols:
        return None

    try:
        usd_value = float(match.group(2).replace(",", ""))
    except ValueError:
        return None
    if usd_value < config.WHALE_MIN_TRANSFER_USD:
        return None

    from_entity = match.group(3).strip()
    to_entity = match.group(4).strip()
    from_is_exchange = from_entity.startswith("#")
    to_is_exchange = to_entity.startswith("#")

    if to_is_exchange and not from_is_exchange:
        return symbol, usd_value, "INFLOW"    # moving INTO a known exchange -> bearish lean
    if from_is_exchange and not to_is_exchange:
        return symbol, usd_value, "OUTFLOW"   # moving OUT of a known exchange -> bullish lean
    return None  # both or neither side is a labeled exchange — ambiguous, skip


async def _whale_alert_scan_async(known_symbols):
    from telethon import TelegramClient

    client = TelegramClient(
        config.TELEGRAM_SESSION_NAME,
        int(config.TELEGRAM_API_ID),
        config.TELEGRAM_API_HASH,
    )
    await client.start()

    cutoff = time.time() - config.WHALE_ALERT_WINDOW_MINUTES * 60
    net = {}

    try:
        entity = await client.get_entity(config.WHALE_ALERT_CHANNEL)
        async for message in client.iter_messages(entity, limit=config.WHALE_ALERT_MESSAGE_LIMIT):
            if message.date.timestamp() < cutoff:
                break  # newest-first — everything after this is older
            parsed = parse_whale_alert_message(message.message or "", known_symbols)
            if not parsed:
                continue
            symbol, usd_value, direction = parsed
            signed = usd_value if direction == "OUTFLOW" else -usd_value
            net[symbol] = net.get(symbol, 0) + signed
    except Exception as e:
        print(f"  [warn] Whale Alert channel fetch failed: {e}")
    finally:
        await client.disconnect()

    return net


def _get_whale_alert_flows(known_symbols):
    """
    Free alternative to Whale Alert's own API (no free tier — see the
    module docstring): reads the SAME alerts from their public Telegram
    channel instead. Reuses Phase 4's Telethon credentials; requires
    TELEGRAM_API_ID/HASH to already be set up (same one-time interactive
    login noted there).
    """
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        return {}

    try:
        import telethon  # noqa: F401  (import check only)
    except ImportError:
        print("  [warn] telethon not installed — run: pip install telethon")
        return {}

    try:
        return asyncio.run(_whale_alert_scan_async(known_symbols))
    except Exception as e:
        print(f"  [warn] Whale Alert scan failed: {e}")
        return {}


# ---------------------------------------------------------------- solscan

SOLSCAN_ACCOUNT_TRANSFER_PATH = "/account/transfer"


def _fetch_solscan_transfers(address):
    """
    See the CONFIDENCE NOTE on SOLSCAN_API_KEY in config.py: endpoint path,
    header name, and base URL are confirmed against several independent
    third-party examples, but not against Solscan's own docs page or a
    live call (sandbox network couldn't reach it). Response field names
    below are read defensively with multiple fallbacks for exactly that
    reason — if Solscan's real schema differs, this should degrade to
    "no data" rather than crash.
    """
    url = config.SOLSCAN_BASE_URL + SOLSCAN_ACCOUNT_TRANSFER_PATH
    headers = {"token": config.SOLSCAN_API_KEY}
    params = {"address": address, "page": 1, "page_size": config.SOLSCAN_MAX_TRANSFERS_PER_WALLET}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"  [warn] Solscan fetch failed for {address}: {e}")
        return []

    if isinstance(payload, dict) and payload.get("success") is False:
        print(f"  [warn] Solscan API error for {address}: {payload.get('message', payload)}")
        return []

    return payload.get("data", []) if isinstance(payload, dict) else (payload or [])


def _get_solscan_flows(known_symbols):
    """
    Phase 9. Same net-flow idea as Etherscan (Phase 3), for Solana's SPL
    tokens via SOLANA_EXCHANGE_WALLETS. Solscan's transfer records
    typically carry a token symbol and USD value directly (they track
    prices themselves), so — unlike the Etherscan path — this doesn't need
    our own price_map; several plausible field names are tried since the
    exact schema isn't independently confirmed (see config.py).
    """
    if not config.SOLSCAN_API_KEY:
        return {}

    cutoff_ts = time.time() - (config.SOLSCAN_WINDOW_MINUTES * 60)
    net = {}

    for wallet in config.SOLANA_EXCHANGE_WALLETS:
        address = wallet["address"]
        transfers = _fetch_solscan_transfers(address)

        for tx in transfers:
            if not isinstance(tx, dict):
                continue

            ts = tx.get("block_time") or tx.get("blockTime") or tx.get("time")
            try:
                ts = int(ts)
            except (TypeError, ValueError):
                continue
            if ts < cutoff_ts:
                break  # sorted desc by time (per docs) — everything after this is older

            symbol = (tx.get("token_symbol") or tx.get("symbol") or "").upper()
            if not symbol or symbol not in known_symbols:
                continue

            usd_value = tx.get("value") or tx.get("amount_usd") or tx.get("usd_value")
            try:
                usd_value = float(usd_value)
            except (TypeError, ValueError):
                continue
            if usd_value < config.WHALE_MIN_TRANSFER_USD:
                continue

            to_addr = tx.get("to_address") or tx.get("to") or ""
            from_addr = tx.get("from_address") or tx.get("from") or ""
            flow = (tx.get("flow") or "").lower()  # some Solscan endpoints label this directly

            is_inflow = flow == "in" or to_addr == address
            is_outflow = flow == "out" or from_addr == address

            if is_inflow and not is_outflow:
                net[symbol] = net.get(symbol, 0) - usd_value
            elif is_outflow and not is_inflow:
                net[symbol] = net.get(symbol, 0) + usd_value

    return net


# --------------------------------------------------------------- combine

def get_whale_flows(price_map, known_symbols):
    """
    Merges all three sources (Etherscan wallets, Whale Alert's Telegram
    channel, Solscan Solana wallets) into one {base_asset: (score, net_usd)}
    dict — the same (score, raw) shape every signal in signal_engine.py
    uses, so this drops straight into a symbol's signals dict as the single
    `whale_flow` signal. Each source contributes independently and any
    combination can be disabled (missing config -> contributes nothing).
    """
    net = {}

    for symbol, usd in _get_etherscan_flows(price_map).items():
        net[symbol] = net.get(symbol, 0) + usd
    for symbol, usd in _get_whale_alert_flows(known_symbols).items():
        net[symbol] = net.get(symbol, 0) + usd
    for symbol, usd in _get_solscan_flows(known_symbols).items():
        net[symbol] = net.get(symbol, 0) + usd

    flows = {}
    for symbol, net_usd in net.items():
        if abs(net_usd) < config.WHALE_MIN_TRANSFER_USD:
            continue  # combined signal too small even if individual sources cleared their own bars
        score = min(abs(net_usd) / config.WHALE_FLOW_THRESHOLD_USD, 1.0)
        flows[symbol] = (score, round(net_usd, 2))

    return flows
