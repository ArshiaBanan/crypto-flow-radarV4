from __future__ import annotations

import csv, json, math, sqlite3
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Iterable

from app.research_v08 import bootstrap_mean_ci

FEATURES = (
    'taker_imbalance', 'volume_z_median', 'oi_change_pct', 'return_5m_pct',
    'orderbook_imbalance', 'funding_rate', 'long_liquidation_usd', 'short_liquidation_usd',
)

@dataclass(frozen=True, slots=True)
class InteractionResult:
    feature_a: str
    feature_b: str
    samples: int
    both_bullish_n: int
    both_bullish_mean_return_pct: float | None
    both_bearish_n: int
    both_bearish_mean_return_pct: float | None
    disagreement_n: int
    disagreement_mean_return_pct: float | None
    interaction_spread_pct: float | None


def _safe(v) -> float:
    try:
        x=float(v)
        return x if math.isfinite(x) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _rows(conn, start_ms, end_ms):
    return conn.execute(
        "SELECT ts_ms,symbol,features FROM feature_snapshots WHERE ts_ms>=? AND ts_ms<? ORDER BY symbol,ts_ms",
        (start_ms,end_ms)).fetchall()


def _future_return(conn, symbol, ts_ms, horizon_min):
    end=ts_ms+horizon_min*60_000
    row=conn.execute(
        "SELECT close FROM bars_1m WHERE symbol=? AND bucket_ms>? AND bucket_ms<=? ORDER BY bucket_ms DESC LIMIT 1",
        (symbol,ts_ms,end)).fetchone()
    if not row: return None
    raw=conn.execute("SELECT json_extract(features,'$.close') FROM feature_snapshots WHERE symbol=? AND ts_ms=? LIMIT 1",(symbol,ts_ms)).fetchone()
    if not raw or _safe(raw[0])<=0: return None
    return (_safe(row[0])/_safe(raw[0])-1)*100


def _sign(v, threshold=0.0):
    return 1 if v>threshold else (-1 if v<threshold else 0)


def _interaction_metrics(records, a, b):
    bb=[]; br=[]; dis=[]
    for f,y in records:
        sa,sb=_sign(_safe(f.get(a))),_sign(_safe(f.get(b)))
        if sa==1 and sb==1: bb.append(y)
        elif sa==-1 and sb==-1: br.append(y)
        elif sa*sb==-1: dis.append(y)
    aligned=mean(bb) if bb else None
    bearish=mean(br) if br else None
    disagreement=mean(dis) if dis else None
    spread=(aligned-disagreement) if aligned is not None and disagreement is not None else None
    return InteractionResult(a,b,len(records),len(bb),aligned,len(br),bearish,len(dis),disagreement,spread)


def interaction_report(db_path: str, output_csv: str, *, horizon_min=60, start_ms=None, end_ms=None, min_samples=20):
    conn=sqlite3.connect(db_path)
    lo,hi=conn.execute('SELECT MIN(ts_ms),MAX(ts_ms) FROM feature_snapshots').fetchone()
    if lo is None: conn.close(); return []
    lo=int(start_ms if start_ms is not None else lo); hi=int(end_ms if end_ms is not None else hi+1)
    records=[]
    for ts,sym,raw in _rows(conn,lo,hi):
        f=json.loads(raw or '{}'); y=_future_return(conn,sym,ts,horizon_min)
        if y is not None: records.append((f,y))
    conn.close()
    out=[]
    for i,a in enumerate(FEATURES):
        for b in FEATURES[i+1:]:
            r=_interaction_metrics(records,a,b)
            if r.samples>=min_samples:
                out.append(r.__dict__ if hasattr(r,'__dict__') else {
                    'feature_a':r.feature_a,'feature_b':r.feature_b,'samples':r.samples,
                    'both_bullish_n':r.both_bullish_n,'both_bullish_mean_return_pct':r.both_bullish_mean_return_pct,
                    'both_bearish_n':r.both_bearish_n,'both_bearish_mean_return_pct':r.both_bearish_mean_return_pct,
                    'disagreement_n':r.disagreement_n,'disagreement_mean_return_pct':r.disagreement_mean_return_pct,
                    'interaction_spread_pct':r.interaction_spread_pct})
    out.sort(key=lambda x: (-math.inf if x['interaction_spread_pct'] is None else -x['interaction_spread_pct']))
    Path(output_csv).parent.mkdir(parents=True,exist_ok=True)
    with open(output_csv,'w',newline='',encoding='utf-8') as f:
        fields=list(out[0].keys()) if out else ['feature_a','feature_b']
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(out)
    return out


