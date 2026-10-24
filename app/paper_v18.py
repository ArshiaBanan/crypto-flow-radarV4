from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence


EPS = 1e-12


@dataclass(frozen=True, slots=True)
class PaperSignal:
    signal_id: str
    ts_ms: int
    symbol: str
    direction: str
    entry_price: float
    stop_pct: float
    target_pct: float
    horizon_min: int
    risk_score: float = 0.0
    fusion_score: float = 0.0
    ml_probability: float | None = None
    quality_tier: str = "B"
    expected_value_r: float | None = None
    slippage_bps: float = 2.0
    fee_bps_per_side: float = 4.0
    funding_cost_pct: float = 0.0
    latency_ms: int = 0


@dataclass(frozen=True, slots=True)
class PaperResult:
    signal_id: str
    symbol: str
    direction: str
    signal_ts_ms: int
    execution_ts_ms: int | None
    exit_ts_ms: int | None
    signal_price: float
    execution_price: float | None
    exit_price: float | None
    status: str  # RESOLVED / OPEN / CANCELLED / NO_DATA
    exit_reason: str | None  # TP / SL / HORIZON / NO_DATA
    gross_return_pct: float | None
    net_return_pct: float | None
    gross_r: float | None
    net_r: float | None
    mfe_pct: float
    mae_pct: float
    slippage_pct: float
    fee_pct: float
    funding_pct: float
    latency_ms: int
    ambiguous_bar: bool
    bars_observed: int
    metadata: dict[str, Any]


class PaperTradeStore:
    """Durable paper-trading ledger, deliberately isolated from live order execution."""

    def __init__(self, path: str = "data/radar.db") -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self._init()

    def _init(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS paper_trades (
                    signal_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    signal_ts_ms INTEGER NOT NULL,
                    execution_ts_ms INTEGER,
                    exit_ts_ms INTEGER,
                    signal_price REAL NOT NULL,
                    execution_price REAL,
                    exit_price REAL,
                    status TEXT NOT NULL,
                    exit_reason TEXT,
                    gross_return_pct REAL,
                    net_return_pct REAL,
                    gross_r REAL,
                    net_r REAL,
                    mfe_pct REAL NOT NULL DEFAULT 0,
                    mae_pct REAL NOT NULL DEFAULT 0,
                    slippage_pct REAL NOT NULL DEFAULT 0,
                    fee_pct REAL NOT NULL DEFAULT 0,
                    funding_pct REAL NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    ambiguous_bar INTEGER NOT NULL DEFAULT 0,
                    bars_observed INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS ix_paper_trades_symbol_ts ON paper_trades(symbol, signal_ts_ms);
                CREATE INDEX IF NOT EXISTS ix_paper_trades_status ON paper_trades(status);
                """
            )

    def write(self, result: PaperResult) -> None:
        payload = json.dumps(result.metadata, separators=(",", ":"), sort_keys=True)
        with self.conn:
            self.conn.execute(
                """INSERT OR REPLACE INTO paper_trades(
                    signal_id,symbol,direction,signal_ts_ms,execution_ts_ms,exit_ts_ms,
                    signal_price,execution_price,exit_price,status,exit_reason,
                    gross_return_pct,net_return_pct,gross_r,net_r,mfe_pct,mae_pct,
                    slippage_pct,fee_pct,funding_pct,latency_ms,ambiguous_bar,bars_observed,metadata
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result.signal_id, result.symbol, result.direction, result.signal_ts_ms,
                    result.execution_ts_ms, result.exit_ts_ms, result.signal_price,
                    result.execution_price, result.exit_price, result.status, result.exit_reason,
                    result.gross_return_pct, result.net_return_pct, result.gross_r, result.net_r,
                    result.mfe_pct, result.mae_pct, result.slippage_pct, result.fee_pct,
                    result.funding_pct, result.latency_ms, int(result.ambiguous_bar),
                    result.bars_observed, payload,
                ),
            )

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM paper_trades ORDER BY signal_ts_ms DESC LIMIT ?", (max(1, int(limit)),)
        ).fetchall()
        cols = [d[0] for d in self.conn.execute("PRAGMA table_info(paper_trades)").fetchall()]
        # PRAGMA table_info returns (cid,name,type,...); use the actual names.
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(paper_trades)").fetchall()]
        out: list[dict[str, Any]] = []
        for row in rows:
            d = dict(zip(cols, row))
            try:
                d["metadata"] = json.loads(d.get("metadata") or "{}")
            except json.JSONDecodeError:
                d["metadata"] = {}
            out.append(d)
        return out

    def close(self) -> None:
        self.conn.close()


