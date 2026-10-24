from __future__ import annotations

import argparse
import json

from app.validation_v21 import ValidationPolicy, ValidationStore, load_paper_rows, validate_paper_rows


def main() -> int:
    p = argparse.ArgumentParser(description="Validate Crypto Flow Radar paper-trading cohort")
    p.add_argument("--db", default="data/radar.db")
    p.add_argument("--cohort", default="all")
    p.add_argument("--symbol")
    p.add_argument("--quality-tier")
    p.add_argument("--min-resolved", type=int, default=100)
    p.add_argument("--min-expectancy-r", type=float, default=0.10)
    p.add_argument("--max-drawdown-pct", type=float, default=20.0)
    p.add_argument("--bootstrap-iters", type=int, default=2000)
    p.add_argument("--record", action="store_true")
    args = p.parse_args()
    policy = ValidationPolicy(
        min_resolved=args.min_resolved,
        min_expectancy_r=args.min_expectancy_r,
        max_drawdown_pct=args.max_drawdown_pct,
        bootstrap_iters=args.bootstrap_iters,
    )
    rows = load_paper_rows(args.db, cohort=args.cohort, symbol=args.symbol, quality_tier=args.quality_tier)
    report = validate_paper_rows(rows, cohort=args.cohort, policy=policy)
    out = {
        "status": report.status,
        "cohort": report.cohort,
        "sample_size": report.sample_size,
        "resolved": report.resolved,
        "expectancy_r": report.expectancy_r,
        "expectancy_ci_low": report.expectancy_ci_low,
        "expectancy_ci_high": report.expectancy_ci_high,
        "hit_rate": report.hit_rate,
        "max_drawdown_pct": report.max_drawdown_pct,
        "checks": report.checks,
    }
    if args.record:
        store = ValidationStore(args.db)
        out["report_id"] = store.record(report)
        store.close()
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 0 if report.status in {"PASS", "INSUFFICIENT_DATA"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
