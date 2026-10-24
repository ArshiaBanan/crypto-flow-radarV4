from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from app.research_v10 import ResearchExample

NUMERIC_FEATURES = (
    "taker_imbalance",
    "volume_z_median",
    "oi_change_pct",
    "funding_rate",
    "orderbook_imbalance",
    "return_1m_pct",
    "return_5m_pct",
    "return_15m_pct",
    "rvol_1m_vs_history",
    "buy_share",
    "short_liquidation_usd",
    "long_liquidation_usd",
    "entry_score",
    # advanced/regime/anomaly context
    "ret_5m",
    "ret_15m",
    "ret_1h",
    "realized_vol_20",
    "atr20",
    "range_pct",
    "volume_ratio_20",
    "anomaly_score",
    "organic_score",
    "caution_score",
    "btc_return_15m",
    "btc_return_1h",
    "btc_volatility_1h",
    "regime_confidence",
    "breadth",
)

REGIMES = (
    "risk_on_trend",
    "risk_off_trend",
    "mild_risk_on",
    "mild_risk_off",
    "range",
    "high_volatility",
    "unknown",
)

FEATURES = NUMERIC_FEATURES + tuple(f"regime__{r}" for r in REGIMES)


@dataclass(slots=True)
class ModelSpec:
    name: str
    seed: int
    model_type: str


MODEL_SPECS = (
    ModelSpec("xgb_a", 17, "xgb"),
    ModelSpec("xgb_b", 42, "xgb"),
    ModelSpec("hist_gb", 73, "hist_gb"),
)


@dataclass(slots=True)
class CalibratedMember:
    name: str
    weight: float
    model: object
    calibrator: object | None


@dataclass(slots=True)
class RegimeEnsemble:
    regime: str
    members: list[CalibratedMember]
    n_train: int


@dataclass(slots=True)
class EnsembleBundle:
    label_key: str
    feature_names: list[str]
    global_members: list[CalibratedMember]
    regime_members: dict[str, RegimeEnsemble]
    min_regime_train: int
    calibration_fraction: float


@dataclass(slots=True)
class EnsembleMetrics:
    brier: float | None
    logloss: float | None
    auc: float | None
    positive_rate: float | None
    mean_pred: float | None


def _safe_num(value) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def regime_of(example: ResearchExample) -> str:
    value = example.features.get("regime_label", "unknown")
    return value if value in REGIMES[:-1] else "unknown"


def _label(example: ResearchExample, key: str) -> int | None:
    data = example.labels.get(key)
    if not isinstance(data, dict):
        return None
    status = data.get("status")
    if status == "TP":
        return 1
    if status == "SL":
        return 0
    signed = data.get("signed_future_return_pct")
    if signed is None:
        return None
    try:
        return int(float(signed) > 0.0)
    except (TypeError, ValueError):
        return None


def make_matrix(examples: Sequence[ResearchExample], feature_names: Sequence[str] = FEATURES, label_key: str = "h60_tp2_sl2"):
    X: list[list[float]] = []
    y: list[int] = []
    kept: list[ResearchExample] = []
    for example in examples:
        lab = _label(example, label_key)
        if lab is None:
            continue
        row: list[float] = []
        regime = regime_of(example)
        for feature in feature_names:
            if feature == "entry_score":
                row.append(_safe_num(example.entry_score))
            elif feature.startswith("regime__"):
                row.append(float(regime == feature.split("__", 1)[1]))
            else:
                row.append(_safe_num(example.features.get(feature)))
        if not np.all(np.isfinite(row)):
            continue
        X.append(row)
        y.append(lab)
        kept.append(example)
    return np.asarray(X, dtype=float), np.asarray(y, dtype=int), kept


def _xgb(seed: int, y: np.ndarray):
    from xgboost import XGBClassifier
    pos = max(1, int(np.sum(y == 1)))
    neg = max(1, int(np.sum(y == 0)))
    return XGBClassifier(
        n_estimators=325,
        max_depth=3,
        learning_rate=0.035,
        min_child_weight=3,
        subsample=0.82,
        colsample_bytree=0.82,
        reg_alpha=0.8,
        reg_lambda=3.0,
        gamma=0.05,
        objective="binary:logistic",
        eval_metric="logloss",
        scale_pos_weight=neg / pos,
        random_state=seed,
        n_jobs=1,
    )


