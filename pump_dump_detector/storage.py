"""
Phase 5 — persistent storage (SQLite) + the data layer backtest.py queries.

Replaces every in-memory, restart-losing piece of state from Phases 1-4:
  - open interest history (was main.py's `_oi_history` dict)
  - Reddit mention history (was social_source.py's `_reddit_mention_history`
    dict — and was documented there as statistically biased, because a
    scan with zero mentions never wrote a "0" anywhere. Fixed here: every
    scan gets a row in `scans` regardless, so the zero-mention case is now
    representable and z-scores are computed against the real baseline.)
and adds what Phases 1-4 couldn't do at all: a full scan history you can
query later, which is what makes backtest.py possible.

SQLite, not Postgres/TimescaleDB, on purpose: it's a single local file,
needs no server to install or keep running, and Python's sqlite3 module is
in the standard library — zero new dependencies. At this project's scan
volume (one process, one exchange, a scan every ~15 minutes) SQLite
comfortably handles years of history. If this ever grows into multiple
scanners writing concurrently, THAT'S the point to migrate to Postgres/
TimescaleDB — the SQL here is plain enough that the migration is mostly a
connection-string change, not a rewrite.

Phase 6 adds a second process reading this same file: dashboard.py, running
alongside main.py --loop. WAL (Write-Ahead Logging) mode is turned on below
specifically for that — it lets a reader see a consistent snapshot while a
writer is mid-transaction, instead of the default mode where a writer
briefly locks readers out. Without it, the dashboard could occasionally
hit "database is locked" during a scan's writes.

Phase 7 adds three precision-filter columns to symbol_snapshots
(confluence, wash_trading_flag, cross_exchange_status — see signal_engine.py)
via the same additive, backward-compatible migration pattern as Phase 6:
_ensure_column adds a column to an existing table without touching its
data, so upgrading from an older database just works. A small market_context
table also appears here, holding one row per scan of market-wide context —
the aggregate funding-rate leverage bias (a free substitute for paid
liquidation-cascade feeds; see config.py) — that isn't tied to any single
symbol, for the dashboard to show alongside the per-symbol table.
"""

import sqlite3
from contextlib import contextmanager
import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    scan_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at  TEXT NOT NULL,      -- ISO 8601 UTC, for humans reading the DB directly
    unix_ts     INTEGER NOT NULL    -- same moment, for fast range queries
);

