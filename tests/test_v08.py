import json
import sqlite3

from app.research_v08 import choose_threshold, score_bucket_report, threshold_report, walk_forward_thresholds
from app.signals.episodes import build_episodes


def _db(tmp_path):
    db = tmp_path / 'r.db'
    c = sqlite3.connect(db)
    c.execute('CREATE TABLE feature_snapshots(ts_ms INTEGER,symbol TEXT,pump_score REAL,dump_score REAL,features TEXT,stage TEXT)')
    c.execute('CREATE TABLE bars_1m(bucket_ms INTEGER,symbol TEXT,open REAL,high REAL,low REAL,close REAL)')
    c.execute('CREATE TABLE funding_samples(ts_ms INTEGER,symbol TEXT,funding_rate REAL)')
    # Two historical signals with different entry scores and profitable futures.
    rows = [
        (1000,'BTCUSDT',82,40,json.dumps({'close':100}), 'EARLY'),
        (2000,'BTCUSDT',92,35,json.dumps({'close':100}), 'IGNITION'),
        (3000,'ETHUSDT',67,40,json.dumps({'close':100}), 'EARLY'),
    ]
    c.executemany('INSERT INTO feature_snapshots VALUES(?,?,?,?,?,?)', rows)
    # Use minute-aligned-ish timestamps far enough after each signal for the evaluator.
    for ts, sym, close in [
        (60000,'BTCUSDT',104),(120000,'BTCUSDT',105),(60000,'ETHUSDT',103),(120000,'ETHUSDT',104),
    ]:
        c.execute('INSERT INTO bars_1m VALUES(?,?,?,?,?,?)',(ts,sym,close,close*1.05,close*0.99,close))
    c.commit(); c.close()
    return db


def test_entry_score_is_first_crossing_score():
    eps = build_episodes([
        {'ts_ms':1,'symbol':'BTCUSDT','pump_score':82,'dump_score':20,'close':100,'stage':'EARLY'},
        {'ts_ms':2,'symbol':'BTCUSDT','pump_score':95,'dump_score':20,'close':101,'stage':'IGNITION'},
    ], min_score=80, cooldown_min=15)
    assert len(eps) == 1
    assert eps[0].entry_score == 82
    assert eps[0].peak_score == 95


def test_choose_threshold_uses_robust_ci():
    rows = [
        {'threshold':80,'resolved':50,'expectancy_R_net':0.2,'bootstrap_lo':0.01},
        {'threshold':90,'resolved':50,'expectancy_R_net':0.4,'bootstrap_lo':-0.10},
        {'threshold':85,'resolved':50,'expectancy_R_net':0.3,'bootstrap_lo':0.05},
    ]
    assert choose_threshold(rows, min_resolved=30)['threshold'] == 85


def test_threshold_report_and_buckets(tmp_path):
    db = _db(tmp_path)
    out1 = tmp_path / 'thresholds.csv'
    rows = threshold_report(str(db), str(out1), thresholds=(60,80,90), cooldown_min=1, horizon_min=1, tp_pct=2, sl_pct=1)
    assert len(rows) == 3 and out1.exists()
    out2 = tmp_path / 'buckets.csv'
    buckets = score_bucket_report(str(db), str(out2), cooldown_min=1, horizon_min=1, tp_pct=2, sl_pct=1)
    assert len(buckets) == 5 and out2.exists()


def test_wfo_emits_folds_on_sufficient_history(tmp_path):
    db = _db(tmp_path)
    c = sqlite3.connect(db)
    # Add wider timestamp history so one 28-day train + 7-day test fold can exist.
    day = 86_400_000
    base = 1_000_000
    for i in range(40):
        ts = base + i*day
        c.execute('INSERT INTO feature_snapshots VALUES(?,?,?,?,?,?)',(ts,'BTCUSDT',82 if i%2==0 else 92,40,json.dumps({'close':100}), 'EARLY'))
        c.execute('INSERT INTO bars_1m VALUES(?,?,?,?,?,?)',(ts+60_000,'BTCUSDT',100,105,99,104))
    c.commit(); c.close()
    out = tmp_path/'wfo.csv'
    rows = walk_forward_thresholds(str(db), str(out), train_days=28, test_days=7, step_days=7, min_resolved_train=1, cooldown_min=1, horizon_min=1, tp_pct=2, sl_pct=1)
    assert rows and out.exists()
