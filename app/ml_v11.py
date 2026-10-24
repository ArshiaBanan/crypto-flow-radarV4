from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Sequence

import numpy as np

FEATURES = (
    'taker_imbalance','volume_z_median','oi_change_pct','funding_rate',
    'orderbook_imbalance','return_1m_pct','return_5m_pct','return_15m_pct',
    'rvol_1m_vs_history','buy_share','short_liquidation_usd','long_liquidation_usd',
    'entry_score'
)

@dataclass(slots=True)
class FoldMLResult:
    fold: int
    train: int
    calibration: int
    test: int
    auc: float | None
    brier: float | None
    logloss: float | None
    positive_rate: float | None
    mean_pred: float | None


def _label(ex, key: str) -> int | None:
    x = ex.labels.get(key)
    if not isinstance(x, dict): return None
    # A positive label is a TP hit or positive signed return when the barrier times out.
    status = x.get('status')
    if status == 'TP': return 1
    if status == 'SL': return 0
    r = x.get('signed_future_return_pct')
    return None if r is None else int(float(r) > 0)


def make_matrix(examples, feature_names: Sequence[str] = FEATURES, label_key: str = 'h60_tp2_sl2'):
    X, y, kept = [], [], []
    for e in examples:
        lab = _label(e, label_key)
        if lab is None: continue
        row = [float(e.entry_score if f == 'entry_score' else (e.features.get(f) or 0.0)) for f in feature_names]
        if not all(np.isfinite(row)): continue
        X.append(row); y.append(lab); kept.append(e)
    return np.asarray(X, dtype=float), np.asarray(y, dtype=int), kept


def _model(seed=42):
    from xgboost import XGBClassifier
    return XGBClassifier(
        n_estimators=250, max_depth=3, learning_rate=0.04,
        subsample=0.8, colsample_bytree=0.8,
        reg_alpha=0.5, reg_lambda=2.0,
        objective='binary:logistic', eval_metric='logloss',
        random_state=seed, n_jobs=1,
    )


def _metrics(y, p):
    from sklearn.metrics import roc_auc_score, brier_score_loss, log_loss
    auc = None if len(np.unique(y)) < 2 else float(roc_auc_score(y, p))
    return auc, float(brier_score_loss(y, p)), float(log_loss(y, np.clip(p,1e-6,1-1e-6), labels=[0,1]))


def fit_calibrated_baseline(train_examples, *, label_key='h60_tp2_sl2', feature_names=FEATURES, calibration_fraction=0.2, seed=42):
    """Fit XGBoost on chronological train data; calibrate on the latest train slice only."""
    from sklearn.isotonic import IsotonicRegression
    from sklearn.linear_model import LogisticRegression
    X, y, kept = make_matrix(train_examples, feature_names, label_key)
    if len(y) < 30 or len(np.unique(y)) < 2: raise ValueError('insufficient training examples')
    split = max(1, int(len(y) * (1-calibration_fraction)))
    if split >= len(y): split = len(y)-1
    model = _model(seed); model.fit(X[:split], y[:split])
    raw_cal = model.predict_proba(X[split:])[:,1]
    yc = y[split:]
    if len(np.unique(yc)) >= 2:
        calibrator = LogisticRegression(solver='lbfgs'); calibrator.fit(raw_cal.reshape(-1,1), yc)
    else:
        # Constant calibrator for tiny/degenerate calibration windows.
        calibrator = None
    return model, calibrator, list(feature_names)


def predict_calibrated(model, calibrator, X):
    raw = model.predict_proba(X)[:,1]
    return raw if calibrator is None else calibrator.predict_proba(raw.reshape(-1,1))[:,1]


def purged_ml_walk_forward(examples, *, train_days=28, test_days=7, step_days=7, embargo_min=0, label_key='h60_tp2_sl2', feature_names=FEATURES, min_train=30):
    """Chronological, purged evaluation. Model selection/calibration never sees test examples."""
    from app.research_v10 import purged_walk_forward_splits
    folds = purged_walk_forward_splits(examples, train_days=train_days, test_days=test_days, step_days=step_days, embargo_min=embargo_min)
    results=[]
    for i, fold in enumerate(folds):
        tr, te = fold['train'], fold['test']
        try:
            model, cal, names = fit_calibrated_baseline(tr, label_key=label_key, feature_names=feature_names)
            Xt, yt, _ = make_matrix(te, names, label_key)
            if len(yt)==0: continue
            p = predict_calibrated(model, cal, Xt)
            auc,brier,ll = _metrics(yt,p)
            results.append(asdict(FoldMLResult(i,len(tr),max(0,int(len(tr)*.2)),len(yt),auc,brier,ll,float(yt.mean()),float(p.mean()))))
        except ValueError:
            results.append(asdict(FoldMLResult(i,len(tr),max(0,int(len(tr)*.2)),0,None,None,None,None,None)))
    return results


def train_and_export(examples, output_json: str, *, label_key='h60_tp2_sl2', feature_names=FEATURES):
    model, cal, names = fit_calibrated_baseline(examples, label_key=label_key, feature_names=feature_names)
    Path(output_json).parent.mkdir(parents=True, exist_ok=True)
    meta={'model':'xgboost_baseline_v1.1','label_key':label_key,'features':names,'calibration':'logistic_on_heldout_chronological_slice','n_estimators':250,'max_depth':3}
    Path(output_json).write_text(json.dumps(meta, indent=2), encoding='utf-8')
    return meta