def _hist_gb(seed: int, y: np.ndarray):
    from sklearn.ensemble import HistGradientBoostingClassifier
    return HistGradientBoostingClassifier(
        learning_rate=0.05,
        max_iter=250,
        max_leaf_nodes=15,
        l2_regularization=2.0,
        random_state=seed,
    )


def _make_model(spec: ModelSpec, y: np.ndarray):
    if spec.model_type == "xgb":
        return _xgb(spec.seed, y)
    if spec.model_type == "hist_gb":
        return _hist_gb(spec.seed, y)
    raise ValueError(f"unknown model_type={spec.model_type}")


def _sample_weights(y: np.ndarray) -> np.ndarray:
    pos = max(1, int(np.sum(y == 1)))
    neg = max(1, int(np.sum(y == 0)))
    total = pos + neg
    w_pos = total / (2.0 * pos)
    w_neg = total / (2.0 * neg)
    return np.where(y == 1, w_pos, w_neg).astype(float)


def _fit_calibrator(raw: np.ndarray, y: np.ndarray):
    from sklearn.linear_model import LogisticRegression
    if len(y) < 12 or len(np.unique(y)) < 2:
        return None
    model = LogisticRegression(C=1.0, solver="lbfgs")
    model.fit(raw.reshape(-1, 1), y)
    return model


def _calibrated_predict(member: CalibratedMember, X: np.ndarray) -> np.ndarray:
    raw = member.model.predict_proba(X)[:, 1]
    if member.calibrator is None:
        return raw
    return member.calibrator.predict_proba(raw.reshape(-1, 1))[:, 1]


def _member_weight(y_cal: np.ndarray, p_cal: np.ndarray) -> float:
    # Lower Brier is better; cap the influence of tiny calibration samples.
    if len(y_cal) < 12:
        return 1.0
    brier = float(np.mean((p_cal - y_cal) ** 2))
    return 1.0 / max(0.05, brier)


def _fit_members(examples: Sequence[ResearchExample], *, label_key: str, feature_names: Sequence[str], calibration_fraction: float) -> list[CalibratedMember]:
    X, y, _ = make_matrix(examples, feature_names, label_key)
    if len(y) < 30 or len(np.unique(y)) < 2:
        raise ValueError("insufficient training examples")
    split = int(len(y) * (1.0 - calibration_fraction))
    split = max(1, min(len(y) - 1, split))
    X_fit, y_fit = X[:split], y[:split]
    X_cal, y_cal = X[split:], y[split:]
    if len(np.unique(y_fit)) < 2:
        raise ValueError("training slice has only one class")

    members: list[CalibratedMember] = []
    raw_calibrations: list[tuple[np.ndarray, CalibratedMember]] = []
    weights: list[float] = []
    sample_weights = _sample_weights(y_fit)

    for spec in MODEL_SPECS:
        model = _make_model(spec, y_fit)
        # XGBoost gets imbalance handling inside the model; HistGradientBoosting gets explicit sample weights.
        if spec.model_type == "hist_gb":
            model.fit(X_fit, y_fit, sample_weight=sample_weights)
        else:
            model.fit(X_fit, y_fit)
        raw = model.predict_proba(X_cal)[:, 1]
        calibrator = _fit_calibrator(raw, y_cal)
        member = CalibratedMember(spec.name, 1.0, model, calibrator)
        p = raw if calibrator is None else calibrator.predict_proba(raw.reshape(-1, 1))[:, 1]
        weight = _member_weight(y_cal, p)
        members.append(member)
        weights.append(weight)

    total = sum(weights) or 1.0
    for member, weight in zip(members, weights):
        member.weight = weight / total
    return members