def _records_for_period(conn, start_ms, end_ms, horizon_min):
    rec=[]
    for ts,sym,raw in _rows(conn,start_ms,end_ms):
        f=json.loads(raw or '{}'); y=_future_return(conn,sym,ts,horizon_min)
        if y is not None: rec.append((f,y,sym,ts))
    return rec


def interaction_walk_forward(db_path: str, output_csv: str, *, train_days=28, test_days=7, step_days=7,
                             horizon_min=60, min_train_samples=50, top_k=5, start_ms=None, end_ms=None):
    conn=sqlite3.connect(db_path)
    lo,hi=conn.execute('SELECT MIN(ts_ms),MAX(ts_ms) FROM feature_snapshots').fetchone()
    if lo is None: conn.close(); return []
    lo=int(start_ms if start_ms is not None else lo); hi=int(end_ms if end_ms is not None else hi+1)
    day=86_400_000; cur=lo+train_days*day; fold=0; out=[]
    while cur+test_days*day<=hi:
        tr=_records_for_period(conn,cur-train_days*day,cur,horizon_min)
        te=_records_for_period(conn,cur,cur+test_days*day,horizon_min)
        train_pairs=[]
        for i,a in enumerate(FEATURES):
            for b in FEATURES[i+1:]:
                r=_interaction_metrics([(f,y) for f,y,_,_ in tr],a,b)
                if r.samples>=min_train_samples and r.interaction_spread_pct is not None:
                    train_pairs.append((r.interaction_spread_pct,a,b))
        train_pairs.sort(reverse=True)
        selected=train_pairs[:top_k]
        for rank,(train_spread,a,b) in enumerate(selected,1):
            test_r=_interaction_metrics([(f,y) for f,y,_,_ in te],a,b)
            out.append({
                'fold':fold,'rank':rank,'feature_a':a,'feature_b':b,
                'train_interaction_spread_pct':train_spread,
                'test_interaction_spread_pct':test_r.interaction_spread_pct,
                'test_both_bullish_n':test_r.both_bullish_n,
                'test_both_bullish_mean_return_pct':test_r.both_bullish_mean_return_pct,
                'test_disagreement_n':test_r.disagreement_n,
                'test_disagreement_mean_return_pct':test_r.disagreement_mean_return_pct,
            })
        fold+=1; cur+=step_days*day
    conn.close()
    Path(output_csv).parent.mkdir(parents=True,exist_ok=True)
    with open(output_csv,'w',newline='',encoding='utf-8') as f:
        fields=list(out[0].keys()) if out else ['fold','feature_a','feature_b']
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(out)
    return out


def interaction_stability(rows: Iterable[dict]) -> list[dict]:
    groups={}
    for r in rows:
        key=(r['feature_a'],r['feature_b'])
        groups.setdefault(key,[]).append(r.get('test_interaction_spread_pct'))
    out=[]
    for (a,b),vals in groups.items():
        vals=[float(v) for v in vals if v is not None]
        ci=bootstrap_mean_ci(vals,iterations=2000) if vals else {'mean':None,'lo':None,'hi':None,'n':0}
        out.append({'feature_a':a,'feature_b':b,'folds':len(vals),
                    'mean_test_spread_pct':ci['mean'],'bootstrap_lo':ci['lo'],'bootstrap_hi':ci['hi'],
                    'positive_fold_rate':sum(v>0 for v in vals)/len(vals) if vals else None})
    return sorted(out,key=lambda x:(-math.inf if x['bootstrap_lo'] is None else -x['bootstrap_lo']))
