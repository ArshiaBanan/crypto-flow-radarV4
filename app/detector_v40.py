"""Adapter for the user's multi-source Pump/Dump Detector project (v4.0 integration).
Runs the upstream scanner in an isolated subprocess and normalizes its CSV rows
for the existing Crypto Flow Radar frontend. A scan is never represented as live
until it has actually completed successfully.
"""
from __future__ import annotations
import asyncio, csv, os, sys, time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DETECTOR_DIR = ROOT / "pump_dump_detector"
RESULTS_FILE = DETECTOR_DIR / "scan_results.csv"
_lock = asyncio.Lock()
_state: dict[str, Any] = {"enabled": os.getenv("V40_ENABLED", "false").lower() == "true", "status": "IDLE", "last_started_ts": None, "last_completed_ts": None, "last_error": None, "last_exit_code": None, "rows_count": 0, "source": "pump_dump_detector", "note": "Signals are not verified profitable; validation requires forward data."}


def _read_rows() -> list[dict[str, Any]]:
    if not RESULTS_FILE.exists():
        return []
    out = []
    try:
        with RESULTS_FILE.open(newline="", encoding="utf-8") as f:
            for raw in csv.DictReader(f):
                score = _num(raw.get("score"))
                direction = str(raw.get("direction") or "NEUTRAL").upper()
                is_pump = "PUMP" in direction
                is_dump = "DUMP" in direction
                # The upstream detector emits a 0..1 composite score. The UI's
                # 0..100 pump/dump dials are a display normalization, not a probability.
                strength = round(max(0.0, min(100.0, score * 100.0)), 2)
                pump_score = strength if is_pump else 0.0
                dump_score = strength if is_dump else 0.0
                confluence = str(raw.get("confluence") or "LOW_CONFIDENCE")
                stage = ("EARLY" if is_pump and strength >= 70 else "WATCH") if is_pump else ("DISTRIBUTION" if is_dump and strength >= 70 else "WATCH")
                row = {
                    "symbol": str(raw.get("symbol") or "").replace("/", "").lower(),
                    "display_symbol": str(raw.get("symbol") or ""),
                    "price": _num(raw.get("price")), "close": _num(raw.get("price")),
                    "pump_score": round(pump_score, 2), "dump_score": round(dump_score, 2),
                    "detector_score": score, "direction": direction,
                    "stage": stage, "confluence": confluence,
                    "volume_z_median": _num(raw.get("volume_z")),
                    "volume_rvol": None,
                    "oi_change_pct": _num(raw.get("oi_change_pct")),
                    "orderbook_imbalance": _num(raw.get("ob_imbalance")),
                    "rush_order_z": _num(raw.get("rush_order_z")),
                    "taker_buy_ratio": _num(raw.get("rush_taker_ratio")),
                    "anomaly_score": 100.0 if str(raw.get("wash_trading_flag")) == "1" else 0.0,
                    "anomaly_classification": "WASH_TRADING_SUSPECTED" if str(raw.get("wash_trading_flag")) == "1" else "NONE",
                    "liquidity_regime": "unknown", "ret_5m": 0.0, "ts_ms": int((_state.get("last_completed_ts") or time.time()) * 1000),
                    "wash_trading_flag": str(raw.get("wash_trading_flag")) == "1",
                    "external": raw.get("external") or "", "social": raw.get("social") or "",
                    "whale_net_flow_usd": _num(raw.get("whale_net_flow_usd")),
                    "signal_engine": "pump_dump_detector_v40",
                }
                if row["symbol"]:
                    out.append(row)
    except (OSError, csv.Error, UnicodeError) as exc:
        _state["last_error"] = f"Could not read detector CSV: {exc}"
    return out


def _num(value: Any) -> float | None:
    try:
        if value in (None, "", "None", "null"):
            return None
        v = float(value)
        return v if v == v and abs(v) != float("inf") else None
    except (TypeError, ValueError):
        return None


def status() -> dict[str, Any]:
    return {**_state, "results_available": RESULTS_FILE.exists(), "rows_count": len(_read_rows()), "detector_path": "pump_dump_detector/"}


def results(limit: int = 100) -> dict[str, Any]:
    rows = _read_rows()
    rows.sort(key=lambda r: float(r.get("detector_score") or 0), reverse=True)
    return {"source": "pump_dump_detector", "count": len(rows), "rows": rows[:max(1, min(limit, 500))], "status": _state["status"], "last_completed_ts": _state["last_completed_ts"]}


async def run_scan() -> dict[str, Any]:
    if not _state["enabled"]:
        return {"ok": False, "status": "DISABLED", "message": "Set V40_ENABLED=true to enable the integrated detector."}
    if _lock.locked():
        return {"ok": False, "status": "ALREADY_RUNNING", "message": "A detector scan is already running."}
    async with _lock:
        _state.update(status="RUNNING", last_started_ts=time.time(), last_error=None, last_exit_code=None)
        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "main.py", cwd=str(DETECTOR_DIR), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            try:
                output, _ = await asyncio.wait_for(proc.communicate(), timeout=int(os.getenv("V40_SCAN_TIMEOUT_SECONDS", "900")))
            except asyncio.TimeoutError:
                proc.kill(); await proc.communicate()
                raise TimeoutError("Detector scan exceeded V40_SCAN_TIMEOUT_SECONDS")
            _state["last_exit_code"] = proc.returncode
            if proc.returncode != 0:
                tail = (output or b"").decode("utf-8", "replace")[-4000:]
                raise RuntimeError(f"Detector exited {proc.returncode}: {tail}")
            _state.update(status="OK", last_completed_ts=time.time(), last_error=None, rows_count=len(_read_rows()))
            return {"ok": True, "status": "OK", "rows_count": _state["rows_count"], "last_completed_ts": _state["last_completed_ts"], "log_tail": (output or b"").decode("utf-8", "replace")[-2000:]}
        except Exception as exc:
            _state.update(status="ERROR", last_error=str(exc), last_completed_ts=time.time())
            return {"ok": False, "status": "ERROR", "error": str(exc)}


async def background_loop() -> None:
    interval = max(300, int(os.getenv("V40_SCAN_INTERVAL_SECONDS", "900")))
    while True:
        await run_scan()
        await asyncio.sleep(interval)
