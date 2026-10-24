import csv
from app import detector_v40


def test_v40_csv_rows_normalize_for_frontend(tmp_path, monkeypatch):
    p = tmp_path / 'scan_results.csv'
    with p.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['symbol','price','score','direction','volume_z','rush_order_z','rush_taker_ratio','ob_imbalance','oi_change_pct','funding_rate','external','social','whale_net_flow_usd','confluence','wash_trading_flag'])
        w.writeheader()
        w.writerow({'symbol':'BTC/USDT','price':'60000','score':'0.82','direction':'PUMP WATCH','volume_z':'3.1','rush_order_z':'4.2','rush_taker_ratio':'0.72','ob_imbalance':'0.3','oi_change_pct':'5.0','funding_rate':'0.0001','external':'LCW pump','social':'CG trending','whale_net_flow_usd':'250000','confluence':'CONFIRMED','wash_trading_flag':'0'})
    monkeypatch.setattr(detector_v40, 'RESULTS_FILE', p)
    rows = detector_v40._read_rows()
    assert len(rows) == 1
    row = rows[0]
    assert row['symbol'] == 'btcusdt'
    assert row['display_symbol'] == 'BTC/USDT'
    assert row['pump_score'] == 82.0
    assert row['dump_score'] < row['pump_score']
    assert row['confluence'] == 'CONFIRMED'
    assert row['signal_engine'] == 'pump_dump_detector_v40'


def test_v40_disabled_scan_is_honest(monkeypatch):
    monkeypatch.setitem(detector_v40._state, 'enabled', False)
    import asyncio
    result = asyncio.run(detector_v40.run_scan())
    assert result['ok'] is False
    assert result['status'] == 'DISABLED'
