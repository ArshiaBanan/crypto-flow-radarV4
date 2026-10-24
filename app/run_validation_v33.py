from __future__ import annotations

import argparse
import json

from app.live_validation_v33 import CohortSpecV33, LivePaperValidationHarnessV33
from app.validation_v21 import ValidationPolicy


def main() -> int:
    parser = argparse.ArgumentParser(description="Crypto Flow Radar v3.3 live paper validation harness")
    parser.add_argument("--db", default="data/radar.db")
    parser.add_argument("--symbol")
    parser.add_argument("--quality-tier")
    parser.add_argument("--direction")
    parser.add_argument("--cohort", default="all")
    parser.add_argument("--min-resolved", type=int, default=100)
    parser.add_argument("--bootstrap-iters", type=int, default=2000)
    parser.add_argument("--min-expectancy-r", type=float, default=0.10)
    args = parser.parse_args()
    policy = ValidationPolicy(
        min_resolved=args.min_resolved,
        bootstrap_iters=args.bootstrap_iters,
        min_expectancy_r=args.min_expectancy_r,
    )
    cohort = CohortSpecV33(args.cohort, args.symbol, args.quality_tier, args.direction)
    harness = LivePaperValidationHarnessV33(args.db)
    run = harness.run(cohort=cohort, policy=policy)
    print(json.dumps(run.as_dict(), ensure_ascii=False, indent=2, default=str))
    return 0 if run.decision in {"READY_FOR_MANUAL_REVIEW", "INSUFFICIENT_DATA"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