def _px(v: float) -> float:
    x = float(v)
    if not math.isfinite(x) or x <= 0:
        raise ValueError(f"invalid price: {v!r}")
    return x


def _validate_signal(signal: PaperSignal) -> None:
    if signal.direction not in {"long", "short"}:
        raise ValueError("direction must be long or short")
    _px(signal.entry_price)
    if signal.stop_pct <= 0 or signal.target_pct <= 0:
        raise ValueError("stop_pct and target_pct must be positive")
    if signal.horizon_min <= 0:
        raise ValueError("horizon_min must be positive")
    if signal.latency_ms < 0:
        raise ValueError("latency_ms cannot be negative")


def _apply_entry_slippage(price: float, direction: str, slippage_bps: float) -> float:
    slip = max(0.0, float(slippage_bps)) / 10000.0
    return price * (1.0 + slip if direction == "long" else 1.0 - slip)


def _apply_exit_slippage(price: float, direction: str, slippage_bps: float) -> float:
    slip = max(0.0, float(slippage_bps)) / 10000.0
    return price * (1.0 - slip if direction == "long" else 1.0 + slip)


def _pct(a: float, b: float) -> float:
    return (a / b - 1.0) * 100.0


def simulate_signal(
    signal: PaperSignal,
    future_bars: Sequence[tuple[int, float, float, float, float]],
) -> PaperResult:
    """Simulate execution from 1m bars.

    Bar tuple = (bucket_ms, open, high, low, close). If both TP and SL are touched in
    the same bar, the model is conservative and records SL first.
    """
    _validate_signal(signal)
    bars = list(future_bars)
    signal_price = _px(signal.entry_price)
    execution_ts: int | None = None
    execution_price: float | None = None
    if not bars:
        return PaperResult(
            signal.signal_id, signal.symbol, signal.direction, signal.ts_ms, None, None,
            signal_price, None, None, "NO_DATA", "NO_DATA", None, None, None, None,
            0.0, 0.0, 0.0, 0.0, 0.0, signal.latency_ms, False, 0,
            {"reason": "no_future_bars"},
        )

    latency_target = signal.ts_ms + int(signal.latency_ms)
    eligible = [b for b in bars if int(b[0]) >= latency_target]
    if not eligible:
        return PaperResult(
            signal.signal_id, signal.symbol, signal.direction, signal.ts_ms, None, None,
            signal_price, None, None, "NO_DATA", "NO_DATA", None, None, None, None,
            0.0, 0.0, 0.0, 0.0, 0.0, signal.latency_ms, False, 0,
            {"reason": "latency_exceeds_available_bars"},
        )

    first = eligible[0]
    execution_ts = int(first[0])
    execution_price = _apply_entry_slippage(float(first[1]), signal.direction, signal.slippage_bps)
    bars = [b for b in eligible if int(b[0]) <= execution_ts + signal.horizon_min * 60_000]

    if signal.direction == "long":
        tp_level = execution_price * (1.0 + signal.target_pct / 100.0)
        sl_level = execution_price * (1.0 - signal.stop_pct / 100.0)
        mfe = max((_pct(float(b[2]), execution_price) for b in bars), default=0.0)
        mae = min((_pct(float(b[3]), execution_price) for b in bars), default=0.0)
    else:
        tp_level = execution_price * (1.0 - signal.target_pct / 100.0)
        sl_level = execution_price * (1.0 + signal.stop_pct / 100.0)
        mfe = max((_pct(execution_price, float(b[3])) for b in bars), default=0.0)
        mae = min((_pct(execution_price, float(b[2])) for b in bars), default=0.0)

    exit_ts = None
    exit_raw = None
    reason = None
    ambiguous = False
    observed = 0
    for b in bars:
        observed += 1
        ts, _, high, low, close = int(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4])
        if signal.direction == "long":
            hit_tp, hit_sl = high >= tp_level, low <= sl_level
        else:
            hit_tp, hit_sl = low <= tp_level, high >= sl_level
        if hit_tp and hit_sl:
            ambiguous = True
            hit_tp, hit_sl = False, True
        if hit_tp:
            exit_ts, exit_raw, reason = ts, tp_level, "TP"
            break
        if hit_sl:
            exit_ts, exit_raw, reason = ts, sl_level, "SL"
            break
        # Horizon close: last eligible bar is marked to close.
        if ts >= execution_ts + signal.horizon_min * 60_000:
            exit_ts, exit_raw, reason = ts, close, "HORIZON"
            break

    if exit_raw is None:
        # We had data, but not enough to reach horizon or a barrier.
        last = bars[-1]
        exit_ts, exit_raw, reason = int(last[0]), float(last[4]), "HORIZON"

    exit_price = _apply_exit_slippage(exit_raw, signal.direction, signal.slippage_bps)
    if signal.direction == "long":
        gross_return = _pct(exit_price, execution_price)
    else:
        gross_return = _pct(execution_price, exit_price)

    entry_slip_pct = abs(_pct(execution_price, float(first[1])))
    exit_slip_pct = abs(_pct(exit_price, exit_raw))
    slippage_pct = entry_slip_pct + exit_slip_pct
    fee_pct = 2.0 * max(0.0, float(signal.fee_bps_per_side)) / 100.0
    funding_pct = float(signal.funding_cost_pct)
    net_return = gross_return - fee_pct - funding_pct
    gross_r = gross_return / float(signal.stop_pct)
    net_r = net_return / float(signal.stop_pct)

    return PaperResult(
        signal.signal_id, signal.symbol, signal.direction, signal.ts_ms, execution_ts, exit_ts,
        signal_price, execution_price, exit_price, "RESOLVED", reason,
        round(gross_return, 6), round(net_return, 6), round(gross_r, 6), round(net_r, 6),
        round(mfe, 6), round(mae, 6), round(slippage_pct, 6), round(fee_pct, 6),
        round(funding_pct, 6), signal.latency_ms, ambiguous, observed,
        {
            "risk_score": signal.risk_score,
            "fusion_score": signal.fusion_score,
            "ml_probability": signal.ml_probability,
            "quality_tier": signal.quality_tier,
            "expected_value_r": signal.expected_value_r,
            "target_pct": signal.target_pct,
            "stop_pct": signal.stop_pct,
        },
    )


