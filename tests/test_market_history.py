from datetime import date

import pandas as pd

from portfolio_manager.market import (
    HistoryAdjustment,
    HistoryInterval,
    YahooMarketProvider,
)


def test_yahoo_adapter_converts_inclusive_end_to_exclusive(monkeypatch) -> None:
    captured = {}
    index = pd.date_range("2026-07-23", periods=3, tz="America/New_York")
    data = pd.DataFrame(
        {
            "Open": [10, 11, 12],
            "High": [12, 13, 14],
            "Low": [9, 10, 11],
            "Close": [11, 12, 13],
            "Volume": [100, 200, 300],
        },
        index=index,
    )

    class FakeTicker:
        def history(self, **kwargs):
            captured.update(kwargs)
            return data

    monkeypatch.setattr("portfolio_manager.market.yf.Ticker", lambda _symbol: FakeTicker())
    result = YahooMarketProvider().history(
        "aapl",
        start_date=date(2026, 7, 23),
        end_date=date(2026, 7, 24),
        interval=HistoryInterval.DAILY,
        adjustment=HistoryAdjustment.UNADJUSTED,
    )

    assert captured["end"] == date(2026, 7, 25)
    assert captured["auto_adjust"] is False
    assert [bar.timestamp.date() for bar in result.bars] == [
        date(2026, 7, 23),
        date(2026, 7, 24),
    ]
