from app.regime import classify_regime, advanced_features

def test_regime_does_not_invent_btc_context():
    r=classify_regime([100]*61)
    assert r.label=='range'
    assert r.confidence==0

def test_regime_risk_on():
    closes=[100+i*0.05 for i in range(61)]
    assets=[0.01,0.012,0.008]
    assert classify_regime(closes,assets).label=='risk_on_trend'

def test_advanced_features():
    closes=list(range(1,82)); vols=[100]*20+[200]*61
    f=advanced_features(closes,vols)
    assert f['ret_15m']>0 and f['volume_ratio_20']>=1
