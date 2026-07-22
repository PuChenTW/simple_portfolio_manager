from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

PositiveDecimal = Annotated[
    Decimal,
    Field(
        gt=0,
        description="A value greater than zero. Decimal strings are recommended for exact input.",
        examples=["10.5"],
    ),
]
RequestId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
    Field(
        description=(
            "Client-generated idempotency key for one logical mutation. Reuse it only when "
            "retrying the exact same request."
        ),
        examples=["trade-20260722-001"],
    ),
]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TradeSide(StrEnum):
    """Whether a spot trade adds to or reduces a position."""

    BUY = "buy"
    SELL = "sell"


class CashAction(StrEnum):
    """Whether an independent cash ledger event adds or removes cash."""

    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"


class TagMode(StrEnum):
    """How multiple tag filters are combined."""

    ANY = "any"
    ALL = "all"


class PortfolioCreate(ApiModel):
    """Create an isolated, single-currency portfolio."""

    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
        Field(description="Unique human-readable portfolio name.", examples=["US long term"]),
    ]
    base_currency: str = Field(
        description=(
            "Three-letter ISO currency. Every instrument added later must quote in this currency."
        ),
        examples=["USD"],
    )

    model_config = ConfigDict(
        json_schema_extra={"examples": [{"name": "US long term", "base_currency": "USD"}]}
    )

    @field_validator("base_currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha() or not value.isascii():
            raise ValueError("base_currency must be a three-letter ISO currency code")
        return value


class PortfolioRead(ApiModel):
    """Persistent portfolio identity and its currency invariant."""

    id: str
    name: str
    base_currency: str
    created_at: datetime


class TradeCreate(ApiModel):
    """Record an executed spot trade; this API does not place orders."""

    request_id: RequestId
    ticker: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=32),
        Field(
            description=(
                "Yahoo-compatible ticker: AAPL, 2330.TW, 8069.TWO, or BTC-USD. "
                "It is normalized to uppercase."
            ),
            examples=["AAPL"],
        ),
    ]
    side: TradeSide = Field(
        description="Use buy to add quantity or sell to reduce an existing position."
    )
    quantity: PositiveDecimal = Field(
        description="Executed asset quantity; fractional values work."
    )
    unit_price: PositiveDecimal = Field(
        description="Actual execution price per unit in the instrument quote currency."
    )
    fee: Annotated[
        Decimal,
        Field(
            ge=0,
            description=(
                "Transaction fee in portfolio currency. Buy fees increase average cost; sell "
                "fees reduce realized P&L."
            ),
            examples=["1.25"],
        ),
    ] = Decimal("0")
    executed_at: datetime | None = Field(
        default=None,
        description="Execution time as RFC 3339. Omit to use the current UTC server time.",
        examples=["2026-07-22T13:00:00Z"],
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "request_id": "trade-20260722-001",
                    "ticker": "AAPL",
                    "side": "buy",
                    "quantity": "10",
                    "unit_price": "200.50",
                    "fee": "1.25",
                    "executed_at": "2026-07-22T13:00:00Z",
                }
            ]
        }
    )

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        return value.upper()


class TradeRead(ApiModel):
    """An immutable trade ledger entry."""

    id: str
    portfolio_id: str
    request_id: str
    ticker: str
    side: TradeSide
    quantity: Decimal
    unit_price: Decimal
    fee: Decimal
    executed_at: datetime
    created_at: datetime


class TradePage(ApiModel):
    """A reverse-chronological page of trade ledger entries."""

    items: list[TradeRead]
    offset: int
    limit: int
    total: int


class CashTransactionCreate(ApiModel):
    """Record cash independently from asset trades."""

    request_id: RequestId
    action: CashAction = Field(description="Deposit adds cash; withdraw removes available cash.")
    amount: PositiveDecimal = Field(description="Cash amount in the portfolio base currency.")
    occurred_at: datetime | None = Field(
        default=None,
        description="Event time as RFC 3339. Omit to use the current UTC server time.",
        examples=["2026-07-22T13:00:00Z"],
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "request_id": "cash-20260722-001",
                    "action": "deposit",
                    "amount": "10000",
                }
            ]
        }
    )


class CashTransactionRead(ApiModel):
    """An immutable cash ledger entry."""

    id: str
    portfolio_id: str
    request_id: str
    action: CashAction
    amount: Decimal
    occurred_at: datetime
    created_at: datetime


