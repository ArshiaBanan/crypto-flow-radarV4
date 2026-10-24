import asyncio
import pytest
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app, alert_ledger, production_runtime, settings
from app.production_v20 import AlertLedger, CorrelationExposure, ProductionRuntimeV20, TelegramNotifier


def test_v20_ledger_roundtrip(tmp_path):
    ledger = AlertLedger(str(tmp_path / 'a.db'))
    from app.production_v20 import AlertEvent
    e = AlertEvent('abc',1000,'btcusdt','long',100,'ALERT','A',90,88,90,0.72,0.2,2.4,3,1.5,4,'normal',10,95,80,('x',),('w',),(),('r',))
    assert ledger.insert(e) is True
    assert ledger.insert(e) is False
    assert ledger.get('abc')['symbol'] == 'btcusdt'
    ledger.mark_delivery('abc','SENT',paper_signal_id='ps1')
    assert ledger.get('abc')['delivery_status'] == 'SENT'
    ledger.close()


def test_v20_pause_state(tmp_path):
    ledger = AlertLedger(str(tmp_path / 'a.db'))
    rt = ProductionRuntimeV20(ledger)
    rt.set_paused(True, 'maintenance')
    assert rt.status()['paused'] is True
    rt.set_paused(False, 'resume')
    assert rt.status()['paused'] is False
    ledger.close()


def test_v20_correlation_exposure(tmp_path):
    from math import sin
    def provider(symbol, n):
        base = [100 + i for i in range(50)] if symbol == 'a' else [200 + 2*i for i in range(50)]
        return [{'close': x} for x in base]
    c = CorrelationExposure(provider, 1.0, 0.75)
    exposure, details = c.exposure('a', [{'symbol':'b'}])
    assert exposure == 1.0
    assert details and abs(details[0]['corr']) >= 0.75


def test_v20_api_status_and_alerts():
    settings.data_engine_enabled = False
    settings.binance_enabled = False
    with TestClient(app) as client:
        h = client.get('/health')
        assert h.status_code == 200
        assert h.json()['version'] == '3.7.0'
        s = client.get('/v20/status')
        assert s.status_code == 200
        a = client.get('/v20/alerts?limit=5')
        assert a.status_code == 200
        assert isinstance(a.json()['rows'], list)

@pytest.mark.asyncio
async def test_v20_live_pipeline_creates_alert(tmp_path):
    ledger = AlertLedger(str(tmp_path / 'pipeline.db'))
    sent = []
    class DummyTelegram:
        async def send(self, event):
            sent.append(event.alert_id)
            return True, None
    rt = ProductionRuntimeV20(ledger, telegram=DummyTelegram(), auto_paper_submit=False)
    features = {
        'symbol':'AAAUSDT','ts_ms':2_000,'close':100.0,
        'taker_imbalance':0.6,'volume_z_median':3.5,'return_5m_pct':1.5,
        'return_15m_pct':2.0,'oi_change_pct':3.0,'funding_rate':0.0,
        'orderbook_imbalance':0.5,'anomaly_score':5.0,'organic_score':80.0,
        'liquidity_regime':'normal','regime_label':'risk_on_trend','regime_confidence':0.8,
        'bar_volume_quote':2_000_000,'depth_usd':1_000_000,'spread_bps':2.0,
    }
    result = await rt.consider(features, 92, 35)
    assert result is not None
    event, risk = result
    assert event.action == 'ALERT'
    assert risk.action == 'ALERT'
    assert sent == [event.alert_id]
    ledger.close()
