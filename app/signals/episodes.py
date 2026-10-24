from __future__ import annotations

from dataclasses import dataclass, asdict
import json
import sqlite3

from app.storage.sqlite import SQLiteStore


@dataclass(slots=True)
class Episode:
    symbol: str
    direction: str
    start_ts_ms: int
    end_ts_ms: int
    peak_score: float
    entry_score: float
    entry_price: float
    snapshots: int
    stage_at_entry: str
    score_version: str = "score_features_v1"


def build_episodes(rows: list[dict], min_score: float = 80.0, cooldown_min: int = 15) -> list[Episode]:
    """Collapse repeated snapshots into independent directional signal episodes."""
    cooldown_ms = cooldown_min * 60_000
    rows = sorted(rows, key=lambda r: (r["symbol"], r["ts_ms"]))
    out: list[Episode] = []
    active: Episode | None = None

    for row in rows:
        pump = float(row.get("pump_score") or 0.0)
        dump = float(row.get("dump_score") or 0.0)
        if pump < min_score and dump < min_score:
            continue
        direction = "long" if pump >= dump and pump >= min_score else "short"
        score = pump if direction == "long" else dump
        ts = int(row["ts_ms"])
        price = float(row.get("close") or 0.0)
        if price <= 0:
            continue

        if active is None or active.symbol != row["symbol"] or active.direction != direction or ts - active.end_ts_ms > cooldown_ms:
            if active is not None:
                out.append(active)
            active = Episode(
                symbol=row["symbol"], direction=direction, start_ts_ms=ts, end_ts_ms=ts,
                peak_score=score, entry_score=score, entry_price=price, snapshots=1,
                stage_at_entry=row.get("stage") or "WATCH",
            )
        else:
            active.end_ts_ms = ts
            active.peak_score = max(active.peak_score, score)
            active.snapshots += 1
    if active is not None:
        out.append(active)
    return out


def materialize_episodes(db_path: str, min_score: float = 80.0, cooldown_min: int = 15) -> int:
    db = SQLiteStore(db_path)
    try:
        rows = db.conn.execute(
            "SELECT ts_ms,symbol,pump_score,dump_score,stage, json_extract(features,'$.close') AS close "
            "FROM feature_snapshots WHERE pump_score>=? OR dump_score>=? ORDER BY symbol,ts_ms",
            (min_score, min_score),
        ).fetchall()
        data = [dict(zip(["ts_ms","symbol","pump_score","dump_score","stage","close"], r)) for r in rows]
        episodes = build_episodes(data, min_score=min_score, cooldown_min=cooldown_min)
        db.clear_signal_episodes()
        for episode in episodes:
            db.write_signal_episode(asdict(episode))
        return len(episodes)
    finally:
        db.close()