class CashTransactionPage(ApiModel):
    """A reverse-chronological page of cash ledger entries."""

    items: list[CashTransactionRead]
    offset: int
    limit: int
    total: int


class TagsUpdate(ApiModel):
    """The complete desired tag set for one open position."""

    tags: Annotated[
        list[str],
        Field(
            max_length=50,
            description=(
                "Replacement tag set. Tags are Unicode-normalized, case-folded, deduplicated, "
                "and limited to 50 characters each. Send [] to remove all tags."
            ),
            examples=[["core", "ai", "長期"]],
        ),
    ]

    model_config = ConfigDict(json_schema_extra={"examples": [{"tags": ["core", "ai"]}]})


class TagsRead(ApiModel):
    """Canonical tags attached to a portfolio-specific position."""

    portfolio_id: str
    ticker: str
    tags: list[str]


class QuoteRead(ApiModel):
    """Current quote plus explicit data provenance and freshness."""

    price: Decimal = Field(description="Latest known market price in the instrument currency.")
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    previous_close: Decimal | None
    volume: Decimal | None
    change: Decimal | None
    change_percent: Decimal | None
    market_cap: Decimal | None
    year_high: Decimal | None
    year_low: Decimal | None
    provider_as_of: datetime = Field(description="Timestamp assigned by the market data provider.")
    fetched_at: datetime = Field(
        description="UTC time when this server fetched and cached the quote."
    )
    stale: bool = Field(
        description=(
            "True when refresh failed and an expired cached quote was returned. Inspect warnings."
        )
    )


class IndicatorsRead(ApiModel):
    """Fixed indicators calculated from adjusted daily closing prices."""

    sma20: Decimal | None
    sma50: Decimal | None
    rsi14: Decimal | None
    macd: Decimal | None
    macd_signal: Decimal | None
    macd_histogram: Decimal | None
    calculated_as_of: datetime


class MarketInstrumentRead(ApiModel):
    """Resolved instrument metadata, quote, indicators, and data-quality warnings."""

    ticker: str
    name: str
    asset_type: str
    market: str
    exchange: str | None
    currency: str
    quote: QuoteRead
    indicators: IndicatorsRead
    warnings: list[str] = Field(default_factory=list)


class HistoryBarRead(ApiModel):
    """One adjusted daily OHLCV bar."""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class HistoryRead(ApiModel):
    """Adjusted daily price history for one canonical ticker."""

    ticker: str
    adjusted: bool = True
    bars: list[HistoryBarRead]


class PositionRead(ApiModel):
    """An open position valued using the latest available quote."""

    ticker: str
    name: str
    asset_type: str
    market: str
    currency: str
    quantity: Decimal
    average_cost: Decimal
    current_price: Decimal
    market_value: Decimal
    realized_pnl: Decimal = Field(
        description="Cumulative P&L already realized by sells, after fees."
    )
    unrealized_pnl: Decimal = Field(description="(current_price - average_cost) × open quantity.")
    total_pnl: Decimal = Field(description="realized_pnl + unrealized_pnl.")
    unrealized_pnl_percent: Decimal | None
    weight_percent: Decimal | None = Field(
        description="Position market value as a percentage of securities plus cash."
    )
    tags: list[str]
    price_as_of: datetime
    price_fetched_at: datetime
    price_stale: bool = Field(description="Whether valuation used an expired cached quote.")


class CashPositionRead(ApiModel):
    """Current cash balance and its share of total portfolio value."""

    currency: str
    amount: Decimal
    weight_percent: Decimal | None
    updated_at: datetime | None


class PositionList(ApiModel):
    """Open positions, optionally filtered by portfolio-local tags."""

    items: list[PositionRead]
    warnings: list[str] = Field(default_factory=list)


class PortfolioSummary(ApiModel):
    """Complete single-currency portfolio valuation, allocation, and P&L."""

    portfolio: PortfolioRead
    positions: list[PositionRead]
    cash: CashPositionRead
    securities_value: Decimal
    cash_value: Decimal
    total_value: Decimal
    realized_pnl: Decimal
    unrealized_pnl: Decimal
    total_pnl: Decimal
    valuation_as_of: datetime
    warnings: list[str] = Field(default_factory=list)


class ErrorResponse(ApiModel):
    """Stable machine-readable error envelope used for validation and domain failures."""

    code: str
    message: str
    details: dict[str, Any]


class HealthRead(ApiModel):
    """API and database liveness status."""

    status: str
    database: str
    timestamp: datetime
