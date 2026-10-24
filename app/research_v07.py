from __future__ import annotations
import csv, json, math, random, sqlite3
from pathlib import Path
from dataclasses import dataclass, asdict
from statistics import mean, median

FEATURE_GROUPS = {
    'flow': ['taker_imbalance'],
    'volume': ['volume_z_median'],
    'oi': ['oi_change_pct'],
    'momentum': ['return_5m_pct'],
    'flow_volume': ['taker_imbalance','volume_z_median'],
    'flow_oi': ['taker_imbalance','oi_change_pct'],
    'flow_volume_oi': ['taker_imbalance','volume_z_median','oi_change_pct'],
    'all_core': ['taker_imbalance','volume_z_median','oi_change_pct','return_5m_pct'],
}


def _safe(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else 0.0
    except Exception:
        return 0.0


def _rows(conn, start_ms, end_ms, symbol=None):
    q='SELECT ts_ms,symbol,pump_score,dump_score,features FROM feature_snapshots WHERE ts_ms>=? AND ts_ms<?'
    args=[start_ms,end_ms]
    if symbol: q+=' AND symbol=?'; args.append(symbol)
    q+=' ORDER BY ts_ms'
    return conn.execute(q,args).fetchall()


def _future_close(conn, symbol, ts_ms, horizon_min):
    end=ts_ms+horizon_min*60_000
    row=conn.execute('SELECT close FROM bars_1m WHERE symbol=? AND bucket_ms>? AND bucket_ms<=? ORDER BY bucket_ms DESC LIMIT 1',(symbol,ts_ms,end)).fetchone()
    return float(row[0]) if row else None


def _future_extremes(conn, symbol, ts_ms, horizon_min):
    end=ts_ms+horizon_min*60_000
    return conn.execute('SELECT high,low FROM bars_1m WHERE symbol=? AND bucket_ms>? AND bucket_ms<=?',(symbol,ts_ms,end)).fetchall()


def _directional_score(f, fields):
    # Point-in-time descriptive score only; it is NOT the production scorer.
    # Normalize signs so positive values consistently mean bullish evidence.
    signs={'taker_imbalance':1,'volume_z_median':1,'oi_change_pct':1,'return_5m_pct':1}
    vals=[signs[k]*_safe(f.get(k)) for k in fields]
    if not vals: return 0.0
    # Robust clipping prevents one pathological feature from dominating.
    clipped=[max(-3.0,min(3.0,v if k!='oi_change_pct' else v/5.0)) for k,v in zip(fields,vals)]
    return mean(clipped)


def _rank(xs):
    order=sorted(range(len(xs)),key=lambda i: xs[i])
    r=[0.0]*len(xs); i=0
    while i<len(order):
        j=i
        while j+1<len(order) and xs[order[j+1]]==xs[order[i]]: j+=1
        avg=(i+j)/2+1
        for k in range(i,j+1): r[order[k]]=avg
        i=j+1
    return r


def spearman(x,y):
    if len(x)<2: return None
    a,b=_rank(x),_rank(y); ma,mb=mean(a),mean(b)
    num=sum((u-ma)*(v-mb) for u,v in zip(a,b)); den=(sum((u-ma)**2 for u in a)*sum((v-mb)**2 for v in b))**0.5
    return num/den if den else 0.0


def ablation_outcome(db_path, output_csv, horizon_min=60, top_fraction=0.10, start_ms=None, end_ms=None):
    conn=sqlite3.connect(db_path)
    lo,hi=conn.execute('SELECT MIN(ts_ms),MAX(ts_ms) FROM feature_snapshots').fetchone()
    if lo is None: return []
    lo=start_ms if start_ms is not None else lo; hi=(end_ms if end_ms is not None else hi+1)
    rows=_rows(conn,lo,hi)
    records=[]
    for ts,sym,_,_,raw in rows:
        f=json.loads(raw or '{}'); close=_safe(f.get('close'))
        if not close: continue
        fc=_future_close(conn,sym,ts,horizon_min)
        if fc is None: continue
        y=(fc/close-1)*100
        records.append((sym,ts,f,y))
    conn.close()
    out=[]
    for name,fields in FEATURE_GROUPS.items():
        scores=[_directional_score(f,fields) for _,_,f,_ in records]
        ys=[y for *_,y in records]
        k=max(1,int(len(scores)*top_fraction)) if scores else 0
        idx=sorted(range(len(scores)),key=lambda i: scores[i],reverse=True)[:k]
        top=[ys[i] for i in idx]
        out.append({'feature_group':name,'samples':len(scores),'spearman_future_return':spearman(scores,ys),'top_decile_mean_return_pct':mean(top) if top else None,'all_mean_return_pct':mean(ys) if ys else None})
    Path(output_csv).parent.mkdir(parents=True,exist_ok=True)
    with open(output_csv,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=out[0].keys() if out else ['feature_group']); w.writeheader(); w.writerows(out)
    return out


def _episode_signals(conn, min_score, cooldown_min, start_ms, end_ms):
    from app.signals.episodes import build_episodes
    rows=_rows(conn,start_ms,end_ms)
    data=[]
    for ts,sym,pump,dump,raw in rows:
        f=json.loads(raw or '{}'); close=f.get('close')
        if close is not None and (float(pump or 0)>=min_score or float(dump or 0)>=min_score):
            data.append({'ts_ms':ts,'symbol':sym,'pump_score':pump or 0,'dump_score':dump or 0,'stage':json.loads(raw or '{}').get('stage','WATCH'),'close':close})
    eps=build_episodes(data,min_score=min_score,cooldown_min=cooldown_min)
    return eps


def evaluate_episode(conn, ep, horizon_min=60, tp_pct=5.0, sl_pct=2.0):
    from app.backtest import SignalEvent, evaluate_signal
    direction='long' if ep.direction=='long' else 'short'
    s=SignalEvent(ep.start_ts_ms,ep.symbol,ep.entry_score if direction=='long' else 0,ep.entry_score if direction=='short' else 0,ep.stage_at_entry,ep.entry_price)
    bars=conn.execute('SELECT bucket_ms,open,high,low,close FROM bars_1m WHERE symbol=? AND bucket_ms>? AND bucket_ms<=? ORDER BY bucket_ms',(ep.symbol,ep.start_ts_ms,ep.start_ts_ms+horizon_min*60_000)).fetchall()
    funds=conn.execute('SELECT ts_ms,funding_rate FROM funding_samples WHERE symbol=? AND ts_ms>? AND ts_ms<=? ORDER BY ts_ms',(ep.symbol,ep.start_ts_ms,ep.start_ts_ms+horizon_min*60_000)).fetchall()
    return evaluate_signal(s,bars,direction,tp_pct,sl_pct,horizon_min,funding_events=funds)


def walk_forward_outcomes(db_path, output_csv, train_days=28, test_days=7, step_days=7, min_score=80, cooldown_min=15, horizon_min=60, tp_pct=5, sl_pct=2):
    conn=sqlite3.connect(db_path)
    lo,hi=conn.execute('SELECT MIN(ts_ms),MAX(ts_ms) FROM feature_snapshots').fetchone()
    if lo is None: return []
    day=86_400_000; cur=lo+train_days*day; fold=0; out=[]
    while cur+test_days*day<=hi+1:
        te0,te1=cur,cur+test_days*day
        eps=_episode_signals(conn,min_score,cooldown_min,te0,te1)
        rs=[]
        for ep in eps:
            o=evaluate_episode(conn,ep,horizon_min,tp_pct,sl_pct)
            if o.net_r is not None: rs.append(o.net_r)
        out.append({'fold':fold,'train_start_ms':cur-train_days*day,'train_end_ms':cur,'test_start_ms':te0,'test_end_ms':te1,'signals':len(eps),'resolved':len(rs),'expectancy_R_net':mean(rs) if rs else None,'hit_rate':sum(x>0 for x in rs)/len(rs) if rs else None,'median_R':median(rs) if rs else None})
        fold+=1; cur+=step_days*day
    conn.close()
    Path(output_csv).parent.mkdir(parents=True,exist_ok=True)
    with open(output_csv,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=out[0].keys() if out else ['fold']); w.writeheader(); w.writerows(out)
    return out


def bootstrap_mean_ci(values, iterations=5000, seed=42, confidence=0.95):
    vals=[float(v) for v in values if v is not None]
    if not vals: return {'mean':None,'lo':None,'hi':None,'n':0}
    rng=random.Random(seed); n=len(vals); means=[]
    for _ in range(iterations): means.append(sum(vals[rng.randrange(n)] for _ in range(n))/n)
    means.sort(); alpha=(1-confidence)/2
    q=lambda p: means[min(len(means)-1,max(0,int(p*(len(means)-1))))]
    return {'mean':mean(vals),'lo':q(alpha),'hi':q(1-alpha),'n':n}


def monte_carlo_drawdown(values, iterations=5000, seed=42):
    vals=[float(v) for v in values if v is not None]
    if not vals: return {'median_max_drawdown':None,'p95_max_drawdown':None}
    rng=random.Random(seed); dds=[]
    for _ in range(iterations):
        seq=vals[:]; rng.shuffle(seq); eq=peak=dd=0.0
        for r in seq:
            eq+=r; peak=max(peak,eq); dd=max(dd,peak-eq)
        dds.append(dd)
    dds.sort()
    return {'median_max_drawdown':median(dds),'p95_max_drawdown':dds[int(.95*(len(dds)-1))]}


def stability_report(db_path, output_csv, min_score=80, cooldown_min=15, horizon_min=60, tp_pct=5, sl_pct=2):
    conn=sqlite3.connect(db_path); lo,hi=conn.execute('SELECT MIN(ts_ms),MAX(ts_ms) FROM feature_snapshots').fetchone()
    if lo is None: return []
    eps=_episode_signals(conn,min_score,cooldown_min,lo,hi+1)
    buckets={}
    for ep in eps:
        o=evaluate_episode(conn,ep,horizon_min,tp_pct,sl_pct)
        if o.net_r is None: continue
        regime='neutral'
        btc=conn.execute("SELECT features FROM feature_snapshots WHERE symbol='btcusdt' AND ts_ms<=? ORDER BY ts_ms DESC LIMIT 1",(ep.start_ts_ms,)).fetchone()
        if btc:
            bf=json.loads(btc[0] or '{}'); r=_safe(bf.get('return_15m_pct'))
            regime='risk_on' if r>=1 else ('risk_off' if r<=-1 else 'neutral')
        key=(ep.symbol.lower(),regime); buckets.setdefault(key,[]).append(o.net_r)
    conn.close()
    out=[]
    for (sym,reg),vals in sorted(buckets.items()):
        ci=bootstrap_mean_ci(vals,iterations=2000)
        out.append({'symbol':sym,'regime':reg,'n':len(vals),'expectancy_R_net':mean(vals),'hit_rate':sum(v>0 for v in vals)/len(vals),'bootstrap_lo':ci['lo'],'bootstrap_hi':ci['hi'],**monte_carlo_drawdown(vals,iterations=2000)})
    Path(output_csv).parent.mkdir(parents=True,exist_ok=True)
    with open(output_csv,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=out[0].keys() if out else ['symbol']); w.writeheader(); w.writerows(out)
    return out
