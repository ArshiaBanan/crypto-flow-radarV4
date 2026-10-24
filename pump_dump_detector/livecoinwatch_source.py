"""
LiveCoinWatch integration.

Mirrors the manual workflow on livecoinwatch.com:
  1. Set the base "pair" to BTC (currency=BTC — prices/volumes denominated in BTC)
  2. Filter to coins with 24h volume between LCW_MIN_VOLUME_BTC and LCW_MAX_VOLUME_BTC
  3. Sort by 24h change and pull out the biggest gainers and losers

...done automatically, every scan, with no manual filtering.
"""

import requests
import config

LCW_BASE_URL = "https://api.livecoinwatch.com"
PAGE_SIZE = 100
MAX_PAGES = 30  # safety cap: 30 * 100 = 3000 coins scanned max per call


def _post(endpoint, payload):
    headers = {
        "content-type": "application/json",
        "x-api-key": config.LCW_API_KEY,
    }
    resp = requests.post(f"{LCW_BASE_URL}{endpoint}", json=payload, headers=headers, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_btc_volume_band_movers():
    """
    Returns (top_gainers, top_losers): two lists of dicts —
    {code, name, volume_btc, change_24h_pct} — each up to LCW_TOP_N long,
    restricted to coins whose 24h volume (in BTC) falls in
    [LCW_MIN_VOLUME_BTC, LCW_MAX_VOLUME_BTC].
    """
    if not config.LCW_API_KEY:
        return [], []

    band = []
    offset = 0

    for _ in range(MAX_PAGES):
        page = _post("/coins/list", {
            "currency": "BTC",
            "sort": "volume",
            "order": "descending",
            "offset": offset,
            "limit": PAGE_SIZE,
            "meta": False,
        })

        if not page:
            break

        page_min_volume = min(c.get("volume", 0) for c in page)

        for coin in page:
            code = coin.get("code")
            volume = coin.get("volume", 0)
            if code == "BTC":
                continue
            if config.LCW_MIN_VOLUME_BTC <= volume <= config.LCW_MAX_VOLUME_BTC:
                delta = coin.get("delta", {})
                delta_day = delta.get("day")
                if delta_day is None:
                    continue
                band.append({
                    "code": code,
                    "name": coin.get("name"),
                    "volume_btc": volume,
                    "change_24h_pct": round((delta_day - 1) * 100, 2),
                })

        offset += PAGE_SIZE

        # Page is sorted by volume descending, so once an entire page falls
        # below our minimum, every later page will too — safe to stop.
        if page_min_volume < config.LCW_MIN_VOLUME_BTC:
            break
        if len(page) < PAGE_SIZE:
            break

    top_gainers = sorted(band, key=lambda c: c["change_24h_pct"], reverse=True)[: config.LCW_TOP_N]
    top_losers = sorted(band, key=lambda c: c["change_24h_pct"])[: config.LCW_TOP_N]

    return top_gainers, top_losers
