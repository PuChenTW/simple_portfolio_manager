from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from portfolio_manager.market import HistoryBar
from portfolio_manager.technical import (
    bars_frame,
    calculate_technical,
    event_metrics,
    relative_returns,
)


def frame(closes: list[float], start: str = "2025-01-01") -> pd.DataFrame:
    index = pd.bdate_range(start, periods=len(closes), tz="UTC")
    return pd.DataFrame(
        {
            "open": [value - 1 for value in closes],
            "high": [value + 2 for value in closes],
            "low": [value - 2 for value in closes],
            "close": closes,
            "volume": list(range(1, len(closes) + 1)),
        },
        index=index,
    )


def test_trend_returns_atr_volatility_drawdown_and_volume() -> None:
    analysis = calculate_technical(frame([float(value) for value in range(1, 301)]))

    assert analysis.trend["sma20"] == Decimal("290.5")
    assert analysis.trend["sma50"] == Decimal("275.5")
    assert analysis.trend["sma200"] == Decimal("200.5")
    assert analysis.momentum["return_20d_percent"] == pytest.approx(
        Decimal("7.14285714285714")
    )
    assert analysis.volatility["atr14"] == Decimal("4.0")
    assert analysis.volatility["realized_volatility_20d_percent"] > 0
    assert analysis.volatility["drawdown_from_252d_high_percent"] == Decimal("0.0")
    assert analysis.volume["latest_vs_20d_average"] == pytest.approx(
        Decimal("1.0327022375215147")
    )
    assert analysis.volume["latest_252d_percentile"] == Decimal("100.0")


@pytest.mark.parametrize(
    ("closes", "expected"),
    [
        ([float(value) for value in range(1, 31)], Decimal("100")),
        ([float(value) for value in range(30, 0, -1)], Decimal("0")),
        ([10.0] * 30, Decimal("50")),
    ],
)
def test_rsi_handles_one_sided_and_flat_prices(closes, expected) -> None:
    assert calculate_technical(frame(closes)).momentum["rsi14"] == expected


def test_insufficient_and_missing_data_produce_nulls_and_warnings() -> None:
    short = calculate_technical(frame([1.0] * 10))
    assert short.trend["sma20"] is None
    assert short.momentum["return_20d_percent"] is None
    assert short.warnings

    missing = calculate_technical(pd.DataFrame({"close": [1.0, 2.0, 3.0]}))
    assert missing.volatility["atr14"] is None
    assert missing.volume["latest_vs_20d_average"] is None
    assert any("Missing OHLCV" in warning for warning in missing.warnings)


def test_as_of_filter_excludes_later_bars() -> None:
    bars = [
        HistoryBar(
            timestamp=datetime(2026, 7, day, tzinfo=UTC),
            open=Decimal(day),
            high=Decimal(day + 1),
            low=Decimal(day - 1),
            close=Decimal(day),
            volume=Decimal("100"),
        )
        for day in range(20, 25)
    ]
    filtered = bars_frame(bars, date(2026, 7, 22))
    assert list(filtered.index.date) == [
        date(2026, 7, 20),
        date(2026, 7, 21),
        date(2026, 7, 22),
    ]


def test_non_trading_event_anchor_and_anchored_vwap() -> None:
    data = frame([10.0, 11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0], "2026-07-20")
    metrics, warnings = event_metrics(data, date(2026, 7, 25))

    assert warnings == []
    assert metrics["effective_anchor_date"] == date(2026, 7, 27)
    assert metrics["close"] == Decimal("15.0")
    assert metrics["gap_percent"] == Decimal("0.0")
    assert metrics["anchored_vwap"] == pytest.approx(Decimal("16.095238095238095"))


def test_relative_returns_align_common_dates() -> None:
    instrument = frame([float(value) for value in range(100, 141)])
    benchmark = frame([float(value) for value in range(200, 241)]).iloc[::2]
    values, warnings = relative_returns(instrument, benchmark)

    assert values["return_20d_percent"] is not None
    assert values["return_60d_percent"] is None
    assert any("60-day" in warning for warning in warnings)
