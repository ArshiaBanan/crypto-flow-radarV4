from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

from app.validation_v21 import ValidationPolicy, ValidationReport, validate_paper_rows

SCHEMA_VERSION = "live_validation_v33"


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value).encode("utf-8")).hexdigest()


def _row_ts(row: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


@dataclass(frozen=True, slots=True)
class CohortSpecV33:
    name: str = "all"
    symbol: str | None = None
    quality_tier: str | None = None
    direction: str | None = None

    def matches(self, row: dict[str, Any]) -> bool:
        if self.symbol and str(row.get("symbol", "")).lower() != self.symbol.lower():
            return False
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        tier = row.get("quality_tier") or metadata.get("quality_tier")
        if self.quality_tier and str(tier or "") != self.quality_tier:
            return False
        if self.direction and str(row.get("direction", "")).lower() != self.direction.lower():
            return False
        return True


@dataclass(frozen=True, slots=True)
class DatasetIntegrityV33:
    cohort: str
    rows_seen: int
    rows_resolved: int
    unique_signal_ids: int
    duplicate_signal_ids: int
    invalid_temporal_rows: int
    digest: str
    earliest_entry_ts_ms: int | None
    latest_entry_ts_ms: int | None
    earliest_resolution_ts_ms: int | None
    latest_resolution_ts_ms: int | None
    valid: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ValidationRunV33:
    run_id: str
    created_at_ms: int
    cohort: str
    dataset_digest: str
    integrity: DatasetIntegrityV33
    report: dict[str, Any]
    decision: str
    reasons: tuple[str, ...]
    config_hash: str
    model_context: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class LivePaperValidationStoreV33:
    """Immutable-ish audit store for dataset manifests and validation runs.

    Rows are append-only by run_id. The same dataset digest + cohort can be re-run,
    but a new run receives a new run_id so historical decisions remain traceable.
    """

    def __init__(self, path: str = "data/radar.db") -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self._init()

    def _init(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS validation_datasets_v33 (
                    dataset_digest TEXT PRIMARY KEY,
                    cohort TEXT NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    integrity_json TEXT NOT NULL,
                    row_ids_json TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    model_context_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_validation_datasets_v33_cohort
                    ON validation_datasets_v33(cohort, created_at_ms DESC);

                CREATE TABLE IF NOT EXISTS validation_runs_v33 (
                    run_id TEXT PRIMARY KEY,
                    created_at_ms INTEGER NOT NULL,
                    cohort TEXT NOT NULL,
                    dataset_digest TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    model_context TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_validation_runs_v33_cohort
                    ON validation_runs_v33(cohort, created_at_ms DESC);
                CREATE INDEX IF NOT EXISTS ix_validation_runs_v33_decision
                    ON validation_runs_v33(decision, created_at_ms DESC);
                """
            )

    def save_dataset(
        self,
        *,
        digest: str,
        cohort: str,
        integrity: DatasetIntegrityV33,
        row_ids: list[str],
        config_hash: str,
        model_context: dict[str, Any],
    ) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO validation_datasets_v33(dataset_digest,cohort,created_at_ms,integrity_json,row_ids_json,config_hash,model_context_json) VALUES(?,?,?,?,?,?,?)",
                (
                    digest,
                    cohort,
                    int(time.time() * 1000),
                    _canon(integrity.as_dict()),
                    _canon(row_ids),
                    config_hash,
                    _canon(model_context),
                ),
            )

    def save_run(self, run: ValidationRunV33) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO validation_runs_v33(run_id,created_at_ms,cohort,dataset_digest,decision,payload,config_hash,model_context) VALUES(?,?,?,?,?,?,?,?)",
                (
                    run.run_id,
                    run.created_at_ms,
                    run.cohort,
                    run.dataset_digest,
                    run.decision,
                    _canon(run.as_dict()),
                    run.config_hash,
                    _canon(run.model_context),
                ),
            )

    def recent(self, limit: int = 50, cohort: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        if cohort:
            rows = self.conn.execute(
                "SELECT payload FROM validation_runs_v33 WHERE cohort=? ORDER BY created_at_ms DESC LIMIT ?",
                (cohort, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT payload FROM validation_runs_v33 ORDER BY created_at_ms DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def latest(self, cohort: str = "all") -> dict[str, Any] | None:
        rows = self.recent(1, cohort=cohort)
        return rows[0] if rows else None

    def status(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "datasets": int(self.conn.execute("SELECT COUNT(*) FROM validation_datasets_v33").fetchone()[0]),
            "runs": int(self.conn.execute("SELECT COUNT(*) FROM validation_runs_v33").fetchone()[0]),
            "latest": self.latest(),
        }


class LivePaperValidationHarnessV33:
    def __init__(self, paper_db_path: str, store: LivePaperValidationStoreV33 | None = None) -> None:
        self.paper_db_path = str(paper_db_path)
        self.store = store or LivePaperValidationStoreV33(paper_db_path)

    def _load_rows(self) -> list[dict[str, Any]]:
        conn = sqlite3.connect(self.paper_db_path)
        try:
            cols = [str(x[1]) for x in conn.execute("PRAGMA table_info(paper_trades)").fetchall()]
            if not cols:
                return []
            cur = conn.execute("SELECT * FROM paper_trades ORDER BY rowid")
            out = []
            for values in cur.fetchall():
                row = dict(zip(cols, values))
                try:
                    row["metadata"] = json.loads(row.get("metadata") or "{}")
                except json.JSONDecodeError:
                    row["metadata"] = {}
                out.append(row)
            return out
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

    @staticmethod
    def _integrity(rows: list[dict[str, Any]], cohort: CohortSpecV33) -> tuple[DatasetIntegrityV33, list[str]]:
        matched = [r for r in rows if cohort.matches(r)]
        ids: list[str] = []
        invalid_temporal = 0
        entries: list[int] = []
        resolutions: list[int] = []
        canonical_rows: list[dict[str, Any]] = []
        for r in matched:
            sid = str(r.get("signal_id") or r.get("id") or "")
            if sid:
                ids.append(sid)
            entry = _row_ts(r, "entry_ts_ms", "ts_ms", "signal_ts_ms")
            resolved = _row_ts(r, "exit_ts_ms", "resolved_ts_ms", "close_ts_ms", "resolution_ts_ms")
            if entry is not None:
                entries.append(entry)
            if resolved is not None:
                resolutions.append(resolved)
            if entry is not None and resolved is not None and resolved < entry:
                invalid_temporal += 1
            canonical_rows.append({k: r[k] for k in sorted(r) if k not in {"payload"}})
        row_ids = sorted(ids)
        duplicate_count = len(ids) - len(set(ids))
        digest = _sha({"cohort": cohort.name, "rows": canonical_rows})
        integrity = DatasetIntegrityV33(
            cohort=cohort.name,
            rows_seen=len(matched),
            rows_resolved=sum(1 for r in matched if str(r.get("status", "RESOLVED")).upper() in {"RESOLVED", "CLOSED", "DONE"}),
            unique_signal_ids=len(set(ids)),
            duplicate_signal_ids=max(0, duplicate_count),
            invalid_temporal_rows=invalid_temporal,
            digest=digest,
            earliest_entry_ts_ms=min(entries) if entries else None,
            latest_entry_ts_ms=max(entries) if entries else None,
            earliest_resolution_ts_ms=min(resolutions) if resolutions else None,
            latest_resolution_ts_ms=max(resolutions) if resolutions else None,
            valid=(duplicate_count == 0 and invalid_temporal == 0),
        )
        return integrity, row_ids

    @staticmethod
    def _filter_for_validation(rows: list[dict[str, Any]], cohort: CohortSpecV33) -> list[dict[str, Any]]:
        matched = [r for r in rows if cohort.matches(r)]
        out: list[dict[str, Any]] = []
        for raw in matched:
            r = dict(raw)
            metadata = r.get("metadata") if isinstance(r.get("metadata"), dict) else {}
            for key in ("ml_probability", "quality_tier", "fusion_score", "risk_score", "expected_value_r"):
                if r.get(key) is None and metadata.get(key) is not None:
                    r[key] = metadata.get(key)
            status = str(r.get("status", "RESOLVED")).upper()
            if status not in {"RESOLVED", "CLOSED", "DONE"}:
                continue
            entry = _row_ts(r, "entry_ts_ms", "ts_ms", "signal_ts_ms")
            resolved = _row_ts(r, "exit_ts_ms", "resolved_ts_ms", "close_ts_ms", "resolution_ts_ms")
            if entry is not None and resolved is not None and resolved < entry:
                continue
            out.append(r)
        return out

    def run(
        self,
        *,
        cohort: CohortSpecV33 = CohortSpecV33(),
        policy: ValidationPolicy | None = None,
        config_hash: str = "",
        model_context: dict[str, Any] | None = None,
    ) -> ValidationRunV33:
        rows = self._load_rows()
        integrity, row_ids = self._integrity(rows, cohort)
        dataset_digest = integrity.digest
        context = model_context or {}
        self.store.save_dataset(
            digest=dataset_digest,
            cohort=cohort.name,
            integrity=integrity,
            row_ids=row_ids,
            config_hash=config_hash,
            model_context=context,
        )

        reasons: list[str] = []
        if not integrity.valid:
            decision = "INVALID_DATASET"
            reasons.append("DATASET_INTEGRITY_FAILED")
            report = {"status": "FAIL", "cohort": cohort.name, "reason": reasons[0], "integrity": integrity.as_dict()}
        else:
            clean = self._filter_for_validation(rows, cohort)
            report_obj: ValidationReport = validate_paper_rows(clean, cohort=cohort.name, policy=policy or ValidationPolicy())
            report = asdict(report_obj)
            if report_obj.status == "PASS":
                decision = "READY_FOR_MANUAL_REVIEW"
                reasons.append("VALIDATION_POLICY_PASSED")
                reasons.append("MANUAL_PROMOTION_REQUIRED")
            elif report_obj.status == "FAIL":
                decision = "VALIDATION_FAILED"
                reasons.append("VALIDATION_POLICY_FAILED")
            else:
                decision = "INSUFFICIENT_DATA"
                reasons.append("MORE_RESOLVED_LIVE_PAPER_DATA_REQUIRED")

        run = ValidationRunV33(
            run_id=uuid.uuid4().hex,
            created_at_ms=int(time.time() * 1000),
            cohort=cohort.name,
            dataset_digest=dataset_digest,
            integrity=integrity,
            report=report,
            decision=decision,
            reasons=tuple(dict.fromkeys(reasons)),
            config_hash=config_hash,
            model_context=context,
        )
        self.store.save_run(run)
        return run
