from collections.abc import Generator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from portfolio_manager.api import app, get_market_provider
from portfolio_manager.db import Base, create_sqlite_engine, get_session
from portfolio_manager.journal import PortfolioKind
from portfolio_manager.market import (
    HistoryAdjustment,
    HistoryBar,
    HistoryInterval,
    HistoryResult,
    InstrumentSnapshot,
    MarketDataError,
    MarketSnapshot,
    QuoteSnapshot,
)
from portfolio_manager.models import Portfolio


def pytest_collection_modifyitems(config, items) -> None:
    if config.option.markexpr == "external":
        return
    skip = pytest.mark.skip(reason="use -m external to call Yahoo Finance")
    for item in items:
        if "external" in item.keywords:
            item.add_marker(skip)


class FakeMarketProvider:
    def __init__(self) -> None:
        self.fail = False
        self.history_limit: int | None = None
        self.calls: list[str] = []
        self.history_calls: list[str] = []
        self.prices = {
            "AAPL": Decimal("140"),
            "MSFT": Decimal("500"),
            "2330.TW": Decimal("1100"),
            "8069.TWO": Decimal("190"),
            "BTC-USD": Decimal("100000"),
            # Plan 4.6 cases: instruments Yahoo's coarse metadata misreports.
            "VOO": Decimal("520"),
            "VT": Decimal("120"),
            "SOXX": Decimal("240"),
            "GLD": Decimal("250"),
            "BOXX": Decimal("110"),
            "USDT-USD": Decimal("1"),
            "TSM": Decimal("180"),
        }
        # Yahoo reports these through `quoteType`; it distinguishes funds from equities but says
        # nothing about what a fund actually holds.
        self.quote_types = {
            "VOO": "ETF",
            "VT": "ETF",
            "SOXX": "ETF",
            "GLD": "ETF",
            "BOXX": "ETF",
            "USDT-USD": "CRYPTOCURRENCY",
            "BTC-USD": "CRYPTOCURRENCY",
        }

    def fetch(self, ticker: str) -> MarketSnapshot:
        self.calls.append(ticker)
        if self.fail or ticker not in self.prices:
            raise MarketDataError(f"No fixture for {ticker}")
        currency = "TWD" if ticker.endswith((".TW", ".TWO")) else "USD"
        quote_type = self.quote_types.get(ticker, "EQUITY")
        if ticker.endswith(".TW"):
            market = "TW"
        elif ticker.endswith(".TWO"):
            market = "TWO"
        elif quote_type == "CRYPTOCURRENCY":
            market = "CRYPTO"
        else:
            market = "US"
        asset_type = "crypto" if market == "CRYPTO" else "stock"
        now = datetime.now(UTC) - timedelta(minutes=1)
        price = self.prices[ticker]
        return MarketSnapshot(
            instrument=InstrumentSnapshot(
                ticker=ticker,
                name=f"{ticker} name",
                asset_type=asset_type,
                market=market,
                exchange=market,
                currency=currency,
                quote_type=quote_type,
            ),
            quote=QuoteSnapshot(
                price=price,
                open=price - 2,
                high=price + 3,
                low=price - 4,
                previous_close=price - 1,
                volume=Decimal("123456"),
                change=Decimal("1"),
                change_percent=Decimal("0.72"),
                market_cap=Decimal("3000000000"),
                year_high=price + 20,
                year_low=price - 20,
                sma20=price - 5,
                sma50=price - 10,
                rsi14=Decimal("55"),
                macd=Decimal("1.5"),
                macd_signal=Decimal("1.2"),
                macd_histogram=Decimal("0.3"),
                provider_as_of=now,
                indicators_as_of=now,
            ),
        )

    def history(
        self,
        ticker: str,
        days: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        interval: HistoryInterval = HistoryInterval.DAILY,
        adjustment: HistoryAdjustment = HistoryAdjustment.AUTO,
    ) -> HistoryResult:
        # Recorded separately from `calls`, which only tracks `fetch`. A cache in front of this
        # provider is only proven by the requests that never arrive.
        self.history_calls.append(ticker)
        if self.fail or ticker not in self.prices:
            raise MarketDataError(f"No fixture for {ticker}")
        price = self.prices[ticker]
        last_date = end_date or date(2026, 7, 24)
        count = min(days or 320, self.history_limit or 320)
        bars = [
            HistoryBar(
                timestamp=datetime.combine(
                    last_date - timedelta(days=index), datetime.min.time(), UTC
                ),
                open=price - 1,
                high=price + 1,
                low=price - 2,
                close=price - Decimal(index) / Decimal("10"),
                volume=Decimal("100") + index,
            )
            for index in reversed(range(count))
        ]
        if start_date is not None:
            bars = [bar for bar in bars if bar.timestamp.date() >= start_date]
        return HistoryResult(
            ticker=ticker,
            provider="Fake Market",
            interval=interval,
            adjustment=adjustment,
            requested_start_date=start_date,
            requested_end_date=end_date,
            fetched_at=datetime.now(UTC),
            warnings=[],
            bars=bars,
        )


@dataclass
class Harness:
    client: TestClient
    provider: FakeMarketProvider
    session_factory: sessionmaker[Session]

    def portfolio(self, name: str = "USD portfolio", currency: str = "USD") -> str:
        response = self.client.post(
            "/api/v1/portfolios", json={"name": name, "base_currency": currency}
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def cash_account(
        self,
        name: str = "Bank account",
        currency: str = "USD",
        institution: str | None = "Test Bank",
    ) -> str:
        """A portfolio marked as a cash account, for tests of the cash-only guardrails."""
        portfolio_id = self.portfolio(name, currency)
        with self.session_factory() as session:
            portfolio = session.get(Portfolio, portfolio_id)
            portfolio.kind = PortfolioKind.CASH.value
            portfolio.institution = institution
            session.commit()
        return portfolio_id

    def liability_account(
        self,
        name: str = "Credit loan",
        currency: str = "USD",
        institution: str | None = "Test Bank",
    ) -> str:
        """A book whose balance is money owed, so it may run negative."""
        response = self.client.post(
            "/api/v1/liability-accounts",
            json={"name": name, "base_currency": currency, "institution": institution},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]


@pytest.fixture
def market_provider() -> FakeMarketProvider:
    """The fake provider on its own, for tests that need no database or HTTP client."""
    return FakeMarketProvider()


@pytest.fixture
def harness(tmp_path) -> Generator[Harness]:
    engine = create_sqlite_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    provider = FakeMarketProvider()

    def session_override() -> Generator[Session]:
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_market_provider] = lambda: provider
    with TestClient(app) as client:
        yield Harness(client=client, provider=provider, session_factory=factory)
    app.dependency_overrides.clear()
    # No drop_all: each test gets its own tmp_path database, and journal_events references itself
    # (reverses_event_id), which SQLite cannot resolve into a safe drop order.
    engine.dispose()
