"""
Backtest: "when the composite score was high, what actually happened next?"
This is the question the whole project exists to answer, and Phases 1-4
had no way to answer it — this is that way.

Run this after main.py --loop has accumulated at least forward_hours worth
of history. A signal that fired 3 hours ago has no 24h-later price to
compare against yet, so with the defaults, results only include signals
at least 24h old.

Usage:
    python backtest.py                             # score>=0.7, 24h forward, both directions
    python backtest.py --min-score 0.6 --hours 12
    python backtest.py --direction "PUMP WATCH"     # only that direction
    python backtest.py --confluence CONFIRMED       # Phase 7: only the top precision tier
    python backtest.py --lift                       # Phase 9: which factors actually improve accuracy?
"""

import argparse
import statistics

import config
import storage


def summarize(label, rows):
    if not rows:
        print(f"\n{label}: no completed signals yet at this threshold/window.")
        return

    rets = [r["forward_return_pct"] for r in rows]
    # A "win" for PUMP WATCH is a positive forward return; for DUMP WATCH,
    # a win is a negative one (the dump-lean actually played out).
    wins = sum(
        1 for r in rows
        if (r["forward_return_pct"] > 0) == (label == "PUMP WATCH")
    )

    print(f"\n{label}: {len(rows)} signals")
    print(f"  avg forward return:    {statistics.mean(rets):+.2f}%")
    print(f"  median forward return: {statistics.median(rets):+.2f}%")
    print(f"  best / worst:          {max(rets):+.2f}% / {min(rets):+.2f}%")
    print(f"  win rate:              {wins / len(rows) * 100:.1f}%")


def print_lift(min_score, hours):
    """
    Phase 9 tuning aid: for each factor, win rate and average edge WITH vs
    WITHOUT it, over completed PUMP/DUMP calls. "Edge" is the return you'd
    have made following the call (long on PUMP WATCH, short on DUMP WATCH).
    """
    result = storage.factor_lift(min_score, hours)
    base = result["baseline"]

    print(f"=== Factor lift: score >= {min_score}, {hours}h forward "
          f"({result['total_calls']} completed calls) ===")
    if not result["total_calls"]:
        print("\nNo completed PUMP/DUMP calls at this threshold/window yet — let "
              "main.py --loop keep running and try again later.")
        return

    print(f"Baseline (every call): win rate {base['win_rate']}%, avg edge {base['avg_edge']:+.2f}%\n")

    def cell(stats):
        if not stats["n"]:
            return "      —      "
        flag = "*" if stats["n"] < config.LIFT_MIN_RELIABLE_SAMPLE else " "
        return f"{stats['win_rate']:>5.1f}% n={stats['n']:<4}{flag}"

    print(f"{'FACTOR':<46}{'WITH':<20}{'WITHOUT':<20}LIFT (win rate)")
    print("-" * 100)
    for f in result["factors"]:
        w, wo = f["with"], f["without"]
        if w["n"] and wo["n"]:
            lift = f"{w['win_rate'] - wo['win_rate']:+.1f} pts"
        else:
            lift = "n/a"
        print(f"{f['factor']:<46}{cell(w):<20}{cell(wo):<20}{lift}")

    print(f"\n* = fewer than {config.LIFT_MIN_RELIABLE_SAMPLE} calls in that group — too few to trust; "
          "a big lift there can be pure luck.")
    print("Lift shows correlation, not cause. A factor with no lift (or negative lift) at a healthy "
          "sample size isn't earning its weight in config.WEIGHTS; an 'agrees with the call' row "
          "with NEGATIVE lift means signal_engine.direction_label is reading that signal backwards.")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-score", type=float, default=config.BACKTEST_DEFAULT_MIN_SCORE,
                         help=f"minimum composite score to count as a signal (default {config.BACKTEST_DEFAULT_MIN_SCORE})")
    parser.add_argument("--hours", type=float, default=config.BACKTEST_DEFAULT_FORWARD_HOURS,
                         help=f"forward window in hours (default {config.BACKTEST_DEFAULT_FORWARD_HOURS})")
    parser.add_argument("--direction", choices=["PUMP WATCH", "DUMP WATCH", "NEUTRAL"], default=None,
                         help="restrict to one direction (default: both PUMP WATCH and DUMP WATCH)")
    parser.add_argument("--confluence", choices=["CONFIRMED", "WATCH", "LOW_CONFIDENCE"], default=None,
                         help="Phase 7: restrict to one confluence tier (default: all tiers)")
    parser.add_argument("--lift", action="store_true",
                         help="Phase 9: compare win rates WITH vs WITHOUT each factor, to tune WEIGHTS with evidence")
    args = parser.parse_args()

    storage.init_db()
    if args.lift:
        print_lift(args.min_score, args.hours)
        return

    rows = storage.backtest(args.min_score, args.hours, args.direction, args.confluence)

    tier_note = f", confluence={args.confluence}" if args.confluence else ""
    print(f"=== Backtest: score >= {args.min_score}, {args.hours}h forward{tier_note} "
          f"({len(rows)} completed signals) ===")

    if not rows:
        print("\nNo completed signals at this threshold/window yet. Either nothing "
              "scored this high, or not enough time has passed since it did — "
              "let main.py --loop keep running and try again later.")
        return

    if args.direction:
        summarize(args.direction, rows)
    else:
        summarize("PUMP WATCH", [r for r in rows if r["direction"] == "PUMP WATCH"])
        summarize("DUMP WATCH", [r for r in rows if r["direction"] == "DUMP WATCH"])

    top5 = sorted(rows, key=lambda r: r["score"], reverse=True)[:5]
    print("\nTop 5 highest-score signals in this window:")
    for r in top5:
        print(f"  {r['symbol']:<10} score={r['score']:.2f}  {r['direction']:<12} "
              f"{r['forward_return_pct']:+.2f}% over {args.hours}h")


if __name__ == "__main__":
    main()
