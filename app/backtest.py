from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

from app.signals.episodes import build_episodes


@dataclass(slots=True)
class SignalEvent:
    ts_ms: int
    symbol: str
    pump_score: float
    dump_score: float
    stage: str
    close: float


@dataclass(slots=True)
class SignalOutcome:
    ts_ms: int
    symbol: str
    direction: str
    score: float
    stage: str
    entry: float
    horizon_min: int
    mfe_pct: float
    mae_pct: float
    hit_tp: bool
    hit_sl: bool
    time_to_tp_min: float | None
    time_to_sl_min: float | None
    gross_r: float | None
    net_r: float | None
    cost_pct: float
    funding_cost_pct: float
    ambiguous_bar: bool


def _bars(conn: sqlite3.Connection, symbol: str, start_ms: int, end_ms: int) -> list[tuple[int,float,float,float,float]]:
    return conn.execute(
        "SELECT bucket_ms,open,high,low,close FROM bars_1m WHERE symbol=? AND bucket_ms>? AND bucket_ms<=? ORDER BY bucket_ms",
        (symbol, start_ms, end_ms),
    ).fetchall()


def evaluate_signal(
    signal: SignalEvent,
    future_bars: Iterable[tuple[int,float,float,float,float]],
    direction: str,
    tp_pct: float,
    sl_pct: float,
    horizon_min: int,
    fee_bps_per_side: float = 4.0,
    slippage_bps_per_side: float = 2.0,
    funding_events: Iterable[tuple[int, float]] | None = None,
) -> SignalOutcome:
    bars = list(future_bars)[:horizon_min]
    entry = signal.close
    round_trip_cost_pct = (2.0 * (fee_bps_per_side + slippage_bps_per_side)) / 100.0
    ambiguous = False

    if direction == "long":
        mfe = max(((r[2] / entry) - 1) * 100 for r in bars) if bars else 0.0
        mae = min(((r[3] / entry) - 1) * 100 for r in bars) if bars else 0.0
        tp_level, sl_level = entry * (1 + tp_pct/100), entry * (1 - sl_pct/100)
        tp_hit = sl_hit = False
        ttp = tsl = None
        for i, r in enumerate(bars, 1):
            hit_tp = r[2] >= tp_level
            hit_sl = r[3] <= sl_level
            if hit_tp and hit_sl:
                ambiguous = True
                # Conservative research convention: assume stop was hit first.
                hit_tp = False
            if hit_tp and not tp_hit:
                tp_hit, ttp = True, float(i)
            if hit_sl and not sl_hit:
                sl_hit, tsl = True, float(i)
            if tp_hit or sl_hit:
                break
        gross_r = None
        if tp_hit and ttp is not None and (tsl is None or ttp <= tsl):
            gross_r = tp_pct / sl_pct
        elif sl_hit:
            gross_r = -1.0
    else:
        mfe = max(((entry / r[3]) - 1) * 100 for r in bars) if bars else 0.0
        mae = min(((entry / r[2]) - 1) * 100 for r in bars) if bars else 0.0
        tp_level, sl_level = entry * (1 - tp_pct/100), entry * (1 + sl_pct/100)
        tp_hit = sl_hit = False
        ttp = tsl = None
        for i, r in enumerate(bars, 1):
            hit_tp = r[3] <= tp_level
            hit_sl = r[2] >= sl_level
            if hit_tp and hit_sl:
                ambiguous = True
                hit_tp = False
            if hit_tp and not tp_hit:
                tp_hit, ttp = True, float(i)
            if hit_sl and not sl_hit:
                sl_hit, tsl = True, float(i)
            if tp_hit or sl_hit:
                break
        gross_r = None
        if tp_hit and ttp is not None and (tsl is None or ttp <= tsl):
            gross_r = tp_pct / sl_pct
        elif sl_hit:
            gross_r = -1.0

    # Funding is charged/credited only at actual funding timestamps. Positive funding
    # means longs pay shorts; negative funding means longs receive. We use the
    # direction of the position to convert signed funding into P&L impact.
    exit_offset_ms = int((ttp if ttp is not None else tsl if tsl is not None else horizon_min) * 60_000)
    exit_ts_ms = signal.ts_ms + exit_offset_ms
    funding_cost_pct = 0.0
    if funding_events:
        for funding_ts, rate in funding_events:
            if signal.ts_ms < int(funding_ts) <= exit_ts_ms:
                if direction == "long":
                    funding_cost_pct += float(rate) * 100.0
                else:
                    funding_cost_pct -= float(rate) * 100.0
    net_cost_pct = round_trip_cost_pct + funding_cost_pct
    net_r = None if gross_r is None else gross_r - (net_cost_pct / sl_pct)
    return SignalOutcome(
        signal.ts_ms, signal.symbol, direction, signal.pump_score if direction == "long" else signal.dump_score,
        signal.stage, entry, horizon_min, mfe, mae, tp_hit, sl_hit, ttp, tsl,
        gross_r, net_r, round_trip_cost_pct, funding_cost_pct, ambiguous,
    )


