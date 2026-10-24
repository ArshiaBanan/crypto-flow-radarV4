"""
Minimal Telegram alert sender. Same bot-token/chat-id pattern as a typical
telegram_notifier.py — you can point this at the same bot as any other
project, using a different chat/channel ID if you want pump/dump alerts
separated from other signals.
"""

import requests
import config


def send_telegram_alert(message: str):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return

    url = f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": config.TELEGRAM_CHAT_ID, "text": message}

    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        print(f"[warn] Telegram alert failed: {e}")
