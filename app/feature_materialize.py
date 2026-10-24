from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone

from app.features.replay import materialize_range
from app.signals.episodes import materialize_episodes


def parse_time(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        raw = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)


async def main_async(args: argparse.Namespace) -> None:
    start_ms, end_ms = parse_time(args.start), parse_time(args.end)
    stats = materialize_range(args.db, args.symbols, start_ms, end_ms, history_bars=args.history_bars)
    episodes = materialize_episodes(args.db, min_score=args.min_score, cooldown_min=args.cooldown_min)
    print(json.dumps({"materialization": stats, "signal_episodes": episodes}, indent=2))


def main() -> None:
    p = argparse.ArgumentParser(description="Point-in-time feature replay and signal-episode materialization")
    p.add_argument("--db", default="data/radar.db")
    p.add_argument("--symbols", nargs="+", required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--history-bars", type=int, default=120)
    p.add_argument("--min-score", type=float, default=80.0)
    p.add_argument("--cooldown-min", type=int, default=15)
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
