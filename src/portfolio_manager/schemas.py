from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .market import HistoryAdjustment, HistoryInterval
from .taxonomy import Provenance

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
    """OHLCV history with explicit request, observation, and adjustment provenance."""

    ticker: str
    provider: str
    interval: HistoryInterval
    adjustment: HistoryAdjustment
    adjusted: bool = Field(description="Compatibility flag; prefer the precise adjustment field.")
    requested_start_date: date | None
    requested_end_date: date | None
    actual_first_observation: date | None
    actual_last_observation: date | None
    fetched_at: datetime
    warnings: list[str] = Field(default_factory=list)
    bars: list[HistoryBarRead]


class TrendRead(ApiModel):
    sma20: Decimal | None
    sma50: Decimal | None
    sma200: Decimal | None
    sma50_change_20d_percent: Decimal | None
    sma200_change_20d_percent: Decimal | None
    price_vs_sma20_percent: Decimal | None
    price_vs_sma50_percent: Decimal | None
    price_vs_sma200_percent: Decimal | None


class MomentumRead(ApiModel):
    return_20d_percent: Decimal | None
    return_60d_percent: Decimal | None
    return_120d_percent: Decimal | None
    return_252d_percent: Decimal | None
    rsi14: Decimal | None
    macd: Decimal | None
    macd_signal: Decimal | None
    macd_histogram: Decimal | None


class VolatilityRead(ApiModel):
    atr14: Decimal | None
    atr14_percent: Decimal | None
    realized_volatility_20d_percent: Decimal | None
    realized_volatility_60d_percent: Decimal | None
    drawdown_from_252d_high_percent: Decimal | None


class VolumeAnalysisRead(ApiModel):
    latest_vs_20d_average: Decimal | None
    latest_252d_percentile: Decimal | None


class RelativeStrengthRead(ApiModel):
    benchmark: str
    common_observation_count: int
    return_20d_percent: Decimal | None
    return_60d_percent: Decimal | None
    return_120d_percent: Decimal | None
    return_252d_percent: Decimal | None


class EventAnalysisRead(ApiModel):
    """Event bar and daily-OHLCV-derived anchored VWAP approximation."""

    requested_event_date: date
    effective_anchor_date: date | None
    gap_percent: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume_percentile: Decimal | None
    anchored_vwap: Decimal | None = Field(
        description=(
            "Approximation from daily typical price (high + low + close) / 3, weighted by "
            "daily volume from the effective anchor through as_of."
        )
    )


class TechnicalSnapshotRead(ApiModel):
    """Reproducible technical market snapshot capped at the actual as-of observation."""

    ticker: str
    provider: str
    as_of: date
    interval: HistoryInterval
    adjustment: HistoryAdjustment
    actual_start_date: date
    actual_end_date: date
    bar_count: int
    trend: TrendRead
    momentum: MomentumRead
    volatility: VolatilityRead
    volume: VolumeAnalysisRead
    relative_strength: RelativeStrengthRead | None
    event_analysis: EventAnalysisRead | None
    warnings: list[str] = Field(default_factory=list)


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


class IssuerRead(ApiModel):
    """The economic entity behind a listing; shared by ADRs and local lines of one company."""

    id: str
    legal_name: str
    display_name: str
    country_of_domicile: str | None
    lei: str | None


class InstrumentAliasRead(ApiModel):
    """A provider symbol that resolves to this instrument, including retired tickers."""

    provider: str
    provider_symbol: str
    exchange: str | None
    effective_from: datetime
    effective_to: datetime | None


class ClassificationFieldRead(ApiModel):
    """One classification field's winning value and the provenance that produced it."""

    value: str | None
    provenance: Provenance = Field(
        description=(
            "Trust rank that won this field: manual_override > verified_internal > provider > "
            "derived > unclassified."
        )
    )
    source: str
    effective_at: datetime | None
    confidence: Decimal | None
    note: str | None


class InstrumentProfileRead(ApiModel):
    """Stable identity, issuer, and field-level classification provenance for one instrument."""

    instrument_id: str
    ticker: str
    name: str
    currency: str
    market: str
    exchange: str | None
    is_fund: bool
    asset_type: str = Field(
        description="Legacy coarse type (stock/crypto). Prefer classification.security_type."
    )
    issuer: IssuerRead | None
    classification: dict[str, ClassificationFieldRead] = Field(
        description="Resolved value per field. Absent fields are unclassified."
    )
    aliases: list[InstrumentAliasRead] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ClassificationOverrideUpdate(ApiModel):
    """Manually correct one classification field without destroying provider data."""

    request_id: RequestId
    field: str = Field(
        description="One of asset_class, security_type, sub_asset_class, country_of_risk, "
        "is_cash_equivalent.",
        examples=["asset_class"],
    )
    value: str | None = Field(
        default=None,
        description="Taxonomy member for the field. Ignored when `retract` is true.",
        examples=["commodity"],
    )
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
        Field(description="Why this override exists; retained for audit.", examples=["GLD"]),
    ]
    effective_at: datetime | None = Field(
        default=None, description="When the override takes effect. Defaults to server time."
    )
    retract: bool = Field(
        default=False,
        description="Retract the existing override, restoring the provider-derived value.",
    )


class IssuerMappingUpdate(ApiModel):
    """Attach an instrument to an issuer so cross-listing exposure can aggregate."""

    request_id: RequestId
    legal_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=200),
        Field(examples=["Taiwan Semiconductor Manufacturing Company Limited"]),
    ]
    display_name: str | None = Field(default=None, examples=["TSMC"])
    country_of_domicile: str | None = Field(
        default=None, description="Two-letter ISO country code.", examples=["TW"]
    )
    lei: str | None = Field(default=None, description="Legal Entity Identifier when known.")
    issuer_id: str | None = Field(
        default=None,
        description="Attach to this existing issuer instead of matching or creating by name.",
    )


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
