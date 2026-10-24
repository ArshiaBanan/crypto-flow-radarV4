"""
CryptoMeter.io integration — free-tier endpoints only, to stay comfortably
inside the 10,000 calls/month quota even when scanning every
SCAN_INTERVAL_MINUTES:

  /rapid-movements/    — free. Independently flags assets with a detected
                          PUMP or DUMP in the last 4 hours. This is almost
                          exactly what this whole project targets, coming
                          from a source that isn't Binance.
  /trend-indicator-v3/ — free. Overall market buy/sell pressure, used as
                          market-wide context rather than a per-symbol signal.

Both are called once per scan (not once per symbol), so at a 15-minute
interval that's ~192 calls/day — well inside quota.
"""

import requests
import config

CM_BASE_URL = "https://api.cryptometer.io"


def _get(endpoint, params):
    params = dict(params)
    params["api_key"] = config.CRYPTOMETER_API_KEY
    try:
        resp = requests.get(f"{CM_BASE_URL}{endpoint}", params=params, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        print(f"  [warn] CryptoMeter request failed for {endpoint}: {e}")
        return None

    if str(payload.get("error")).lower() not in ("false", "none"):
        print(f"  [warn] CryptoMeter error on {endpoint}: {payload}")
        return None

    return payload.get("data")


def get_rapid_movements():
    """List of {pair, exchange, change_detected, side, timestamp} PUMP/DUMP flags."""
    if not config.CRYPTOMETER_API_KEY:
        return []
    return _get("/rapid-movements/", {}) or []


def get_market_trend():
    """Single dict: {trend_score, buy_pressure, sell_pressure, timestamp} or None."""
    if not config.CRYPTOMETER_API_KEY:
        return None
    data = _get("/trend-indicator-v3/", {})
    return data[0] if data else None
