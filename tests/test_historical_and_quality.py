from __future__ import annotations

from app.collectors.binance_history import funding_to_sample, kline_to_bar
from app.data_quality import audit_database
from app.storage.sqlite import SQLiteStore


def test_kline_mapping_and_funding_mapping():
    row = [1700000000000, "100", "110", "90", "105", "1000", 1700000059999, "105000", 123, "600", "63000", "0"]
    bar = kline_to_bar(row)
    assert bar["volume_quote"] == 105000.0
    assert bar["buy_quote"] == 63000.0
    assert bar["sell_quote"] == 42000.0
    funding = funding_to_sample({"fundingTime": 1700000400000, "fundingRate": "0.001", "markPrice": "105", "rateType": "Regular"})
    assert funding == (1700000400000, 0.001, 105.0, "Regular")


def test_point_in_time_oi_and_funding(tmp_path):
    db = SQLiteStore(str(tmp_path / "q.db"))
    db.write_oi("btcusdt", 1_000, 100.0)
    db.write_oi("btcusdt", 4_000, 110.0)
    assert round(db.oi_change_pct_at("btcusdt", 4_000, 3_000), 6) == 10.0
    db.write_funding("btcusdt", 2_000, 0.001)
    db.write_funding("btcusdt", 5_000, 0.002)
    assert db.funding_at("btcusdt", 4_000) == 0.001


def test_quality_audit_passes_full_minute_series(tmp_path):
    path = tmp_path / "q.db"
    db = SQLiteStore(str(path))
    for i in range(5):
        ts = i * 60_000
        db.upsert_bar({"symbol":"btcusdt","bucket_ms":ts,"open":1,"high":2,"low":0.5,"close":1.5,"volume_quote":100,"buy_quote":60,"sell_quote":40,"trades":10,"source":"test"})
    report = audit_database(str(path), "btcusdt", 0, 4*60_000)
    assert report.status == "PASS"
    assert report.missing_bars == 0


def test_resample_1m_requires_complete_window():
    from app.features.resample import resample_1m_bars
    bars = []
    for i in range(5):
        bars.append({"symbol":"btcusdt","bucket_ms":i*60_000,"open":10+i,"high":11+i,"low":9+i,"close":10.5+i,"volume_quote":100,"buy_quote":60,"sell_quote":40,"trades":10})
    assert len(resample_1m_bars(bars, 5)) == 1
    assert len(resample_1m_bars(bars[:4], 5)) == 0
