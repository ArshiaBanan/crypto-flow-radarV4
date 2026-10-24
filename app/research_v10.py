from __future__ import annotations

import csv
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median
from typing import Iterable, Sequence

from app.signals.episodes import Episode, build_episodes

DAY_MS = 86_400_000
MIN_MS = 60_000
DEFAULT_HORIZONS = (15, 30, 60, 240, 1440)
DEFAULT_TP_TARGETS = (1.0, 2.0, 5.0, 10.0)
DEFAULT_SL_PCT = 2.0
DATASET_VERSION = "research_dataset_v1"


@dataclass(slots=True, frozen=True)
class BarrierLabel:
    horizon_min: int
    tp_pct: float
    sl_pct: float
    outcome: int  # +1 TP first, -1 SL first, 0 timeout
    hit_tp: bool
    hit_sl: bool
    ambiguous_bar: bool
    time_to_tp_min: float | None
    time_to_sl_min: float | None
    mfe_pct: float
    mae_pct: float
    future_return_pct: float
    signed_future_return_pct: float


@dataclass(slots=True, frozen=True)
class ResearchExample:
    dataset_version: str
    symbol: str
    direction: str
    ts_ms: int
    entry_price: float
    entry_score: float
    score_version: str
    label_end_ts_ms: int
    features: dict
    labels: dict


def _safe_float(value) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _future_bars(conn: sqlite3.Connection, symbol: str, ts_ms: int, horizon_min: int) -> list[tuple]:
    end = ts_ms + horizon_min * MIN_MS
    rows = conn.execute(
        "SELECT bucket_ms,open,high,low,close FROM bars_1m "
        "WHERE symbol=? AND bucket_ms>? AND bucket_ms<=? ORDER BY bucket_ms",
        (symbol, int(ts_ms), int(end)),
    ).fetchall()
    if len(rows) != horizon_min:
        return []
    expected = [int(ts_ms) + i * MIN_MS for i in range(1, horizon_min + 1)]
    actual = [int(r[0]) for r in rows]
    if actual != expected:
        return []
    return rows


def _label_one(
    *,
    entry: float,
    direction: str,
    bars: Sequence[tuple],
    horizon_min: int,
    tp_pct: float,
    sl_pct: float,
) -> BarrierLabel:
    if not bars:
        raise ValueError("bars must be non-empty")
    if entry <= 0 or tp_pct <= 0 or sl_pct <= 0:
        raise ValueError("entry, tp_pct and sl_pct must be positive")

    if direction == "long":
        mfe = max(((float(r[2]) / entry) - 1.0) * 100.0 for r in bars)
        mae = min(((float(r[3]) / entry) - 1.0) * 100.0 for r in bars)
        tp_level = entry * (1.0 + tp_pct / 100.0)
        sl_level = entry * (1.0 - sl_pct / 100.0)
    elif direction == "short":
        mfe = max(((entry / float(r[3])) - 1.0) * 100.0 for r in bars)
        mae = min(((entry / float(r[2])) - 1.0) * 100.0 for r in bars)
        tp_level = entry * (1.0 - tp_pct / 100.0)
        sl_level = entry * (1.0 + sl_pct / 100.0)
    else:
        raise ValueError(f"unknown direction: {direction}")

    hit_tp = hit_sl = False
    ambiguous = False
    ttp = tsl = None
    for i, r in enumerate(bars, 1):
        high, low = float(r[2]), float(r[3])
        if direction == "long":
            cur_tp = high >= tp_level
            cur_sl = low <= sl_level
        else:
            cur_tp = low <= tp_level
            cur_sl = high >= sl_level
        if cur_tp and cur_sl:
            ambiguous = True
            # Conservative convention: SL wins when OHLC cannot determine intrabar order.
            cur_tp = False
        if cur_tp and not hit_tp:
            hit_tp, ttp = True, float(i)
        if cur_sl and not hit_sl:
            hit_sl, tsl = True, float(i)
        if hit_tp or hit_sl:
            break

    last_close = float(bars[-1][4])
    future_return = ((last_close / entry) - 1.0) * 100.0
    signed_return = future_return if direction == "long" else -future_return

    if hit_tp:
        outcome = 1
    elif hit_sl:
        outcome = -1
    else:
        outcome = 0

    return BarrierLabel(
        horizon_min=horizon_min,
        tp_pct=tp_pct,
        sl_pct=sl_pct,
        outcome=outcome,
        hit_tp=hit_tp,
        hit_sl=hit_sl,
        ambiguous_bar=ambiguous,
        time_to_tp_min=ttp,
        time_to_sl_min=tsl,
        mfe_pct=mfe,
        mae_pct=mae,
        future_return_pct=future_return,
        signed_future_return_pct=signed_return,
    )


