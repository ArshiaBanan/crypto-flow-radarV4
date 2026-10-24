from app.collectors.livecoinwatch import LiveCoinWatchClient


def test_lcw_rank_movers_uses_multiplicative_delta():
    rows = [
        {"code": "A", "volume": 300, "delta": {"day": 1.25}},
        {"code": "B", "volume": 450, "delta": {"day": 0.80}},
        {"code": "C", "volume": 600, "delta": {"day": 1.10}},
    ]
    gainers, losers = LiveCoinWatchClient.rank_movers(rows, "day")
    assert gainers[0]["code"] == "A"
    assert round(gainers[0]["change_pct"], 2) == 25.0
    assert losers[0]["code"] == "B"
    assert round(losers[0]["change_pct"], 2) == -20.0