def fit_regime_aware_ensemble(
    train_examples: Sequence[ResearchExample],
    *,
    label_key: str = "h60_tp2_sl2",
    feature_names: Sequence[str] = FEATURES,
    calibration_fraction: float = 0.2,
    min_regime_train: int = 80,
) -> EnsembleBundle:
    ordered = sorted(train_examples, key=lambda x: (x.ts_ms, x.symbol))
    global_members = _fit_members(
        ordered,
        label_key=label_key,
        feature_names=feature_names,
        calibration_fraction=calibration_fraction,
    )

    by_regime: dict[str, list[ResearchExample]] = {}
    for example in ordered:
        regime = regime_of(example)
        if regime == "unknown":
            continue
        by_regime.setdefault(regime, []).append(example)

    regime_members: dict[str, RegimeEnsemble] = {}
    for regime, items in by_regime.items():
        if len(items) < min_regime_train:
            continue
        try:
            members = _fit_members(
                items,
                label_key=label_key,
                feature_names=feature_names,
                calibration_fraction=calibration_fraction,
            )
        except ValueError:
            continue
        regime_members[regime] = RegimeEnsemble(regime, members, len(items))

    return EnsembleBundle(
        label_key=label_key,
        feature_names=list(feature_names),
        global_members=global_members,
        regime_members=regime_members,
        min_regime_train=min_regime_train,
        calibration_fraction=calibration_fraction,
    )


def _predict_members(members: Sequence[CalibratedMember], X: np.ndarray) -> np.ndarray:
    if not members:
        raise ValueError("ensemble has no members")
    preds = np.vstack([_calibrated_predict(m, X) for m in members])
    weights = np.asarray([m.weight for m in members], dtype=float)
    weights = weights / (weights.sum() or 1.0)
    return np.clip(np.average(preds, axis=0, weights=weights), 0.0, 1.0)


def predict_regime_aware(bundle: EnsembleBundle, examples: Sequence[ResearchExample]) -> np.ndarray:
    out = np.full(len(examples), np.nan, dtype=float)
    groups: dict[str, list[int]] = {}
    for i, example in enumerate(examples):
        groups.setdefault(regime_of(example), []).append(i)
    for regime, idxs in groups.items():
        local = [examples[i] for i in idxs]
        X, _, kept = make_matrix(local, bundle.feature_names, bundle.label_key)
        if len(kept) != len(local):
            # Prediction-time matrix should remain row-aligned; use a second pass on valid rows.
            valid_positions = []
            for j, example in enumerate(local):
                xx, _, kk = make_matrix([example], bundle.feature_names, bundle.label_key)
                if len(kk) == 1:
                    valid_positions.append((j, xx[0]))
            if not valid_positions:
                continue
            X = np.asarray([row for _, row in valid_positions], dtype=float)
            idxs_valid = [idxs[j] for j, _ in valid_positions]
        else:
            idxs_valid = idxs

        members = bundle.regime_members.get(regime).members if regime in bundle.regime_members else bundle.global_members
        p = _predict_members(members, X)
        out[np.asarray(idxs_valid, dtype=int)] = p
    return out



def feature_vector(features: dict, entry_score: float = 0.0, feature_names: Sequence[str] = FEATURES) -> np.ndarray:
    """Build one inference vector without requiring a future label."""
    regime = features.get("regime_label", "unknown")
    if regime not in REGIMES[:-1]:
        regime = "unknown"
    row=[]
    for feature in feature_names:
        if feature == "entry_score":
            row.append(_safe_num(entry_score))
        elif feature.startswith("regime__"):
            row.append(float(regime == feature.split("__",1)[1]))
        else:
            row.append(_safe_num(features.get(feature)))
    return np.asarray(row, dtype=float).reshape(1, -1)


def predict_feature_dict(bundle: EnsembleBundle, features: dict, *, entry_score: float = 0.0) -> float:
    """Predict a calibrated probability for a live point-in-time feature snapshot."""
    X = feature_vector(features, entry_score=entry_score, feature_names=bundle.feature_names)
    regime = features.get("regime_label", "unknown")
    if regime in bundle.regime_members:
        members = bundle.regime_members[regime].members
    else:
        members = bundle.global_members
    return float(_predict_members(members, X)[0])


