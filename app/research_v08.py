from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median
from typing import Iterable

from app.backtest import SignalEvent, evaluate_signal
from app.signals.episodes import Episode, build_episodes
from app.research_v07 import bootstrap_mean_ci


DEFAULT_THRESHOLDS = tuple(float(x) for x in range(60, 96, 5))
DEFAULT_SCORE_BINS = ((50.0, 60.0), (60.0, 70.0), (70.0, 80.0), (80.0, 90.0), (90.0, 101.0))


@dataclass(slots=True)
class EpisodeOutcome:
    episode: Episode
    net_r: float | None
    gross_r: float | None
    hit: bool | None
    mfe_pct: float
    mae_pct: float
    ambiguous_bar: bool


def _safe(v: object) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
        return x if math.isfinite(x) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _load_rows(conn: sqlite3.Connection, start_ms: int, end_ms: int, symbol: str | None = None) -> list[dict]:
    q = (
        "SELECT ts_ms,symbol,pump_score,dump_score,stage,"
        "json_extract(features,'$.close') AS close FROM feature_snapshots "
        "WHERE ts_ms>=? AND ts_ms<?"
    )
    args: list[object] = [start_ms, end_ms]
    if symbol:
        q += " AND symbol=?"
        args.append(symbol)
    q += " ORDER BY symbol,ts_ms"
    rows = conn.execute(q, args).fetchall()
    return [
        {
            "ts_ms": int(r[0]),
            "symbol": str(r[1]),
            "pump_score": _safe(r[2]),
            "dump_score": _safe(r[3]),
            "stage": r[4] or "WATCH",
            "close": _safe(r[5]),
        }
        for r in rows
        if _safe(r[5]) > 0
    ]


def _future_inputs(conn: sqlite3.Connection, ep: Episode, horizon_min: int) -> tuple[list[tuple], list[tuple]]:
    end = ep.start_ts_ms + horizon_min * 60_000
    bars = conn.execute(
        "SELECT bucket_ms,open,high,low,close FROM bars_1m "
        "WHERE symbol=? AND bucket_ms>? AND bucket_ms<=? ORDER BY bucket_ms",
        (ep.symbol, ep.start_ts_ms, end),
    ).fetchall()
    funds = conn.execute(
        "SELECT ts_ms,funding_rate FROM funding_samples "
        "WHERE lower(symbol)=? AND ts_ms>? AND ts_ms<=? ORDER BY ts_ms",
        (ep.symbol.lower(), ep.start_ts_ms, end),
    ).fetchall()
    return list(bars), list(funds)


def _evaluate_episode(
    conn: sqlite3.Connection,
    ep: Episode,
    *,
    horizon_min: int,
    tp_pct: float,
    sl_pct: float,
    fee_bps_per_side: float,
    slippage_bps_per_side: float,
) -> EpisodeOutcome:
    direction = ep.direction
    score = ep.entry_score
    signal = SignalEvent(
        ep.start_ts_ms,
        ep.symbol,
        score if direction == "long" else 0.0,
        score if direction == "short" else 0.0,
        ep.stage_at_entry,
        ep.entry_price,
    )
    bars, funds = _future_inputs(conn, ep, horizon_min)
    result = evaluate_signal(
        signal,
        bars,
        direction,
        tp_pct,
        sl_pct,
        horizon_min,
        fee_bps_per_side,
        slippage_bps_per_side,
        funds,
    )
    hit = None if result.net_r is None else result.net_r > 0
    return EpisodeOutcome(ep, result.net_r, result.gross_r, hit, result.mfe_pct, result.mae_pct, result.ambiguous_bar)


def collect_threshold_outcomes(
    db_path: str,
    *,
    threshold: float,
    start_ms: int | None = None,
    end_ms: int | None = None,
    cooldown_min: int = 15,
    horizon_min: int = 60,
    tp_pct: float = 5.0,
    sl_pct: float = 2.0,
    fee_bps_per_side: float = 4.0,
    slippage_bps_per_side: float = 2.0,
) -> list[EpisodeOutcome]:
    conn = sqlite3.connect(db_path)
    lo, hi = conn.execute("SELECT MIN(ts_ms),MAX(ts_ms) FROM feature_snapshots").fetchone()
    if lo is None or hi is None:
        conn.close()
        return []
    lo = int(start_ms if start_ms is not None else lo)
    hi = int(end_ms if end_ms is not None else hi + 1)
    rows = _load_rows(conn, lo, hi)
    episodes = build_episodes(rows, min_score=threshold, cooldown_min=cooldown_min)
    # For train/test isolation, discard signals whose complete outcome horizon crosses end_ms.
    if end_ms is not None:
        episodes = [e for e in episodes if e.start_ts_ms + horizon_min * 60_000 <= int(end_ms)]
    out = [
        _evaluate_episode(
            conn,
            ep,
            horizon_min=horizon_min,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            fee_bps_per_side=fee_bps_per_side,
            slippage_bps_per_side=slippage_bps_per_side,
        )
        for ep in episodes
    ]
    conn.close()
    return out