def build_paper_signal(
    row: dict[str, Any],
    *,
    default_stop_pct: float = 2.0,
    default_target_pct: float = 5.0,
    default_horizon_min: int = 60,
    latency_ms: int = 0,
    slippage_bps: float = 2.0,
    fee_bps_per_side: float = 4.0,
) -> PaperSignal:
    """Map a fusion/risk alert row into a paper-trading signal."""
    direction = str(row.get("direction", "long"))
    return PaperSignal(
        signal_id=str(row.get("signal_id") or uuid.uuid4().hex),
        ts_ms=int(row["ts_ms"]),
        symbol=str(row["symbol"]),
        direction=direction,
        entry_price=_px(float(row.get("entry_price") or row.get("close"))),
        stop_pct=float(row.get("stop_pct", default_stop_pct)),
        target_pct=float(row.get("target_pct", default_target_pct)),
        horizon_min=int(row.get("horizon_min", default_horizon_min)),
        risk_score=float(row.get("risk_score", 0.0)),
        fusion_score=float(row.get("fusion_score", 0.0)),
        ml_probability=None if row.get("ml_probability") is None else float(row["ml_probability"]),
        quality_tier=str(row.get("quality_tier", "B")),
        expected_value_r=None if row.get("expected_value_r") is None else float(row["expected_value_r"]),
        slippage_bps=slippage_bps if row.get("slippage_bps") is None else float(row["slippage_bps"]),
        fee_bps_per_side=fee_bps_per_side if row.get("fee_bps_per_side") is None else float(row["fee_bps_per_side"]),
        funding_cost_pct=float(row.get("funding_cost_pct", 0.0)),
        latency_ms=int(row.get("latency_ms", latency_ms)),
    )


def replay_signals(
    signals: Iterable[PaperSignal],
    bars_by_symbol: dict[str, Sequence[tuple[int, float, float, float, float]]],
    *,
    store: PaperTradeStore | None = None,
) -> list[PaperResult]:
    results: list[PaperResult] = []
    for signal in signals:
        bars = bars_by_symbol.get(signal.symbol, ())
        result = simulate_signal(signal, bars)
        results.append(result)
        if store:
            store.write(result)
    return results