def save_bundle(bundle: EnsembleBundle, output_path: str) -> str:
    """Persist the fitted ensemble; caller is responsible for artifact versioning."""
    import joblib
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, output_path)
    return output_path


def load_bundle(path: str) -> EnsembleBundle:
    import joblib
    bundle = joblib.load(path)
    if not isinstance(bundle, EnsembleBundle):
        raise TypeError("artifact is not an EnsembleBundle")
    return bundle

def evaluate(y: Sequence[int], p: Sequence[float]) -> EnsembleMetrics:
    from sklearn.metrics import brier_score_loss, log_loss, roc_auc_score
    y_arr = np.asarray(y, dtype=int)
    p_arr = np.clip(np.asarray(p, dtype=float), 1e-6, 1 - 1e-6)
    auc = None if len(np.unique(y_arr)) < 2 else float(roc_auc_score(y_arr, p_arr))
    return EnsembleMetrics(
        brier=float(brier_score_loss(y_arr, p_arr)),
        logloss=float(log_loss(y_arr, p_arr, labels=[0, 1])),
        auc=auc,
        positive_rate=float(y_arr.mean()) if len(y_arr) else None,
        mean_pred=float(p_arr.mean()) if len(p_arr) else None,
    )


def walk_forward_regime_ensemble(
    examples: Sequence[ResearchExample],
    *,
    train_days: int = 28,
    test_days: int = 7,
    step_days: int = 7,
    label_key: str = "h60_tp2_sl2",
    feature_names: Sequence[str] = FEATURES,
    min_train: int = 60,
    min_regime_train: int = 80,
):
    from app.research_v10 import purged_walk_forward_splits

    folds = purged_walk_forward_splits(
        examples,
        train_days=train_days,
        test_days=test_days,
        step_days=step_days,
        embargo_min=60,
    )
    results: list[dict] = []
    for fold_id, fold in enumerate(folds):
        train = fold["train"]
        test = fold["test"]
        if len(train) < min_train:
            continue
        try:
            bundle = fit_regime_aware_ensemble(
                train,
                label_key=label_key,
                feature_names=feature_names,
                min_regime_train=min_regime_train,
            )
        except ValueError:
            continue

        X_test, y_test, kept = make_matrix(test, feature_names, label_key)
        if len(y_test) == 0:
            continue
        p_test = _predict_members(bundle.global_members, X_test)

        # Apply regime-specialized members where the held-out example carries a known regime.
        known_idx = [i for i, e in enumerate(kept) if regime_of(e) in bundle.regime_members]
        if known_idx:
            for regime, reg_bundle in bundle.regime_members.items():
                r_idx = [i for i, e in enumerate(kept) if regime_of(e) == regime]
                if r_idx:
                    p_test[np.asarray(r_idx)] = _predict_members(reg_bundle.members, X_test[np.asarray(r_idx)])

        metrics = evaluate(y_test, p_test)
        results.append({
            "fold": fold_id,
            "train": len(train),
            "test": len(y_test),
            "regime_models": sorted(bundle.regime_members),
            **asdict(metrics),
        })
    return results


def export_bundle_metadata(bundle: EnsembleBundle, output_json: str) -> dict:
    Path(output_json).parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "model": "regime_aware_ensemble_v1.5",
        "label_key": bundle.label_key,
        "features": bundle.feature_names,
        "members": [
            {"name": m.name, "weight": round(m.weight, 6), "calibrated": m.calibrator is not None}
            for m in bundle.global_members
        ],
        "regime_models": {
            k: {
                "n_train": v.n_train,
                "members": [
                    {"name": m.name, "weight": round(m.weight, 6), "calibrated": m.calibrator is not None}
                    for m in v.members
                ],
            }
            for k, v in bundle.regime_members.items()
        },
        "min_regime_train": bundle.min_regime_train,
        "calibration_fraction": bundle.calibration_fraction,
    }
    Path(output_json).write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return meta