def _metric_row(threshold: float, outcomes: Iterable[EpisodeOutcome], bootstrap_iterations: int = 3000) -> dict:
    xs = list(outcomes)
    resolved = [x.net_r for x in xs if x.net_r is not None]
    wins = sum(1 for x in resolved if x > 0)
    losses = sum(1 for x in resolved if x <= 0)
    gross_wins = sum(x for x in resolved if x > 0)
    gross_losses = abs(sum(x for x in resolved if x < 0))
    ci = bootstrap_mean_ci(resolved, iterations=bootstrap_iterations) if resolved else {"lo": None, "hi": None, "mean": None, "n": 0}
    entry_scores = [x.episode.entry_score for x in xs]
    return {
        "threshold": float(threshold),
        "signals": len(xs),
        "resolved": len(resolved),
        "wins": wins,
        "losses": losses,
        "hit_rate": wins / len(resolved) if resolved else None,
        "expectancy_R_net": mean(resolved) if resolved else None,
        "median_R_net": median(resolved) if resolved else None,
        "profit_factor": gross_wins / gross_losses if gross_losses > 0 else (math.inf if gross_wins > 0 else None),
        "bootstrap_lo": ci["lo"],
        "bootstrap_hi": ci["hi"],
        "avg_mfe_pct": mean(x.mfe_pct for x in xs) if xs else None,
        "avg_mae_pct": mean(x.mae_pct for x in xs) if xs else None,
        "ambiguous_rate": mean(1.0 if x.ambiguous_bar else 0.0 for x in xs) if xs else None,
        "mean_entry_score": mean(entry_scores) if entry_scores else None,
        "score_above_80_rate": mean(1.0 if s >= 80 else 0.0 for s in entry_scores) if entry_scores else None,
    }


