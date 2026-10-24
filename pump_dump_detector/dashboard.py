"""
Phase 6 + Phase 7 — live web dashboard.

A small Flask app that reads directly from the same SQLite database
main.py has been writing to since Phase 5 (storage.py) — nothing here talks
to Binance, LiveCoinWatch, CryptoMeter, Etherscan, Reddit, or Telegram
directly. It's a read-only window onto data main.py already collected, so
it stays fast and never risks tripping any exchange rate limit.

Phase 7 adds: the confluence tier (CONFIRMED/WATCH/LOW_CONFIDENCE) and
wash-trading/cross-exchange flags per row, a market-wide leverage-bias
banner (from signal_engine.compute_market_funding_bias), and a confluence
filter on the backtest panel.

Run this ALONGSIDE main.py --loop (in a second terminal), not instead of it:

    python main.py --loop      # terminal 1 — keeps scanning and writing to the DB
    python dashboard.py        # terminal 2 — serves the live view of it

Then open http://127.0.0.1:5000 in a browser. The page polls the API every
few seconds, so leaving it open shows new scans as main.py writes them —
no manual refresh needed.
"""

from flask import Flask, jsonify, render_template, request

import config
import storage

app = Flask(__name__)


@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/latest")
def api_latest():
    scan = storage.get_latest_scan()
    if not scan:
        return jsonify({"scan": None, "rows": [], "scan_count": 0, "market_context": None})

    rows = storage.get_scan_rows(scan["scan_id"])
    return jsonify({
        "scan": scan,
        "rows": rows,
        "scan_count": storage.get_scan_count(),
        "market_context": storage.get_latest_market_context(),
    })


@app.route("/api/history/<symbol>")
def api_history(symbol):
    limit = request.args.get("limit", default=50, type=int)
    return jsonify({"symbol": symbol.upper(), "points": storage.get_symbol_history(symbol.upper(), limit)})


@app.route("/api/backtest")
def api_backtest():
    min_score = request.args.get("min_score", default=config.BACKTEST_DEFAULT_MIN_SCORE, type=float)
    hours = request.args.get("hours", default=config.BACKTEST_DEFAULT_FORWARD_HOURS, type=float)
    direction = request.args.get("direction") or None
    confluence = request.args.get("confluence") or None

    rows = storage.backtest(min_score, hours, direction, confluence)

    def summarize(subset, label):
        if not subset:
            return {"count": 0}
        rets = [r["forward_return_pct"] for r in subset]
        wins = sum(1 for r in subset if (r["forward_return_pct"] > 0) == (label == "PUMP WATCH"))
        return {
            "count": len(subset),
            "avg_return": round(sum(rets) / len(rets), 2),
            "win_rate": round(wins / len(subset) * 100, 1),
            "best": round(max(rets), 2),
            "worst": round(min(rets), 2),
        }

    return jsonify({
        "min_score": min_score,
        "hours": hours,
        "direction": direction,
        "confluence": confluence,
        "total": len(rows),
        "pump": summarize([r for r in rows if r["direction"] == "PUMP WATCH"], "PUMP WATCH"),
        "dump": summarize([r for r in rows if r["direction"] == "DUMP WATCH"], "DUMP WATCH"),
        "top": sorted(rows, key=lambda r: r["score"], reverse=True)[:10],
    })


if __name__ == "__main__":
    storage.init_db()
    app.run(host="127.0.0.1", port=5000, debug=False)