CREATE TABLE IF NOT EXISTS symbol_snapshots (
    scan_id                INTEGER NOT NULL REFERENCES scans(scan_id),
    symbol                 TEXT NOT NULL,
    price                  REAL,
    score                  REAL NOT NULL,
    direction               TEXT NOT NULL,
    volume_z               REAL,
    ob_imbalance            REAL,
    oi_change_pct           REAL,
    funding_rate            REAL,
    whale_net_flow_usd      REAL,
    external_text           TEXT,
    social_text             TEXT,
    confluence               TEXT,     -- 'CONFIRMED' | 'WATCH' | 'LOW_CONFIDENCE' (Phase 7)
    wash_trading_flag        INTEGER,  -- 1 if volume spiked without matching price movement
    cross_exchange_status    TEXT,     -- 'CONFIRMED' | 'CONTRADICTED' | 'NOT_LISTED' | 'SKIPPED'
    PRIMARY KEY (scan_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_symbol ON symbol_snapshots(symbol);
CREATE INDEX IF NOT EXISTS idx_snapshots_score  ON symbol_snapshots(score);

-- Price for every liquid symbol, every scan — even ones with no signal
-- that particular scan. This is what lets backtest.py find "the price
-- ~24h after a signal fired" for a symbol that wasn't itself flagged
-- again at that later moment.
CREATE TABLE IF NOT EXISTS price_snapshots (
    scan_id  INTEGER NOT NULL,
    symbol   TEXT NOT NULL,
    price    REAL NOT NULL,
    PRIMARY KEY (scan_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_prices_symbol ON price_snapshots(symbol);

CREATE TABLE IF NOT EXISTS oi_latest (
    symbol           TEXT PRIMARY KEY,
    open_interest    REAL NOT NULL,
    updated_scan_id  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS reddit_mentions (
    scan_id  INTEGER NOT NULL,
    symbol   TEXT NOT NULL,
    count    INTEGER NOT NULL,
    PRIMARY KEY (scan_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_reddit_symbol ON reddit_mentions(symbol);

-- One row per scan of market-wide (not per-symbol) context: the aggregate
-- funding-rate leverage bias (see signal_engine.compute_market_funding_bias
-- and the MARKET_BIAS_* comment in config.py). Kept separate from
-- symbol_snapshots because it describes the whole market at that moment,
-- not one coin.
CREATE TABLE IF NOT EXISTS market_context (
    scan_id                  INTEGER PRIMARY KEY REFERENCES scans(scan_id),
    funding_long_share_pct   REAL,
    funding_short_share_pct  REAL,
    bias                     TEXT   -- 'DUMP_CASCADE_RISK' | 'PUMP_SQUEEZE_RISK' | 'NEUTRAL'
);
"""


def _ensure_column(conn, table, column, coltype_default):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype_default}")


@contextmanager
def _connect():
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """
    Creates every table/index if this is a fresh DB file, and migrates an
    existing Phase 1-6 database by adding the three Phase 7 columns if
    they're missing — CREATE TABLE IF NOT EXISTS alone won't add a column
    to a table that already exists from an earlier version of this schema.
    Safe to call every run either way.
    """
    with _connect() as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "symbol_snapshots", "confluence", "TEXT")
        _ensure_column(conn, "symbol_snapshots", "wash_trading_flag", "INTEGER")
        _ensure_column(conn, "symbol_snapshots", "cross_exchange_status", "TEXT")
        _ensure_column(conn, "symbol_snapshots", "rush_order_z", "REAL")
        _ensure_column(conn, "symbol_snapshots", "rush_taker_ratio", "REAL")
        _ensure_column(conn, "market_context", "funding_long_share_pct", "REAL")
        _ensure_column(conn, "market_context", "funding_short_share_pct", "REAL")
        _ensure_column(conn, "market_context", "bias", "TEXT")


def start_scan(scanned_at_iso, unix_ts):
    """Creates the row for a new scan, returns its scan_id (used by every other call below)."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO scans (scanned_at, unix_ts) VALUES (?, ?)",
            (scanned_at_iso, unix_ts),
        )
        return cur.lastrowid


def record_symbol_snapshot(scan_id, row):
    """
    row: the same dict shape main.py builds for print_table/CSV, plus a
    'price' key and the Phase 7 fields ('confluence', 'wash_trading_flag',
    'cross_exchange_status'), all optional. The symbol is normalized to the
    base asset (e.g. "PEPE/USDT" -> "PEPE") so it matches price_snapshots'
    keying — both tables are always joined on the same base-asset symbol.
    """
    base_asset = row["symbol"].split("/")[0]
    with _connect() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO symbol_snapshots
            (scan_id, symbol, price, score, direction, volume_z, ob_imbalance,
             oi_change_pct, funding_rate, whale_net_flow_usd, external_text, social_text,
             confluence, wash_trading_flag, cross_exchange_status, rush_order_z, rush_taker_ratio)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            scan_id, base_asset, row.get("price"), row["score"], row["direction"],
            row.get("volume_z"), row.get("ob_imbalance"), row.get("oi_change_pct"),
            row.get("funding_rate"), row.get("whale_net_flow_usd"),
            row.get("external") or None, row.get("social") or None,
            row.get("confluence"), row.get("wash_trading_flag"), row.get("cross_exchange_status"),
            row.get("rush_order_z"), row.get("rush_taker_ratio"),
        ))


def record_price_snapshots(scan_id, price_by_symbol):
    """price_by_symbol: {base_asset: price} for every symbol in this scan's liquid universe."""
    rows = [(scan_id, sym, price) for sym, price in price_by_symbol.items() if price]
    if not rows:
        return
    with _connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO price_snapshots (scan_id, symbol, price) VALUES (?, ?, ?)",
            rows,
        )


def get_oi(symbol):
    with _connect() as conn:
        row = conn.execute(
            "SELECT open_interest FROM oi_latest WHERE symbol = ?", (symbol,)
        ).fetchone()
        return row["open_interest"] if row else None


def set_oi(symbol, value, scan_id):
    with _connect() as conn:
        conn.execute("""
            INSERT INTO oi_latest (symbol, open_interest, updated_scan_id)
            VALUES (?, ?, ?)
            ON CONFLICT(symbol) DO UPDATE SET
                open_interest = excluded.open_interest,
                updated_scan_id = excluded.updated_scan_id
        """, (symbol, value, scan_id))


def record_reddit_mentions(scan_id, counts_by_symbol):
    if not counts_by_symbol:
        return
    with _connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO reddit_mentions (scan_id, symbol, count) VALUES (?, ?, ?)",
            [(scan_id, sym, count) for sym, count in counts_by_symbol.items()],
        )


def get_reddit_mention_history(symbol, before_scan_id, lookback):
    """
    Zero-padded mention-count history for `symbol`, over the `lookback`
    scans strictly before `before_scan_id` — including scans where it had
    zero mentions. This is the actual fix for the baseline-skew limitation
    documented in Phase 4: a coin that went quiet for a while now correctly
    shows zeros in its history instead of those scans being invisible.
    """
    with _connect() as conn:
        rows = conn.execute("""
            SELECT COALESCE(rm.count, 0) AS count
            FROM (
                SELECT scan_id FROM scans WHERE scan_id < ? ORDER BY scan_id DESC LIMIT ?
            ) recent
            LEFT JOIN reddit_mentions rm
              ON rm.scan_id = recent.scan_id AND rm.symbol = ?
            ORDER BY recent.scan_id ASC
        """, (before_scan_id, lookback, symbol)).fetchall()
        return [r["count"] for r in rows]


def backtest(min_score, forward_hours, direction=None, confluence=None):
    """
    For every historical symbol_snapshot with score >= min_score (optionally
    restricted to one direction and/or one confluence tier — see
    signal_engine.classify_confluence), finds that symbol's price at the
    closest available scan to (signal time + forward_hours) and returns the
    actual forward % return. Signals less than forward_hours old are
    skipped — there's no future price to compare against yet.

    Returns a list of dicts: scan_id, symbol, score, direction, confluence,
    entry_price, exit_price, forward_return_pct.
    """
    with _connect() as conn:
        query = """
            SELECT ss.scan_id, ss.symbol, ss.score, ss.direction, ss.confluence,
                   ss.price AS entry_price, s.unix_ts AS entry_ts
            FROM symbol_snapshots ss
            JOIN scans s ON s.scan_id = ss.scan_id
            WHERE ss.score >= ? AND ss.price IS NOT NULL
        """
        params = [min_score]
        if direction:
            query += " AND ss.direction = ?"
            params.append(direction)
        if confluence:
            query += " AND ss.confluence = ?"
            params.append(confluence)

        signal_rows = conn.execute(query, params).fetchall()

        results = []
        for row in signal_rows:
            target_ts = row["entry_ts"] + forward_hours * 3600
            future = conn.execute("""
                SELECT ps.price
                FROM price_snapshots ps
                JOIN scans s ON s.scan_id = ps.scan_id
                WHERE ps.symbol = ? AND s.unix_ts >= ?
                ORDER BY s.unix_ts ASC LIMIT 1
            """, (row["symbol"], target_ts)).fetchone()

            if not future or not row["entry_price"]:
                continue  # not enough time has passed yet for this signal

            forward_return_pct = (future["price"] - row["entry_price"]) / row["entry_price"] * 100
            results.append({
                "scan_id": row["scan_id"],
                "symbol": row["symbol"],
                "score": row["score"],
                "direction": row["direction"],
                "confluence": row["confluence"],
                "entry_price": row["entry_price"],
                "exit_price": future["price"],
                "forward_return_pct": round(forward_return_pct, 2),
            })

        return results


# --------------------------------------------------- Phase 6: dashboard reads

def get_latest_scan():
    """Returns {scan_id, scanned_at, unix_ts} for the most recent scan, or None if empty."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT scan_id, scanned_at, unix_ts FROM scans ORDER BY scan_id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def get_scan_count():
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) AS n FROM scans").fetchone()["n"]


def get_scan_rows(scan_id):
    """Every symbol_snapshot for one scan, highest score first."""
    with _connect() as conn:
        rows = conn.execute("""
            SELECT symbol, price, score, direction, volume_z, rush_order_z, rush_taker_ratio, ob_imbalance,
                   oi_change_pct, funding_rate, whale_net_flow_usd,
                   external_text, social_text, confluence, wash_trading_flag,
                   cross_exchange_status
            FROM symbol_snapshots
            WHERE scan_id = ?
            ORDER BY score DESC
        """, (scan_id,)).fetchall()
        return [dict(r) for r in rows]


def get_symbol_history(symbol, limit=50):
    """
    Score + price for `symbol` over its last `limit` appearances in
    symbol_snapshots (i.e. scans where it had at least one signal — scans
    where it was quiet aren't in this table, unlike the zero-padded Reddit
    history above). Returned oldest-first, ready to feed straight into a
    line chart.
    """
    with _connect() as conn:
        rows = conn.execute("""
            SELECT s.scanned_at, s.unix_ts, ss.score, ss.price, ss.direction
            FROM symbol_snapshots ss
            JOIN scans s ON s.scan_id = ss.scan_id
            WHERE ss.symbol = ?
            ORDER BY s.unix_ts DESC
            LIMIT ?
        """, (symbol, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]


# --------------------------------------------- Phase 7: market-wide context

def record_market_context(scan_id, funding_long_share_pct, funding_short_share_pct, bias):
    with _connect() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO market_context
            (scan_id, funding_long_share_pct, funding_short_share_pct, bias)
            VALUES (?, ?, ?, ?)
        """, (scan_id, funding_long_share_pct, funding_short_share_pct, bias))


def get_latest_market_context():
    with _connect() as conn:
        row = conn.execute("""
            SELECT mc.funding_long_share_pct, mc.funding_short_share_pct, mc.bias, s.scanned_at
            FROM market_context mc
            JOIN scans s ON s.scan_id = mc.scan_id
            ORDER BY mc.scan_id DESC LIMIT 1
        """).fetchone()
        return dict(row) if row else None


# ------------------------------------------- Phase 9: factor lift (tuning aid)

def _agrees(direction, value):
    """
    Does a signed signal value point the same way as the call?
    True/False when it can be judged, None when it can't (missing or
    exactly zero — no direction to agree or disagree with).
    """
    if value is None or value == 0:
        return None
    return (value > 0) == (direction == "PUMP WATCH")


# (name, fn(call) -> True | False | None). None = this factor doesn't apply
# to this call, so it lands in neither the "with" nor "without" group.
# Presence factors apply to every call; agreement factors only apply where
# the underlying signal actually had a value to agree or disagree with.
_LIFT_FACTORS = [
    ("external_signal present",
     lambda c: bool(c["external_text"])),
    ("whale_flow present",
     lambda c: c["whale_net_flow_usd"] is not None),
    ("social_signal present",
     lambda c: bool(c["social_text"])),
    ("wash-trading flagged",
     lambda c: bool(c["wash_trading_flag"])),
    ("cross-exchange CONFIRMED (vs CONTRADICTED)",
     lambda c: (c["cross_exchange_status"] == "CONFIRMED")
     if c["cross_exchange_status"] in ("CONFIRMED", "CONTRADICTED") else None),
    ("confluence tier CONFIRMED (vs the rest)",
     lambda c: c["confluence"] == "CONFIRMED"),
    ("whale flow agrees with the call",
     lambda c: _agrees(c["direction"], c["whale_net_flow_usd"])),
    ("funding rate agrees with the call",
     lambda c: _agrees(c["direction"], c["funding_rate"])),
    ("order-book imbalance agrees with the call",
     lambda c: _agrees(c["direction"], c["ob_imbalance"])),
    ("rush-order taker-buy direction agrees with the call",
     lambda c: _agrees(c["direction"], (c["rush_taker_ratio"] - 0.5) if c["rush_taker_ratio"] is not None else None)),
]


def _group_stats(group):
    n = len(group)
    if n == 0:
        return {"n": 0, "win_rate": None, "avg_edge": None}
    wins = sum(1 for c in group if c["edge"] > 0)
    return {
        "n": n,
        "win_rate": round(wins / n * 100, 1),
        "avg_edge": round(sum(c["edge"] for c in group) / n, 2),
    }


def factor_lift(min_score, forward_hours):
    """
    For every completed PUMP/DUMP call (score >= min_score, at least
    forward_hours old), computes its "edge": the forward return you'd have
    made following the call (long on PUMP WATCH, short on DUMP WATCH), so
    both directions are comparable on one scale. Then, for each factor in
    _LIFT_FACTORS, compares win rate and average edge between calls WITH
    the factor and calls WITHOUT it.

    This is the tool for tuning WEIGHTS and thresholds with evidence
    instead of guesses: a factor that shows no lift (or negative lift) at a
    healthy sample size isn't earning its weight, and an "agrees with the
    call" factor showing NEGATIVE lift means direction_label is reading
    that signal backwards.

    Returns {"total_calls", "baseline": stats, "factors": [{factor, with,
    without}, ...]} where each stats dict is {n, win_rate, avg_edge}.
    Correlation, not causation — and meaningless at small n (see
    config.LIFT_MIN_RELIABLE_SAMPLE).
    """
    with _connect() as conn:
        rows = conn.execute("""
            SELECT ss.symbol, ss.direction, ss.confluence, ss.price AS entry_price,
                   ss.ob_imbalance, ss.funding_rate, ss.whale_net_flow_usd, ss.rush_taker_ratio,
                   ss.external_text, ss.social_text, ss.wash_trading_flag,
                   ss.cross_exchange_status, s.unix_ts AS entry_ts
            FROM symbol_snapshots ss
            JOIN scans s ON s.scan_id = ss.scan_id
            WHERE ss.score >= ? AND ss.price IS NOT NULL
              AND ss.direction IN ('PUMP WATCH', 'DUMP WATCH')
        """, (min_score,)).fetchall()

        calls = []
        for row in rows:
            future = conn.execute("""
                SELECT ps.price
                FROM price_snapshots ps
                JOIN scans s ON s.scan_id = ps.scan_id
                WHERE ps.symbol = ? AND s.unix_ts >= ?
                ORDER BY s.unix_ts ASC LIMIT 1
            """, (row["symbol"], row["entry_ts"] + forward_hours * 3600)).fetchone()

            if not future or not row["entry_price"]:
                continue

            forward_pct = (future["price"] - row["entry_price"]) / row["entry_price"] * 100
            call = dict(row)
            call["edge"] = forward_pct if row["direction"] == "PUMP WATCH" else -forward_pct
            calls.append(call)

    factors = []
    for name, fn in _LIFT_FACTORS:
        with_group, without_group = [], []
        for call in calls:
            verdict = fn(call)
            if verdict is None:
                continue
            (with_group if verdict else without_group).append(call)
        factors.append({
            "factor": name,
            "with": _group_stats(with_group),
            "without": _group_stats(without_group),
        })

    return {"total_calls": len(calls), "baseline": _group_stats(calls), "factors": factors}
