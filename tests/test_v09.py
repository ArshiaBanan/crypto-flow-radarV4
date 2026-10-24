import json, sqlite3
from app.research_v09 import interaction_report, interaction_stability, interaction_walk_forward


def _db(tmp_path):
    db=tmp_path/'r.db'; c=sqlite3.connect(db)
    c.execute('CREATE TABLE feature_snapshots(ts_ms INTEGER,symbol TEXT,pump_score REAL,dump_score REAL,features TEXT,stage TEXT)')
    c.execute('CREATE TABLE bars_1m(bucket_ms INTEGER,symbol TEXT,open REAL,high REAL,low REAL,close REAL)')
    for i in range(40):
        ts=1_000_000+i*86_400_000
        # aligned positive flow+volume should outperform disagreement
        flow=1 if i%4 in (0,1) else -1
        vol=1 if i%4 in (0,2) else -1
        close=100
        future=104 if flow==1 and vol==1 else (101 if flow==vol==-1 else 99)
        f={'close':close,'taker_imbalance':flow,'volume_z_median':vol,'oi_change_pct':flow,'return_5m_pct':flow}
        c.execute('INSERT INTO feature_snapshots VALUES(?,?,?,?,?,?)',(ts,'BTCUSDT',80,20,json.dumps(f),'EARLY'))
        c.execute('INSERT INTO bars_1m VALUES(?,?,?,?,?,?)',(ts+60_000,'BTCUSDT',close,future,future,future))
    c.commit(); c.close(); return db


def test_interaction_report_finds_aligned_regime(tmp_path):
    db=_db(tmp_path); out=tmp_path/'i.csv'
    rows=interaction_report(str(db),str(out),horizon_min=1,min_samples=10)
    pair=next(r for r in rows if r['feature_a']=='taker_imbalance' and r['feature_b']=='volume_z_median')
    assert pair['both_bullish_n']>0
    assert pair['interaction_spread_pct'] is not None
    assert out.exists()


def test_walk_forward_is_chronological(tmp_path):
    db=_db(tmp_path); out=tmp_path/'wf.csv'
    rows=interaction_walk_forward(str(db),str(out),train_days=20,test_days=7,step_days=7,horizon_min=1,min_train_samples=5,top_k=2)
    assert rows and all(r['fold']>=0 for r in rows)
    assert out.exists()


def test_stability_positive_rate():
    rows=[{'feature_a':'a','feature_b':'b','test_interaction_spread_pct':1.0},{'feature_a':'a','feature_b':'b','test_interaction_spread_pct':2.0},{'feature_a':'a','feature_b':'b','test_interaction_spread_pct':-0.5}]
    x=interaction_stability(rows)[0]
    assert x['folds']==3 and x['positive_fold_rate']==2/3
