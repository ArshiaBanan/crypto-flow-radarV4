from app.research_v10 import ResearchExample
from app.ml_v15 import FEATURES, make_matrix, fit_regime_aware_ensemble, _predict_members, regime_of


def _examples(n=180):
    out=[]
    for i in range(n):
        regime = 'risk_on_trend' if i % 3 == 0 else 'range' if i % 3 == 1 else 'risk_off_trend'
        taker = ((i % 10) - 5) / 5
        oi = ((i % 8) - 4) * .8
        y = int((taker + 0.15*oi + (0.25 if regime == 'risk_on_trend' else -0.1 if regime == 'risk_off_trend' else 0)) > 0.0)
        f = {
            'taker_imbalance': taker, 'volume_z_median': i/40, 'oi_change_pct': oi,
            'funding_rate': 0.0001, 'return_1m_pct': taker*0.5,
            'return_5m_pct': taker*0.8, 'return_15m_pct': taker,
            'rvol_1m_vs_history': 1.0 + abs(taker), 'buy_share': 0.5+taker*.2,
            'anomaly_score': 10.0 if regime == 'range' else 5.0,
            'organic_score': 70.0, 'caution_score': 10.0,
            'regime_label': regime, 'regime_confidence': .5,
            'btc_return_15m': .003 if regime == 'risk_on_trend' else -.003 if regime == 'risk_off_trend' else 0.0,
            'btc_return_1h': .01 if regime == 'risk_on_trend' else -.01 if regime == 'risk_off_trend' else 0.0,
            'btc_volatility_1h': .002, 'breadth': .2 if regime == 'risk_on_trend' else -.2 if regime == 'risk_off_trend' else 0.0,
        }
        out.append(ResearchExample('v1','BTCUSDT','long',i*60000,100,80,'v1',i*60000+3600000,f,{'h60_tp2_sl2':{'status':'TP' if y else 'SL','signed_future_return_pct':2 if y else -2}}))
    return out


def test_matrix_has_regime_and_advanced_features():
    xs=_examples(20)
    X,y,kept=make_matrix(xs)
    assert X.shape == (20, len(FEATURES))
    assert set(y) == {0,1}
    assert len(kept)==20
    assert regime_of(kept[0]) == 'risk_on_trend'


def test_regime_aware_ensemble_fits_and_predicts():
    xs=_examples(180)
    bundle=fit_regime_aware_ensemble(xs, min_regime_train=50)
    X,y,_=make_matrix(xs, bundle.feature_names)
    p=_predict_members(bundle.global_members, X)
    assert len(bundle.global_members) == 3
    assert ((p>=0)&(p<=1)).all()
    assert 'risk_on_trend' in bundle.regime_members


def test_small_regime_falls_back_to_global():
    xs=_examples(90)
    bundle=fit_regime_aware_ensemble(xs, min_regime_train=100)
    assert bundle.regime_members == {}


def test_live_inference_does_not_require_labels(tmp_path):
    from app.ml_v15 import feature_vector, predict_feature_dict, save_bundle, load_bundle
    xs=_examples(180)
    bundle=fit_regime_aware_ensemble(xs, min_regime_train=50)
    f=dict(xs[0].features)
    X=feature_vector(f, entry_score=80, feature_names=bundle.feature_names)
    assert X.shape == (1, len(bundle.feature_names))
    p1=predict_feature_dict(bundle, f, entry_score=80)
    assert 0 <= p1 <= 1
    path=tmp_path/'bundle.joblib'
    save_bundle(bundle, str(path))
    loaded=load_bundle(str(path))
    p2=predict_feature_dict(loaded, f, entry_score=80)
    assert abs(p1-p2) < 1e-12
