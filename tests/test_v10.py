import json
import sqlite3

from app.research_v10 import build_examples, dataset_summary, purged_walk_forward_splits, build_research_dataset


def _db(tmp_path):
    db = tmp_path / 'v10.db'
    c = sqlite3.connect(db)
    c.execute('CREATE TABLE feature_snapshots(id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER,symbol TEXT,timeframe TEXT,features TEXT,pump_score REAL,dump_score REAL,stage TEXT)')
    c.execute('CREATE TABLE bars_1m(bucket_ms INTEGER,symbol TEXT,open REAL,high REAL,low REAL,close REAL,volume_quote REAL,buy_quote REAL,sell_quote REAL,trades INTEGER)')
    # 36 one-minute signal candidates + 1500 future 1m bars, two symbols.
    for i in range(70):
        ts = 1_000_000 + i * 60_000
        close = 100.0 if i < 50 else 102.0
        f = {'close': close, 'taker_imbalance': 1.0, 'volume_z_median': 2.0}
        score = 85 if i == 0 else 0
        c.execute('INSERT INTO feature_snapshots(ts_ms,symbol,timeframe,features,pump_score,dump_score,stage) VALUES(?,?,?,?,?,?,?)',
                  (ts, 'BTCUSDT', '1m', json.dumps(f), score, 10 if score else 0, 'EARLY'))
    for i in range(1, 200):
        ts = 1_000_000 + i * 60_000
        close = 100.0 + (0.10 * i)
        c.execute('INSERT INTO bars_1m VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (ts, 'BTCUSDT', close - 0.05, close + 0.1, close - 0.1, close, 1000, 600, 400, 10))
    c.commit(); c.close(); return db


def test_multihorizon_labels_are_point_in_time(tmp_path):
    db = _db(tmp_path)
    xs = build_examples(str(db), min_score=80, cooldown_min=15, horizons=(15, 30), tp_targets=(1.0, 2.0), sl_pct=1.0)
    assert len(xs) == 1
    e = xs[0]
    assert e.entry_score == 85
    assert e.label_end_ts_ms == e.ts_ms + 30 * 60_000
    assert 'h15_tp1_sl1' in e.labels
    assert e.labels['h15_tp1_sl1']['signed_future_return_pct'] > 0


def test_incomplete_horizon_is_excluded(tmp_path):
    db = _db(tmp_path)
    xs = build_examples(str(db), min_score=80, horizons=(200,), tp_targets=(1.0,))
    assert xs == []


def test_purged_split_removes_training_examples_whose_labels_cross_test_start(tmp_path):
    db = _db(tmp_path)
    xs = build_examples(str(db), min_score=80, horizons=(30,), tp_targets=(1.0,))
    # Add a synthetic second example with a label end that overlaps the test boundary.
    e = xs[0]
    from dataclasses import replace
    e2 = replace(e, ts_ms=e.ts_ms + 7*86_400_000, label_end_ts_ms=e.ts_ms + 7*86_400_000 + 60_000)
    e3 = replace(e, ts_ms=e.ts_ms + 8*86_400_000, label_end_ts_ms=e.ts_ms + 8*86_400_000 + 60_000)
    xs = list(xs) + [e2, e3]
    folds = purged_walk_forward_splits(xs, train_days=7, test_days=1, step_days=1, embargo_min=0)
    assert folds
    for fold in folds:
        assert all(x.label_end_ts_ms < fold['test_start_ms'] for x in fold['train'])


def test_build_dataset_persists_and_exports(tmp_path):
    db = _db(tmp_path); out = tmp_path / 'dataset.csv'
    result = build_research_dataset(str(db), str(out), min_score=80, horizons=(15,), tp_targets=(1.0,))
    assert result['examples'] == 1
    assert out.exists()
    assert dataset_summary(build_examples(str(db), min_score=80, horizons=(15,), tp_targets=(1.0,)))['examples'] == 1