def threshold_report(
    db_path: str,
    output_csv: str,
    *,
    thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS,
    start_ms: int | None = None,
    end_ms: int | None = None,
    cooldown_min: int = 15,
    horizon_min: int = 60,
    tp_pct: float = 5.0,
    sl_pct: float = 2.0,
    fee_bps_per_side: float = 4.0,
    slippage_bps_per_side: float = 2.0,
    bootstrap_iterations: int = 3000,
) -> list[dict]:
    out: list[dict] = []
    for threshold in thresholds:
        outcomes = collect_threshold_outcomes(
            db_path,
            threshold=threshold,
            start_ms=start_ms,
            end_ms=end_ms,
            cooldown_min=cooldown_min,
            horizon_min=horizon_min,
            tp_pct=tp_pct,
            sl_pct=sl_pct,
            fee_bps_per_side=fee_bps_per_side,
            slippage_bps_per_side=slippage_bps_per_side,
        )
        out.append(_metric_row(threshold, outcomes, bootstrap_iterations))

    Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        fields = list(out[0].keys()) if out else ["threshold"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(out)
    return out


def score_bucket_report(
    db_path: str,
    output_csv: str,
    *,
    bins: tuple[tuple[float, float], ...] = DEFAULT_SCORE_BINS,
    start_ms: int | None = None,
    end_ms: int | None = None,
    cooldown_min: int = 15,
    horizon_min: int = 60,
    tp_pct: float = 5.0,
    sl_pct: float = 2.0,
    fee_bps_per_side: float = 4.0,
    slippage_bps_per_side: float = 2.0,
) -> list[dict]:
    conn = sqlite3.connect(db_path)
    lo, hi = conn.execute("SELECT MIN(ts_ms),MAX(ts_ms) FROM feature_snapshots").fetchone()
    if lo is None or hi is None:
        conn.close()
        return []
    lo = int(start_ms if start_ms is not None else lo)
    hi = int(end_ms if end_ms is not None else hi + 1)
    # Build at the lowest bucket threshold so entry_score is the first actual crossing score.
    floor = min(b[0] for b in bins)
    rows = _load_rows(conn, lo, hi)
    episodes = build_episodes(rows, min_score=floor, cooldown_min=cooldown_min)
    if end_ms is not None:
        episodes = [e for e in episodes if e.start_ts_ms + horizon_min * 60_000 <= int(end_ms)]
    evaluated = [_evaluate_episode(conn, e, horizon_min=horizon_min, tp_pct=tp_pct, sl_pct=sl_pct,
                                    fee_bps_per_side=fee_bps_per_side, slippage_bps_per_side=slippage_bps_per_side)
                 for e in episodes]
    conn.close()
    out = []
    for lo_score, hi_score in bins:
        xs = [x for x in evaluated if lo_score <= x.episode.entry_score < hi_score]
        rs = [x.net_r for x in xs if x.net_r is not None]
        wins = sum(1 for r in rs if r > 0)
        out.append({
            "score_bin": f"{lo_score:g}-{hi_score:g}",
            "score_lo": lo_score,
            "score_hi": hi_score,
            "signals": len(xs),
            "resolved": len(rs),
            "hit_rate": wins / len(rs) if rs else None,
            "expectancy_R_net": mean(rs) if rs else None,
            "median_R_net": median(rs) if rs else None,
            "avg_mfe_pct": mean(x.mfe_pct for x in xs) if xs else None,
            "avg_mae_pct": mean(x.mae_pct for x in xs) if xs else None,
        })
    Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        fields = list(out[0].keys()) if out else ["score_bin"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(out)
    return out


def choose_threshold(
    rows: Iterable[dict],
    *,
    min_resolved: int = 30,
) -> dict | None:
    candidates = [
        r for r in rows
        if r.get("expectancy_R_net") is not None and int(r.get("resolved") or 0) >= min_resolved
    ]
    if not candidates:
        return None
    # Robust selection: maximize the bootstrap lower confidence bound,
    # then prefer higher resolved count and a higher threshold as tie breakers.
    return max(
        candidates,
        key=lambda r: (
            -math.inf if r.get("bootstrap_lo") is None else float(r["bootstrap_lo"]),
            int(r.get("resolved") or 0),
            float(r["threshold"]),
        ),
    )


def walk_forward_thresholds(
    db_path: str,
    output_csv: str,
    *,
    thresholds: tuple[float, ...] = DEFAULT_THRESHOLDS,
    train_days: int = 28,
    test_days: int = 7,
    step_days: int = 7,
    cooldown_min: int = 15,
    horizon_min: int = 60,
    tp_pct: float = 5.0,
    sl_pct: float = 2.0,
    fee_bps_per_side: float = 4.0,
    slippage_bps_per_side: float = 2.0,
    min_resolved_train: int = 30,
    bootstrap_iterations: int = 2000,
) -> list[dict]:
    conn = sqlite3.connect(db_path)
    lo, hi = conn.execute("SELECT MIN(ts_ms),MAX(ts_ms) FROM feature_snapshots").fetchone()
    conn.close()
    if lo is None or hi is None:
        return []
    lo, hi = int(lo), int(hi) + 1
    day = 86_400_000
    cur = lo + train_days * day
    out: list[dict] = []
    fold = 0
    while cur + test_days * day <= hi:
        train0, train1 = cur - train_days * day, cur
        test0, test1 = cur, cur + test_days * day
        train_rows = threshold_report(
            db_path, f"{Path(output_csv).with_suffix('')}_train_fold{fold}.csv",
            thresholds=thresholds, start_ms=train0, end_ms=train1, cooldown_min=cooldown_min,
            horizon_min=horizon_min, tp_pct=tp_pct, sl_pct=sl_pct,
            fee_bps_per_side=fee_bps_per_side, slippage_bps_per_side=slippage_bps_per_side,
            bootstrap_iterations=bootstrap_iterations,
        )
        chosen = choose_threshold(train_rows, min_resolved=min_resolved_train)
        chosen_threshold = float(chosen["threshold"]) if chosen else None
        if chosen_threshold is None:
            test_metrics = _metric_row(0.0, [], bootstrap_iterations=bootstrap_iterations)
        else:
            test_outcomes = collect_threshold_outcomes(
                db_path, threshold=chosen_threshold, start_ms=test0, end_ms=test1,
                cooldown_min=cooldown_min, horizon_min=horizon_min, tp_pct=tp_pct, sl_pct=sl_pct,
                fee_bps_per_side=fee_bps_per_side, slippage_bps_per_side=slippage_bps_per_side,
            )
            test_metrics = _metric_row(chosen_threshold, test_outcomes, bootstrap_iterations=bootstrap_iterations)
        out.append({
            "fold": fold,
            "train_start_ms": train0,
            "train_end_ms": train1,
            "test_start_ms": test0,
            "test_end_ms": test1,
            "chosen_threshold": chosen_threshold,
            "train_resolved": chosen.get("resolved") if chosen else 0,
            "train_expectancy_R_net": chosen.get("expectancy_R_net") if chosen else None,
            "train_bootstrap_lo": chosen.get("bootstrap_lo") if chosen else None,
            "test_signals": test_metrics["signals"],
            "test_resolved": test_metrics["resolved"],
            "test_hit_rate": test_metrics["hit_rate"],
            "test_expectancy_R_net": test_metrics["expectancy_R_net"],
            "test_median_R_net": test_metrics["median_R_net"],
            "test_bootstrap_lo": test_metrics["bootstrap_lo"],
            "test_bootstrap_hi": test_metrics["bootstrap_hi"],
        })
        fold += 1
        cur += step_days * day

    Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        fields = list(out[0].keys()) if out else ["fold"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(out)
    return out


def aggregate_walk_forward(rows: Iterable[dict]) -> dict:
    xs = list(rows)
    test_r = [float(x["test_expectancy_R_net"]) for x in xs if x.get("test_expectancy_R_net") is not None]
    thresholds = [float(x["chosen_threshold"]) for x in xs if x.get("chosen_threshold") is not None]
    return {
        "folds": len(xs),
        "resolved_folds": len(test_r),
        "mean_fold_expectancy_R_net": mean(test_r) if test_r else None,
        "median_fold_expectancy_R_net": median(test_r) if test_r else None,
        "positive_fold_rate": sum(r > 0 for r in test_r) / len(test_r) if test_r else None,
        "mean_chosen_threshold": mean(thresholds) if thresholds else None,
        "thresholds_used": sorted(set(thresholds)),
    }