def _load_episode_signals(db_path: str, min_score: float, cooldown_min: int) -> list[SignalEvent]:
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT ts_ms,symbol,pump_score,dump_score,stage, json_extract(features,'$.close') AS close "
        "FROM feature_snapshots WHERE pump_score>=? OR dump_score>=? ORDER BY symbol,ts_ms",
        (min_score, min_score),
    ).fetchall()
    conn.close()
    data = [dict(zip(["ts_ms","symbol","pump_score","dump_score","stage","close"], r)) for r in rows if r[5] is not None]
    episodes = build_episodes(data, min_score=min_score, cooldown_min=cooldown_min)
    return [SignalEvent(e.start_ts_ms, e.symbol, e.entry_score if e.direction == "long" else 0.0,
                        e.entry_score if e.direction == "short" else 0.0, e.stage_at_entry, e.entry_price) for e in episodes]


def run_backtest(
    db_path: str,
    output_csv: str,
    min_score: float = 80.0,
    tp_pct: float = 5.0,
    sl_pct: float = 2.0,
    horizon_min: int = 60,
    cooldown_min: int = 15,
    fee_bps_per_side: float = 4.0,
    slippage_bps_per_side: float = 2.0,
) -> dict:
    conn = sqlite3.connect(db_path)
    signals = _load_episode_signals(db_path, min_score, cooldown_min)
    outcomes: list[SignalOutcome] = []
    for s in signals:
        direction = "long" if s.pump_score >= s.dump_score and s.pump_score >= min_score else "short"
        future = _bars(conn, s.symbol, s.ts_ms, s.ts_ms + horizon_min * 60_000)
        funding_rows = conn.execute(
            "SELECT ts_ms,funding_rate FROM funding_samples WHERE symbol=? AND ts_ms>? AND ts_ms<=? ORDER BY ts_ms",
            (s.symbol, s.ts_ms, s.ts_ms + horizon_min * 60_000),
        ).fetchall()
        outcomes.append(evaluate_signal(
            s, future, direction, tp_pct, sl_pct, horizon_min, fee_bps_per_side, slippage_bps_per_side, funding_rows
        ))
    conn.close()

    Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(asdict(outcomes[0]).keys()) if outcomes else ["ts_ms"]
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(asdict(x) for x in outcomes)

    resolved = [o for o in outcomes if o.net_r is not None]
    wins = sum(1 for o in resolved if (o.net_r or 0) > 0)
    losses = sum(1 for o in resolved if (o.net_r or 0) <= 0)
    avg_net_r = sum(o.net_r for o in resolved) / len(resolved) if resolved else None
    return {
        "signals": len(outcomes), "resolved": len(resolved), "wins": wins, "losses": losses,
        "hit_rate": wins / len(resolved) if resolved else None,
        "expectancy_R_net": avg_net_r,
        "tp_pct": tp_pct, "sl_pct": sl_pct, "horizon_min": horizon_min,
        "cooldown_min": cooldown_min, "fee_bps_per_side": fee_bps_per_side,
        "slippage_bps_per_side": slippage_bps_per_side, "output_csv": output_csv,
    }


def run_backtest_matrix(db_path: str, output_dir: str, min_score: float = 80.0, cooldown_min: int = 15,
                        horizons: tuple[int, ...] = (15, 30, 60, 240, 1440),
                        targets: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0), sl_pct: float = 2.0,
                        fee_bps_per_side: float = 4.0, slippage_bps_per_side: float = 2.0) -> list[dict]:
    results: list[dict] = []
    for horizon in horizons:
        for tp in targets:
            out = Path(output_dir) / f"bt_h{horizon}_tp{str(tp).replace('.', '_')}.csv"
            results.append(run_backtest(db_path, str(out), min_score=min_score, tp_pct=tp, sl_pct=sl_pct,
                                        horizon_min=horizon, cooldown_min=cooldown_min,
                                        fee_bps_per_side=fee_bps_per_side, slippage_bps_per_side=slippage_bps_per_side))
    return results
