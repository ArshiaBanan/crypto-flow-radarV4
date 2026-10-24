import sqlite3, json
from app.research import walk_forward_splits, feature_ablation

def test_walk_forward_no_overlap():
    xs=list(walk_forward_splits(0,60*86_400_000,train_days=10,test_days=5,step_days=5))
    assert xs
    for _,a,b,c,d in xs: assert a < b == c < d

def test_ablation(tmp_path):
    db=tmp_path/'x.db'; c=sqlite3.connect(db)
    c.execute('CREATE TABLE feature_snapshots(ts_ms INTEGER,symbol TEXT,pump_score REAL,dump_score REAL,features TEXT)')
    c.execute('INSERT INTO feature_snapshots VALUES(?,?,?,?,?)',(1,'BTCUSDT',85,40,json.dumps({'taker_imbalance':.2,'volume_z_median':2})))
    c.commit(); c.close()
    out=feature_ablation(str(db),str(tmp_path/'a.csv'))
    assert len(out)==len(__import__('app.research',fromlist=['DEFAULT_GROUPS']).DEFAULT_GROUPS)
