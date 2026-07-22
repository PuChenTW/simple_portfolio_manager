import pandas as pd

from portfolio_manager.market import calculate_indicators


def test_indicators_are_null_until_enough_data_exists() -> None:
    indicators = calculate_indicators(pd.Series(range(10), dtype=float))
    assert all(value is None for value in indicators.values())


def test_indicators_are_calculated_for_a_long_series() -> None:
    indicators = calculate_indicators(pd.Series(range(1, 101), dtype=float))
    assert indicators["sma20"] is not None
    assert indicators["sma50"] is not None
    assert indicators["macd"] is not None
    assert indicators["macd_signal"] is not None
    assert indicators["macd_histogram"] is not None
