from dataclasses import dataclass
from math import log
from statistics import mean, pstdev

@dataclass(frozen=True)
class Regime:
    label: str
    confidence: float
    btc_return_15m: float
    btc_return_1h: float
    btc_volatility_1h: float
    breadth: float

def _ret(closes, n):
    if len(closes) <= n or closes[-n-1] <= 0: return 0.0
    return closes[-1] / closes[-n-1] - 1.0

def classify_regime(btc_closes, asset_returns=None):
    r15 = _ret(btc_closes, 15)
    r1h = _ret(btc_closes, 60)
    logs=[]
    for a,b in zip(btc_closes[:-1], btc_closes[1:]):
        if a>0 and b>0: logs.append(log(b/a))
    vol = pstdev(logs[-60:]) if len(logs)>=2 else 0.0
    breadth = mean(asset_returns) if asset_returns else 0.0
    # conservative thresholds; confidence is a descriptive strength, not probability
    if r1h >= 0.02 and breadth >= 0.002: label='risk_on_trend'
    elif r1h <= -0.02 and breadth <= -0.002: label='risk_off_trend'
    elif vol >= 0.01: label='high_volatility'
    elif abs(r1h) <= 0.006 and abs(r15) <= 0.003: label='range'
    elif r1h > 0: label='mild_risk_on'
    else: label='mild_risk_off'
    strength=min(1.0, abs(r1h)/0.02 + abs(r15)/0.01 + vol/0.01)/3
    return Regime(label, strength, r15, r1h, vol, breadth)

def advanced_features(closes, volumes=None, highs=None, lows=None):
    out={}
    out['ret_5m']=_ret(closes,5); out['ret_15m']=_ret(closes,15); out['ret_1h']=_ret(closes,60)
    if len(closes)>=20:
        rs=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
        out['realized_vol_20']=pstdev(rs[-20:])
    else: out['realized_vol_20']=0.0
    if highs and lows and len(closes):
        tr=[]
        for i in range(max(1,len(closes)-20),len(closes)):
            tr.append(max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])))
        out['atr20']=mean(tr) if tr else 0.0
        out['range_pct']=((highs[-1]-lows[-1])/closes[-1]) if closes[-1] else 0.0
    else: out['atr20']=0.0; out['range_pct']=0.0
    if volumes and len(volumes)>=20:
        med=sorted(volumes[-20:])[9:11]; base=mean(med) if med else 0
        out['volume_ratio_20']=volumes[-1]/base if base else 0.0
    else: out['volume_ratio_20']=0.0
    return out