def summarize_results(results: Iterable[PaperResult]) -> dict[str, Any]:
    all_results = list(results)
    rows = [r for r in all_results if r.status == "RESOLVED" and r.net_r is not None]
    wins = [r for r in rows if (r.net_r or 0.0) > 0]
    losses = [r for r in rows if (r.net_r or 0.0) <= 0]
    net_r = sum(float(r.net_r) for r in rows)
    avg_net_r = net_r / len(rows) if rows else None
    returns = [float(r.net_return_pct) for r in rows if r.net_return_pct is not None]
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret in returns:
        equity *= 1.0 + ret / 100.0
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
    return {
        "signals": len(all_results),
        "resolved": len(rows),
        "wins": len(wins),
        "losses": len(losses),
        "hit_rate": len(wins) / len(rows) if rows else None,
        "sum_net_r": round(net_r, 6),
        "expectancy_R_net": None if avg_net_r is None else round(avg_net_r, 6),
        "avg_net_return_pct": (sum(returns) / len(returns)) if returns else None,
        "max_drawdown_pct": round(max_dd * 100.0, 6),
        "tp_rate": sum(r.exit_reason == "TP" for r in rows) / len(rows) if rows else None,
        "sl_rate": sum(r.exit_reason == "SL" for r in rows) / len(rows) if rows else None,
        "horizon_rate": sum(r.exit_reason == "HORIZON" for r in rows) / len(rows) if rows else None,
        "ambiguous_rate": sum(r.ambiguous_bar for r in rows) / len(rows) if rows else None,
        "avg_mfe_pct": (sum(r.mfe_pct for r in rows) / len(rows)) if rows else None,
        "avg_mae_pct": (sum(r.mae_pct for r in rows) / len(rows)) if rows else None,
    }

@dataclass(slots=True)
class LivePaperPosition:
    signal: PaperSignal
    execution_ts_ms: int
    execution_price: float
    highest_mfe_pct: float = 0.0
    lowest_mae_pct: float = 0.0
    bars_observed: int = 0


