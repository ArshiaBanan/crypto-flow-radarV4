from __future__ import annotations
import csv, json, sqlite3
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

DEFAULT_GROUPS = {
    'all': None,
    'flow_only': ['taker_imbalance'],
    'volume_only': ['volume_z_median'],
    'oi_only': ['oi_change_pct'],
    'momentum_only': ['return_5m_pct'],
    'flow_volume': ['taker_imbalance','volume_z_median'],
    'flow_oi': ['taker_imbalance','oi_change_pct'],
    'flow_volume_oi': ['taker_imbalance','volume_z_median','oi_change_pct'],
}

@dataclass(slots=True)
class FoldResult:
    fold: int; train_start_ms: int; train_end_ms: int; test_start_ms: int; test_end_ms: int
    signals: int; resolved: int; expectancy_R: float|None; hit_rate: float|None
    feature_group: str


def _rows(conn, start_ms, end_ms, symbol=None):
    q = 'SELECT ts_ms,symbol,pump_score,dump_score,features FROM feature_snapshots WHERE ts_ms>=? AND ts_ms<?'
    args=[start_ms,end_ms]
    if symbol: q += ' AND symbol=?'; args.append(symbol)
    q += ' ORDER BY ts_ms'
    return conn.execute(q,args).fetchall()


def feature_ablation(db_path: str, output_csv: str, start_ms: int|None=None, end_ms: int|None=None) -> list[dict]:
    conn=sqlite3.connect(db_path)
    lo,hi=conn.execute('SELECT MIN(ts_ms),MAX(ts_ms) FROM feature_snapshots').fetchone()
    start_ms=start_ms if start_ms is not None else lo; end_ms=end_ms if end_ms is not None else hi+1
    rows=_rows(conn,start_ms,end_ms); conn.close()
    out=[]
    for name, fields in DEFAULT_GROUPS.items():
        scores=[]
        for ts,sym,pump,dump,raw in rows:
            f=json.loads(raw or '{}')
            if fields is None: score=max(float(pump or 0),float(dump or 0))
            else:
                vals=[float(f.get(k) or 0) for k in fields]
                score=min(100.0,50.0+sum(abs(v) for v in vals)*10.0)
            scores.append(score)
        out.append({'feature_group':name,'rows':len(scores),'mean_score':sum(scores)/len(scores) if scores else None,
                    'p90_score':sorted(scores)[int(.9*len(scores))-1] if scores else None})
    Path(output_csv).parent.mkdir(parents=True,exist_ok=True)
    with open(output_csv,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=out[0].keys()); w.writeheader(); w.writerows(out)
    return out


def walk_forward_splits(start_ms:int,end_ms:int,train_days:int=28,test_days:int=7,step_days:int=7):
    day=86_400_000; train=train_days*day; test=test_days*day; step=step_days*day
    cur=start_ms+train
    fold=0
    while cur+test<=end_ms:
        yield fold,cur-train,cur,cur,cur+test
        fold+=1; cur+=step


def walk_forward_report(db_path:str, output_csv:str, train_days:int=28,test_days:int=7,step_days:int=7,min_score:float=80.0)->list[dict]:
    conn=sqlite3.connect(db_path)
    lo,hi=conn.execute('SELECT MIN(ts_ms),MAX(ts_ms) FROM feature_snapshots').fetchone()
    if lo is None: return []
    # Research harness: folds are chronological; threshold is fixed before each test fold.
    results=[]
    for fold,tr0,tr1,te0,te1 in walk_forward_splits(lo,hi+1,train_days,test_days,step_days):
        test_rows=_rows(conn,te0,te1)
        sig=[r for r in test_rows if max(float(r[2] or 0),float(r[3] or 0))>=min_score]
        vals=[]
        for r in sig:
            f=json.loads(r[4] or '{}'); ret=float(f.get('return_15m_pct') or 0)
            direction='long' if float(r[2] or 0)>=float(r[3] or 0) else 'short'
            vals.append(ret/2 if direction=='long' else -ret/2)
        resolved=len(vals); exp=sum(vals)/resolved if resolved else None
        hit=sum(v>0 for v in vals)/resolved if resolved else None
        results.append(asdict(FoldResult(fold,tr0,tr1,te0,te1,len(sig),resolved,exp,hit,'all')))
    conn.close()
    Path(output_csv).parent.mkdir(parents=True,exist_ok=True)
    with open(output_csv,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=results[0].keys() if results else [x for x in FoldResult.__dataclass_fields__]); w.writeheader(); w.writerows(results)
    return results
