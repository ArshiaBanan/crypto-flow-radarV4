import json
from dataclasses import replace
from app.research_v10 import ResearchExample
from app.ml_v11 import make_matrix, fit_calibrated_baseline, predict_calibrated


def _examples(n=80):
    out=[]
    for i in range(n):
        f={'taker_imbalance': (i%10)/10, 'volume_z_median': i/20, 'oi_change_pct': (i%6)-2}
        y=1 if f['taker_imbalance']+f['oi_change_pct']*.05 > .35 else 0
        out.append(ResearchExample('v1','BTCUSDT','long',i*60000,100,80,'v1',i*60000+3600000,f,{'h60_tp2_sl2':{'status':'TP' if y else 'SL','signed_future_return_pct':2 if y else -2}}))
    return out


def test_make_matrix_and_labels():
    X,y,kept=make_matrix(_examples(10))
    assert X.shape==(10,13)
    assert set(y)=={0,1}
    assert len(kept)==10


def test_xgb_baseline_calibration_and_prediction():
    xs=_examples(100)
    model,cal,names=fit_calibrated_baseline(xs)
    X,y,_=make_matrix(xs, names)
    p=predict_calibrated(model,cal,X)
    assert len(p)==100
    assert ((p>=0)&(p<=1)).all()