class LivePaperEngine:
    """Stateful live-paper executor fed one closed 1m bar at a time."""

    def __init__(self, store: PaperTradeStore | None = None, *, initial_equity: float = 10_000.0, risk_pct: float = 1.0, max_open_positions: int = 20) -> None:
        if initial_equity <= 0 or risk_pct <= 0 or max_open_positions <= 0:
            raise ValueError("initial_equity, risk_pct and max_open_positions must be positive")
        self.store = store
        self.initial_equity = float(initial_equity)
        self.equity = float(initial_equity)
        self.risk_pct = float(risk_pct)
        self.max_open_positions = int(max_open_positions)
        self.positions: dict[str, LivePaperPosition] = {}
        self.closed: list[PaperResult] = []
        self.equity_peak = self.equity
        self.max_drawdown_pct = 0.0

    def _risk_cash(self) -> float:
        return self.equity * self.risk_pct / 100.0

    def submit(self, signal: PaperSignal) -> str:
        _validate_signal(signal)
        if len(self.positions) >= self.max_open_positions:
            return "BLOCKED_MAX_OPEN_POSITIONS"
        if signal.signal_id in self.positions:
            return "DUPLICATE_SIGNAL"
        if any(p.signal.symbol == signal.symbol and p.signal.direction == signal.direction for p in self.positions.values()):
            return "DUPLICATE_SYMBOL_DIRECTION"
        # Signal is queued. Execution occurs only when a bar reaches signal+latency.
        self.positions[signal.signal_id] = LivePaperPosition(signal=signal, execution_ts_ms=0, execution_price=0.0)
        if self.store:
            self.store.write(PaperResult(
                signal.signal_id, signal.symbol, signal.direction, signal.ts_ms, None, None,
                signal.entry_price, None, None, "OPEN", None, None, None, None, None,
                0.0, 0.0, 0.0, 0.0, 0.0, signal.latency_ms, False, 0,
                {"risk_pct": self.risk_pct, "initial_equity": self.initial_equity, "queued": True},
            ))
        return "QUEUED"

    def on_bar(self, symbol: str, bar: tuple[int, float, float, float, float]) -> list[PaperResult]:
        """Process one closed 1m bar: execute queued signals, then resolve open positions."""
        ts, open_px, high, low, close = int(bar[0]), _px(float(bar[1])), _px(float(bar[2])), _px(float(bar[3])), _px(float(bar[4]))
        finished: list[PaperResult] = []
        for sid, pos in list(self.positions.items()):
            s = pos.signal
            if s.symbol != symbol:
                continue
            if pos.execution_ts_ms == 0:
                if ts < s.ts_ms + s.latency_ms:
                    continue
                pos.execution_ts_ms = ts
                pos.execution_price = _apply_entry_slippage(open_px, s.direction, s.slippage_bps)
            pos.bars_observed += 1
            if s.direction == "long":
                mfe = _pct(high, pos.execution_price)
                mae = _pct(low, pos.execution_price)
                tp = pos.execution_price * (1 + s.target_pct / 100)
                sl = pos.execution_price * (1 - s.stop_pct / 100)
                hit_tp, hit_sl = high >= tp, low <= sl
            else:
                mfe = _pct(pos.execution_price, low)
                mae = _pct(pos.execution_price, high)
                tp = pos.execution_price * (1 - s.target_pct / 100)
                sl = pos.execution_price * (1 + s.stop_pct / 100)
                hit_tp, hit_sl = low <= tp, high >= sl
            pos.highest_mfe_pct = max(pos.highest_mfe_pct, mfe)
            pos.lowest_mae_pct = min(pos.lowest_mae_pct, mae)
            ambiguous = hit_tp and hit_sl
            reason: str | None = None
            raw_exit: float | None = None
            if ambiguous:
                hit_tp = False
                hit_sl = True
            if hit_tp:
                reason, raw_exit = "TP", tp
            elif hit_sl:
                reason, raw_exit = "SL", sl
            elif ts >= pos.execution_ts_ms + s.horizon_min * 60_000:
                reason, raw_exit = "HORIZON", close
            if reason is None:
                continue

            exit_px = _apply_exit_slippage(raw_exit, s.direction, s.slippage_bps)
            gross = _pct(exit_px, pos.execution_price) if s.direction == "long" else _pct(pos.execution_price, exit_px)
            entry_slip = abs(_pct(pos.execution_price, open_px))
            exit_slip = abs(_pct(exit_px, raw_exit))
            slip = entry_slip + exit_slip
            fee_pct = 2 * max(0.0, s.fee_bps_per_side) / 100.0
            funding_pct = s.funding_cost_pct
            net = gross - fee_pct - funding_pct
            gross_r = gross / s.stop_pct
            net_r = net / s.stop_pct
            result = PaperResult(
                s.signal_id, s.symbol, s.direction, s.ts_ms, pos.execution_ts_ms, ts,
                s.entry_price, pos.execution_price, exit_px, "RESOLVED", reason,
                round(gross, 6), round(net, 6), round(gross_r, 6), round(net_r, 6),
                round(pos.highest_mfe_pct, 6), round(pos.lowest_mae_pct, 6), round(slip, 6),
                round(fee_pct, 6), round(funding_pct, 6), s.latency_ms, ambiguous, pos.bars_observed,
                {"risk_pct": self.risk_pct, "equity_before": self.equity, "risk_cash": self._risk_cash(),
                 "quality_tier": s.quality_tier, "fusion_score": s.fusion_score,
                 "ml_probability": s.ml_probability, "expected_value_r": s.expected_value_r},
            )
            self.equity += self._risk_cash() * net_r
            self.equity_peak = max(self.equity_peak, self.equity)
            if self.equity_peak > 0:
                dd = (self.equity / self.equity_peak - 1.0) * 100.0
                self.max_drawdown_pct = min(self.max_drawdown_pct, dd)
            self.closed.append(result)
            if self.store:
                self.store.write(result)
            finished.append(result)
            del self.positions[sid]
        return finished

    def snapshot(self) -> dict[str, Any]:
        return {
            "open_positions": len(self.positions),
            "closed_trades": len(self.closed),
            "initial_equity": self.initial_equity,
            "equity": round(self.equity, 6),
            "equity_return_pct": round((self.equity / self.initial_equity - 1) * 100, 6),
            "max_drawdown_pct": round(self.max_drawdown_pct, 6),
            "risk_pct_per_trade": self.risk_pct,
            "max_open_positions": self.max_open_positions,
        }
