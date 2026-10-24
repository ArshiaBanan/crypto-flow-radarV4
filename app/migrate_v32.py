from __future__ import annotations

import argparse
import json
from typing import Any

from app.storage_v32 import DEFAULT_TABLES, PostgreSQLLedgerV32, SQLiteOutboxV32


def main() -> int:
    ap = argparse.ArgumentParser(description="Crypto Flow Radar v3.2 full ledger migration/reconciliation")
    ap.add_argument("--sqlite", default="data/radar.db")
    ap.add_argument("--postgres", required=True)
    ap.add_argument("--tables", default=",")
    args = ap.parse_args()
    tables = tuple(x.strip() for x in args.tables.split(",") if x.strip()) or DEFAULT_TABLES
    outbox = SQLiteOutboxV32(args.sqlite, tables)
    pg = PostgreSQLLedgerV32(args.postgres)
    result = pg.apply_snapshot_from_sqlite(outbox, tables)
    print(json.dumps(result, indent=2, sort_keys=True))
    outbox.close()
    pg.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