def _feature_row(conn: sqlite3.Connection, symbol: str, ts_ms: int) -> dict | None:
    row = conn.execute(
        "SELECT features FROM feature_snapshots WHERE symbol=? AND ts_ms=? ORDER BY id LIMIT 1",
        (symbol, int(ts_ms)),
    ).fetchone()
    if not row:
        return None
    try:
        data = json.loads(row[0] or "{}")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _episode_rows(conn: sqlite3.Connection, min_score: float) -> list[dict]:
    rows = conn.execute(
        "SELECT ts_ms,symbol,pump_score,dump_score,stage,"
        "json_extract(features,'$.close') AS close "
        "FROM feature_snapshots WHERE pump_score>=? OR dump_score>=? ORDER BY symbol,ts_ms",
        (float(min_score), float(min_score)),
    ).fetchall()
    return [dict(zip(["ts_ms", "symbol", "pump_score", "dump_score", "stage", "close"], r)) for r in rows]


def build_examples(
    db_path: str,
    *,
    min_score: float = 80.0,
    cooldown_min: int = 15,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    tp_targets: Sequence[float] = DEFAULT_TP_TARGETS,
    sl_pct: float = DEFAULT_SL_PCT,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> list[ResearchExample]:
    if not horizons or any(int(x) <= 0 for x in horizons):
        raise ValueError("horizons must contain positive integers")
    if not tp_targets or any(float(x) <= 0 for x in tp_targets):
        raise ValueError("tp_targets must contain positive values")
    max_horizon = max(int(x) for x in horizons)

    conn = sqlite3.connect(db_path)
    try:
        rows = _episode_rows(conn, min_score)
        if start_ms is not None:
            rows = [r for r in rows if int(r["ts_ms"]) >= int(start_ms)]
        if end_ms is not None:
            rows = [r for r in rows if int(r["ts_ms"]) < int(end_ms)]
        episodes = build_episodes(rows, min_score=min_score, cooldown_min=cooldown_min)

        examples: list[ResearchExample] = []
        for episode in episodes:
            label_end = episode.start_ts_ms + max_horizon * MIN_MS
            if end_ms is not None and label_end > int(end_ms):
                continue
            features = _feature_row(conn, episode.symbol, episode.start_ts_ms)
            if not features:
                continue
            max_bars = _future_bars(conn, episode.symbol, episode.start_ts_ms, max_horizon)
            if not max_bars:
                continue

            labels: dict[str, dict] = {}
            for horizon in sorted({int(x) for x in horizons}):
                bars = max_bars[:horizon]
                for tp in sorted({float(x) for x in tp_targets}):
                    label = _label_one(
                        entry=episode.entry_price,
                        direction=episode.direction,
                        bars=bars,
                        horizon_min=horizon,
                        tp_pct=tp,
                        sl_pct=float(sl_pct),
                    )
                    key = f"h{horizon}_tp{tp:g}_sl{float(sl_pct):g}"
                    labels[key] = asdict(label)

            examples.append(
                ResearchExample(
                    dataset_version=DATASET_VERSION,
                    symbol=episode.symbol,
                    direction=episode.direction,
                    ts_ms=episode.start_ts_ms,
                    entry_price=episode.entry_price,
                    entry_score=episode.entry_score,
                    score_version=episode.score_version,
                    label_end_ts_ms=label_end,
                    features=features,
                    labels=labels,
                )
            )
        return examples
    finally:
        conn.close()


def _init_dataset_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS research_examples (
            dataset_version TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            entry_score REAL NOT NULL,
            score_version TEXT NOT NULL,
            label_end_ts_ms INTEGER NOT NULL,
            features TEXT NOT NULL,
            labels TEXT NOT NULL,
            PRIMARY KEY(dataset_version, symbol, ts_ms)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS ix_research_examples_ts ON research_examples(ts_ms)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_research_examples_symbol_ts ON research_examples(symbol,ts_ms)")


def persist_examples(db_path: str, examples: Sequence[ResearchExample], *, replace_version: bool = True) -> int:
    conn = sqlite3.connect(db_path)
    try:
        _init_dataset_table(conn)
        if replace_version:
            conn.execute("DELETE FROM research_examples WHERE dataset_version=?", (DATASET_VERSION,))
        for e in examples:
            conn.execute(
                "INSERT OR REPLACE INTO research_examples"
                "(dataset_version,symbol,direction,ts_ms,entry_price,entry_score,score_version,label_end_ts_ms,features,labels)"
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    e.dataset_version,
                    e.symbol,
                    e.direction,
                    e.ts_ms,
                    e.entry_price,
                    e.entry_score,
                    e.score_version,
                    e.label_end_ts_ms,
                    json.dumps(e.features, separators=(",", ":"), sort_keys=True),
                    json.dumps(e.labels, separators=(",", ":"), sort_keys=True),
                ),
            )
        conn.commit()
        return len(examples)
    finally:
        conn.close()


def export_examples(examples: Sequence[ResearchExample], output_csv: str) -> int:
    Path(output_csv).parent.mkdir(parents=True, exist_ok=True)
    feature_keys = sorted({k for e in examples for k in e.features.keys()})
    label_keys = sorted({k for e in examples for k in e.labels.keys()})
    fields = [
        "dataset_version", "symbol", "direction", "ts_ms", "entry_price", "entry_score",
        "score_version", "label_end_ts_ms",
        *[f"feature__{k}" for k in feature_keys],
        *[f"label__{k}" for k in label_keys],
    ]
    with open(output_csv, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for e in examples:
            row = {
                "dataset_version": e.dataset_version,
                "symbol": e.symbol,
                "direction": e.direction,
                "ts_ms": e.ts_ms,
                "entry_price": e.entry_price,
                "entry_score": e.entry_score,
                "score_version": e.score_version,
                "label_end_ts_ms": e.label_end_ts_ms,
            }
            row.update({f"feature__{k}": v for k, v in e.features.items()})
            row.update({f"label__{k}": json.dumps(v, separators=(",", ":"), sort_keys=True) for k, v in e.labels.items()})
            writer.writerow(row)
    return len(examples)


def build_research_dataset(
    db_path: str,
    output_csv: str,
    *,
    min_score: float = 80.0,
    cooldown_min: int = 15,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    tp_targets: Sequence[float] = DEFAULT_TP_TARGETS,
    sl_pct: float = DEFAULT_SL_PCT,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict:
    examples = build_examples(
        db_path,
        min_score=min_score,
        cooldown_min=cooldown_min,
        horizons=horizons,
        tp_targets=tp_targets,
        sl_pct=sl_pct,
        start_ms=start_ms,
        end_ms=end_ms,
    )
    persisted = persist_examples(db_path, examples)
    exported = export_examples(examples, output_csv)
    return {
        "dataset_version": DATASET_VERSION,
        "examples": len(examples),
        "persisted": persisted,
        "exported": exported,
        "max_horizon_min": max(horizons) if horizons else None,
        "output_csv": output_csv,
    }


def load_examples(db_path: str, *, dataset_version: str = DATASET_VERSION) -> list[ResearchExample]:
    conn = sqlite3.connect(db_path)
    try:
        _init_dataset_table(conn)
        rows = conn.execute(
            "SELECT dataset_version,symbol,direction,ts_ms,entry_price,entry_score,score_version,"
            "label_end_ts_ms,features,labels FROM research_examples WHERE dataset_version=? ORDER BY ts_ms",
            (dataset_version,),
        ).fetchall()
        out: list[ResearchExample] = []
        for r in rows:
            out.append(
                ResearchExample(
                    dataset_version=r[0],
                    symbol=r[1],
                    direction=r[2],
                    ts_ms=int(r[3]),
                    entry_price=float(r[4]),
                    entry_score=float(r[5]),
                    score_version=r[6],
                    label_end_ts_ms=int(r[7]),
                    features=json.loads(r[8] or "{}"),
                    labels=json.loads(r[9] or "{}"),
                )
            )
        return out
    finally:
        conn.close()


def purged_walk_forward_splits(
    examples: Sequence[ResearchExample],
    *,
    train_days: int = 28,
    test_days: int = 7,
    step_days: int = 7,
    embargo_min: int = 0,
) -> list[dict]:
    if not examples:
        return []
    if min(train_days, test_days, step_days) <= 0:
        raise ValueError("train_days, test_days and step_days must be positive")
    if embargo_min < 0:
        raise ValueError("embargo_min must be non-negative")

    lo = min(e.ts_ms for e in examples)
    hi = max(e.ts_ms for e in examples)
    out = []
    day = DAY_MS
    cur = lo + train_days * day
    fold = 0
    while cur + test_days * day <= hi + 1:
        train_start = cur - train_days * day
        test_start = cur
        test_end = cur + test_days * day
        purge_before = test_start - embargo_min * MIN_MS
        train = [e for e in examples if train_start <= e.ts_ms < test_start and e.label_end_ts_ms < purge_before]
        test = [e for e in examples if test_start <= e.ts_ms < test_end and e.label_end_ts_ms <= test_end]
        out.append({
            "fold": fold,
            "train_start_ms": train_start,
            "train_end_ms": test_start,
            "test_start_ms": test_start,
            "test_end_ms": test_end,
            "train": train,
            "test": test,
        })
        cur += step_days * day
        fold += 1
    return out


def dataset_summary(examples: Iterable[ResearchExample]) -> dict:
    xs = list(examples)
    symbols = sorted({e.symbol for e in xs})
    longs = sum(1 for e in xs if e.direction == "long")
    shorts = sum(1 for e in xs if e.direction == "short")
    return {
        "dataset_version": DATASET_VERSION,
        "examples": len(xs),
        "symbols": len(symbols),
        "long_examples": longs,
        "short_examples": shorts,
        "first_ts_ms": min((e.ts_ms for e in xs), default=None),
        "last_ts_ms": max((e.ts_ms for e in xs), default=None),
        "avg_entry_score": mean(e.entry_score for e in xs) if xs else None,
        "median_entry_score": median(e.entry_score for e in xs) if xs else None,
        "avg_label_span_min": mean((e.label_end_ts_ms - e.ts_ms) / MIN_MS for e in xs) if xs else None,
    }


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Build the v1 research dataset from point-in-time signal episodes")
    parser.add_argument("--db", default="data/radar.db")
    parser.add_argument("--output", default="data/research/research_dataset_v1.csv")
    parser.add_argument("--min-score", type=float, default=80.0)
    parser.add_argument("--cooldown-min", type=int, default=15)
    parser.add_argument("--sl-pct", type=float, default=DEFAULT_SL_PCT)
    parser.add_argument("--horizons", default=",".join(map(str, DEFAULT_HORIZONS)))
    parser.add_argument("--targets", default=",".join(map(str, DEFAULT_TP_TARGETS)))
    args = parser.parse_args()
    result = build_research_dataset(
        args.db,
        args.output,
        min_score=args.min_score,
        cooldown_min=args.cooldown_min,
        horizons=tuple(int(x) for x in args.horizons.split(",") if x),
        tp_targets=tuple(float(x) for x in args.targets.split(",") if x),
        sl_pct=args.sl_pct,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
