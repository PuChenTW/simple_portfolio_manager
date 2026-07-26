from datetime import UTC, datetime, timedelta

import pytest

from portfolio_manager.market import YahooMarketProvider
from portfolio_manager.services import MarketService


@pytest.mark.external
@pytest.mark.parametrize("ticker", ["AAPL", "2330.TW", "BTC-USD"])
def test_live_yahoo_market_smoke(ticker: str) -> None:
    provider = YahooMarketProvider()
    snapshot = provider.fetch(ticker)
    assert snapshot.instrument.ticker == ticker
    assert snapshot.quote.price > 0

    cutoff = datetime.now(UTC).date() - timedelta(days=3)
    history = provider.history(
        ticker,
        start_date=cutoff - timedelta(days=400),
        end_date=cutoff,
    )
    assert history.bars
    assert history.bars[-1].timestamp.date() <= cutoff

    market = MarketService(None, provider, 300)  # type: ignore[arg-type]
    technical = market.technical_snapshot(ticker, cutoff, None, None, 2)
    assert technical.actual_end_date <= cutoff
    assert technical.bar_count > 100
    assert technical.trend.sma20 is not None
    assert technical.momentum.rsi14 is not None
