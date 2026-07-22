import pytest

from portfolio_manager.market import YahooMarketProvider


@pytest.mark.external
@pytest.mark.parametrize("ticker", ["AAPL", "2330.TW", "BTC-USD"])
def test_live_yahoo_market_smoke(ticker: str) -> None:
    snapshot = YahooMarketProvider().fetch(ticker)
    assert snapshot.instrument.ticker == ticker
    assert snapshot.quote.price > 0
