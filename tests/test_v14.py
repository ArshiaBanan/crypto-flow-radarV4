from app.anomaly import detect_anomaly, anomaly_features


def base():
    return {
        "taker_imbalance": 0.65,
        "volume_z_median": 1.0,
        "rvol_1m_vs_history": 1.5,
        "return_1m_pct": 0.3,
        "return_5m_pct": 0.8,
        "oi_change_pct": 2.0,
        "funding_rate": 0.0001,
        "orderbook_imbalance": 0.25,
        "long_liquidation_usd": 0.0,
        "short_liquidation_usd": 0.0,
        "bar_volume_quote": 5000000.0,
        "trades": 10000,
    }


def test_normal_state_is_not_high_anomaly():
    r = detect_anomaly(base())
    assert 0 <= r.score <= 100
    assert r.classification in {"NORMAL", "RELATIVELY_ORGANIC", "CAUTION"}


def test_price_volume_dislocation_is_flagged():
    f = base(); f.update({"return_5m_pct": 3.0, "volume_z_median": 0.2, "rvol_1m_vs_history": 0.8, "taker_imbalance": -0.2})
    r = detect_anomaly(f)
    assert "PRICE_VOLUME_DISLOCATION" in r.flags
    assert r.score > 0


def test_price_oi_divergence_is_flagged():
    f = base(); f.update({"return_5m_pct": 2.0, "oi_change_pct": -6.0})
    r = detect_anomaly(f)
    assert "PRICE_OI_DIVERGENCE" in r.flags


def test_extreme_funding_and_liquidations_raise_caution():
    f = base(); f.update({"funding_rate": 0.0008, "long_liquidation_usd": 2_000_000, "short_liquidation_usd": 50_000})
    r = detect_anomaly(f)
    assert "EXTREME_FUNDING" in r.flags
    assert "LIQUIDATION_CASCADE" in r.flags
    assert r.caution_score > 0


def test_liquidity_collapse_flags():
    f = base(); f.update({"depth_change_pct": -50.0, "spread_change_pct": 90.0, "spread_bps": 20.0})
    r = detect_anomaly(f)
    assert "DEPTH_COLLAPSE" in r.flags
    assert "SPREAD_WIDENING" in r.flags
    assert "WIDE_SPREAD" in r.flags


def test_anomaly_features_are_serializable():
    out = anomaly_features(base())
    assert isinstance(out["anomaly_flags"], list)
    assert 0 <= out["anomaly_score"] <= 100
    assert set(["anomaly_score", "anomaly_classification", "anomaly_flags", "organic_score", "caution_score", "anomaly_diagnostics"]) <= set(out)

def test_sqlite_feature_schema_migrates(tmp_path):
    from app.storage.sqlite import SQLiteStore
    db = tmp_path / 'radar.db'
    store = SQLiteStore(str(db))
    cols = {r[1] for r in store.conn.execute('PRAGMA table_info(feature_snapshots)').fetchall()}
    store.close()
    assert {'anomaly_score', 'anomaly_classification', 'anomaly_flags'} <= cols
