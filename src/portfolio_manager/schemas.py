from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from .journal import ActionType, EventType, FlowClassification, PortfolioKind
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
    kind: PortfolioKind = Field(
        default=PortfolioKind.INVESTMENT,
        description=(
            "`investment` holds securities and cash; `cash` holds only cash and rejects any "
            "transaction that would create a position."
        ),
    )
    institution: str | None = Field(
        default=None, description="The bank or broker holding the account, when recorded."
    )
    created_at: datetime


class CashAccountCreate(ApiModel):
    """Open an account that holds cash and never a position.

    A savings account, an e-wallet, or the settlement balance of a broker tracked on its own.
    It is a portfolio in every other respect: the same journal, valuation, and performance.
    """

    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
        Field(description="Unique human-readable account name.", examples=["Cathay savings"]),
    ]
    base_currency: str = Field(
        description="Three-letter ISO currency. The account holds this currency only.",
        examples=["TWD"],
    )
    institution: str | None = Field(
        default=None,
        description="The bank or provider holding the money.",
        examples=["Cathay United Bank"],
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "Cathay savings",
                    "base_currency": "TWD",
                    "institution": "Cathay United Bank",
                }
            ]
        }
    )

    @field_validator("base_currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha() or not value.isascii():
            raise ValueError("base_currency must be a three-letter ISO currency code")
        return value


class LiabilityAccountCreate(ApiModel):
    """Open an account for money owed: a personal loan, a mortgage, a credit line.

    The same book as a cash account with the sign reversed. Its balance is what is outstanding,
    so it runs negative and subtracts from net worth. Drawing the loan down and repaying it are
    ordinary transfers between this account and wherever the money went.

    It records the balance and the cash that moves, not the loan's terms: there is no rate,
    schedule, or remaining-instalment count here, and `institution` is where a note about them
    belongs until something actually computes with them.
    """

    name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
        Field(description="Unique human-readable account name.", examples=["Cathay credit loan"]),
    ]
    base_currency: str = Field(
        description="Three-letter ISO currency. The debt is denominated in this currency only.",
        examples=["TWD"],
    )
    institution: str | None = Field(
        default=None,
        description="The lender.",
        examples=["Cathay United Bank"],
    )

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "name": "Cathay credit loan",
                    "base_currency": "TWD",
                    "institution": "Cathay United Bank",
                }
            ]
        }
    )

    @field_validator("base_currency")
    @classmethod
    def validate_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if len(value) != 3 or not value.isalpha() or not value.isascii():
            raise ValueError("base_currency must be a three-letter ISO currency code")
        return value


class TransferCreate(ApiModel):
    """Move cash between two portfolios as one indivisible event."""

    request_id: RequestId
    from_portfolio_id: str = Field(description="The portfolio the money leaves.")
    to_portfolio_id: str = Field(description="The portfolio the money arrives in.")
    amount: PositiveDecimal = Field(
        description="Amount leaving the source, in the source's currency."
    )
    fx_rate: PositiveDecimal | None = Field(
        default=None,
        description=(
            "Destination units per source unit, as actually executed. Required when the two "
            "portfolios differ in currency and rejected when they do not. This service never "
            "supplies a market rate here: the gap between a market rate and the one you were "
            "given would enter the ledger as cash that came from nowhere."
        ),
        examples=["0.03125"],
    )
    occurred_at: datetime | None = Field(
        default=None, description="When the transfer happened. Defaults to now."
    )
    source_reference: str | None = None
    memo: str | None = None


class TransferSideRead(ApiModel):
    """One half of a transfer, as recorded in its own portfolio."""

    portfolio_id: str
    event_id: str
    currency: str
    amount: Decimal = Field(description="Signed: negative leaving the source, positive arriving.")
    role: str = Field(description="`out` or `in`.")


class TransferRead(ApiModel):
    """Both halves of one transfer and the rate between them."""

    transfer_id: str
    status: str = Field(description="`posted` or `reversed`.")
    occurred_at: datetime
    fx_rate: Decimal | None = Field(
        default=None, description="Null when both portfolios share a currency."
    )
    sent: TransferSideRead
    received: TransferSideRead


class TransferReversalCreate(ApiModel):
    """Unwind both halves of a transfer together."""

    request_id: RequestId
    memo: str | None = None


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


class CacheClearRead(ApiModel):
    """What a cache-clear actually removed, including when there was no cache to clear."""

    ticker: str
    cleared_keys: int = Field(
        description="Number of cached entries removed. Zero means nothing was cached."
    )
    cache_enabled: bool = Field(
        description=(
            "False when no cache is configured. The call then did nothing, rather than "
            "silently reporting success for a cache that does not exist."
        )
    )
    warnings: list[str] = []


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


class TransactionCreate(ApiModel):
    """Record one economic event with every leg posted atomically."""

    request_id: RequestId
    transaction_type: EventType = Field(
        description=(
            "buy, sell, deposit, withdrawal, transfer_in, transfer_out, dividend, interest, fee, "
            "or tax."
        ),
        examples=["buy"],
    )
    ticker: str | None = Field(
        default=None,
        description="Required for buy and sell; optional on dividend and interest.",
        examples=["AAPL"],
    )
    quantity: Decimal | None = Field(default=None, description="Required for buy and sell.")
    unit_price: Decimal | None = Field(
        default=None, description="Actual execution price; required for buy and sell."
    )
    amount: Decimal | None = Field(
        default=None,
        description=(
            "Positive magnitude for non-trade events. Direction comes from `transaction_type`, "
            "so a withdrawal takes a positive amount. For income this is the gross figure."
        ),
    )
    fee: Decimal = Field(default=Decimal("0"), ge=0, description="Capitalized into cost basis.")
    tax: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        description="Withholding on income, or a capitalized trade tax.",
    )
    settlement_amount: Decimal | None = Field(
        default=None,
        description=(
            "Signed cash actually settled. Overrides the computed figure when a broker reports "
            "an exact amount; the event must still balance."
        ),
    )
    occurred_at: datetime | None = Field(default=None, description="Defaults to server time.")
    trade_date: datetime | None = None
    settlement_date: datetime | None = None
    source_reference: str | None = Field(
        default=None, description="Broker confirmation or statement ID, for reconciliation."
    )
    memo: str | None = None

    # This is the only write path into the ledger, so the examples carry the three shapes an
    # agent needs: cash in, a purchase, and an opening balance transferred from another account.
    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "request_id": "dep-20260801-001",
                    "transaction_type": "deposit",
                    "amount": "10000",
                },
                {
                    "request_id": "buy-20260801-001",
                    "transaction_type": "buy",
                    "ticker": "AAPL",
                    "quantity": "10",
                    "unit_price": "200",
                    "fee": "1",
                },
                {
                    "request_id": "open-20260102-001",
                    "transaction_type": "transfer_in",
                    "amount": "65000",
                    "occurred_at": "2026-01-02T00:00:00Z",
                    "memo": "Opening balance: cash plus the cost basis of transferred holdings",
                },
            ]
        }
    )

    @field_validator("quantity", "unit_price")
    @classmethod
    def validate_positive(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and value <= 0:
            raise ValueError("must be greater than zero")
        return value


class TransactionReverse(ApiModel):
    """Undo a posted event by writing its mirror image."""

    request_id: RequestId
    memo: str | None = Field(default=None, description="Why the original event was undone.")


class JournalLegRead(ApiModel):
    """One side of an event, in the currency named on the leg."""

    leg_type: str
    account_role: str
    currency: str
    instrument_id: str | None
    quantity_delta: Decimal | None
    amount_delta: Decimal | None
    unit_price: Decimal | None
    fx_rate: Decimal | None
    metadata: str | None = None
    ticker: str | None = Field(
        default=None,
        description=(
            "The instrument's ticker, resolved from `instrument_id` for display. Null on cash, "
            "fee, and tax legs, which name no instrument."
        ),
    )


class BalanceRead(ApiModel):
    """Proof that the event's legs net to zero in its functional currency."""

    balanced: bool
    residual: Decimal
    functional_currency: str
    leg_count: int
    warnings: list[str] = Field(default_factory=list)


class JournalEventRead(ApiModel):
    """A posted event header. Legs are present only when the caller asked for them."""

    id: str
    portfolio_id: str
    request_id: str
    event_type: str
    status: str
    functional_currency: str
    occurred_at: datetime
    trade_date: datetime | None
    settlement_date: datetime | None
    source: str
    source_reference: str | None
    memo: str | None
    reverses_event_id: str | None
    flow_classification: FlowClassification = Field(
        description=(
            "Whether this event moved investor capital across the portfolio boundary (external) "
            "or was portfolio activity (internal), derived from the event type."
        )
    )
    created_at: datetime
    legs: list[JournalLegRead] | None = Field(
        default=None,
        description=(
            "This event's legs, present only when the request set `include_legs`. Null means "
            "the legs were not requested, not that the event has none."
        ),
    )


class JournalEventDetail(ApiModel):
    """An event with its legs, balance validation, and reversal chain."""

    event: JournalEventRead
    legs: list[JournalLegRead]
    balance: BalanceRead | None
    flow_classification: FlowClassification | None = Field(
        description=(
            "external for investor contributions and withdrawals; internal for trades, income, "
            "fees, and corporate actions. Performance measurement neutralizes external flows."
        )
    )
    reverses_event_id: str | None
    reversed_by_event_id: str | None


class JournalEventPage(ApiModel):
    """A page of journal events, newest first."""

    items: list[JournalEventRead]
    total: int
    offset: int
    limit: int


class CorporateActionCreate(ApiModel):
    """Record an announced corporate action as a fact about an instrument."""

    request_id: RequestId
    ticker: str = Field(description="Instrument the action applies to.", examples=["AAPL"])
    action_type: ActionType = Field(
        description=(
            "cash_dividend, interest, split, reverse_split, stock_dividend, return_of_capital, "
            "symbol_change, merger, or spinoff."
        ),
        examples=["split"],
    )
    ex_date: datetime = Field(description="Ex-date; holdings before this date qualify.")
    source: str = Field(
        description="Where these facts came from, retained for audit.",
        examples=["issuer announcement"],
    )
    ratio: Decimal | None = Field(
        default=None,
        description=(
            "New shares per existing share. 2 for a 2-for-1 split, 0.5 for a 1-for-2 reverse "
            "split. Required for splits and stock dividends."
        ),
    )
    cash_amount: Decimal | None = Field(
        default=None,
        description=(
            "Cash **per share**, not the total. Required for dividends, interest, and return "
            "of capital."
        ),
    )
    currency: str | None = None
    withholding_tax: Decimal | None = Field(
        default=None, description="Total withholding on the distribution, if any."
    )
    new_ticker: str | None = Field(
        default=None, description="Successor instrument for a merger, spin-off, or symbol change."
    )
    cost_allocation_percent: Decimal | None = Field(
        default=None,
        description=(
            "Percent of original basis staying with the original instrument. Supply only when the "
            "issuer disclosed it; leaving it null marks the action cost-basis unresolved rather "
            "than guessing an allocation."
        ),
    )
    announcement_date: datetime | None = None
    record_date: datetime | None = None
    pay_date: datetime | None = None
    effective_at: datetime | None = Field(
        default=None, description="Defaults to the ex-date."
    )
    source_reference: str | None = None


class CorporateActionRead(ApiModel):
    """A recorded corporate action."""

    id: str
    instrument_id: str
    action_type: str
    status: str
    ex_date: datetime
    record_date: datetime | None
    pay_date: datetime | None
    effective_at: datetime
    ratio: Decimal | None
    cash_amount: Decimal | None
    currency: str | None
    withholding_tax: Decimal | None
    new_instrument_id: str | None
    cost_allocation_percent: Decimal | None
    cost_basis_unresolved: bool = Field(
        description=(
            "True when the correct basis treatment is unknown. The action is still recorded; it "
            "is never applied with an invented allocation."
        )
    )
    source: str
    source_reference: str | None
    created_at: datetime


class CorporateActionPage(ApiModel):
    """A page of recorded corporate actions."""

    items: list[CorporateActionRead]
    total: int
    offset: int
    limit: int


class CorporateActionPreview(ApiModel):
    """What applying an action would do, computed without writing anything."""

    portfolio_id: str
    action_id: str
    action_type: str
    applicable: bool = Field(
        description="False when the action cannot be applied; see `warnings` for why."
    )
    original_quantity: Decimal | None
    original_average_cost: Decimal | None
    resulting_quantity: Decimal | None
    resulting_average_cost: Decimal | None
    cash_amount: Decimal | None
    withholding_tax: Decimal | None
    cash_in_lieu: Decimal | None
    fractional_handling: str | None
    cost_basis_unresolved: bool
    legs: list[JournalLegRead] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class CorporateActionApply(ApiModel):
    """Apply a recorded action to one portfolio."""

    request_id: RequestId


class CorporateActionApplicationRead(ApiModel):
    """The record of an action having been applied, with before and after values."""

    id: str
    corporate_action_id: str
    portfolio_id: str
    journal_event_id: str | None
    original_quantity: Decimal | None
    original_average_cost: Decimal | None
    resulting_quantity: Decimal | None
    resulting_average_cost: Decimal | None
    cash_in_lieu: Decimal | None
    fractional_handling: str | None
    status: str
    created_at: datetime


class SnapshotCreate(ApiModel):
    """Request to value one portfolio on one date."""

    valuation_date: date = Field(
        description="The date to value, in the portfolio's terms. Must not be in the future."
    )
    force_revision: bool = Field(
        default=False,
        description=(
            "Replace an existing snapshot for this date instead of returning it. Use only when "
            "the stored figures are known to be wrong; a normal retry should leave them alone."
        ),
    )


class PositionSnapshotRead(ApiModel):
    """One holding inside a snapshot, with the price actually used."""

    instrument_id: str
    ticker_at_time: str = Field(
        description="The ticker as it stood on the valuation date, which may differ from today's."
    )
    quantity: Decimal
    average_cost: Decimal
    cost_basis: Decimal
    local_currency: str
    price: Decimal | None = Field(
        description="Null when no price was available on or before the valuation date."
    )
    market_value: Decimal | None = Field(
        description="Null whenever `price` is null; an unpriced holding is never valued at zero."
    )
    price_as_of: datetime | None
    price_provider: str | None
    price_stale: bool = Field(
        description="True when the price was carried forward across a long gap in the data."
    )
    warnings: list[str]


class SnapshotRead(ApiModel):
    """A portfolio's value on one date, priced only with data available at that time."""

    id: str
    portfolio_id: str
    valuation_date: date
    valuation_as_of: datetime = Field(
        description="The cutoff instant; events after it are excluded from this snapshot."
    )
    base_currency: str
    securities_value: Decimal = Field(
        description="Priced holdings only. Holdings that could not be priced are excluded."
    )
    unpriced_market_value: Decimal = Field(
        description="Cost basis of holdings with no available price, so the gap stays visible."
    )
    cash_value: Decimal
    total_value: Decimal
    cost_basis: Decimal
    external_flow_amount: Decimal = Field(
        description="Net investor contributions, excluding income, fees, and trading."
    )
    income_amount: Decimal
    fee_amount: Decimal
    tax_amount: Decimal
    pricing_coverage_percent: Decimal
    positions_total: int
    positions_priced: int
    calculation_version: str
    status: str = Field(description="`complete` when every holding was priced, else `partial`.")
    calculation_method: str
    warnings: list[str]
    positions: list[PositionSnapshotRead]
    created_at: datetime


class SnapshotSummary(ApiModel):
    """One snapshot without its position detail, for series responses."""

    id: str
    valuation_date: date
    valuation_as_of: datetime
    securities_value: Decimal
    unpriced_market_value: Decimal
    cash_value: Decimal
    total_value: Decimal
    external_flow_amount: Decimal
    pricing_coverage_percent: Decimal
    status: str


class NavHistoryRead(ApiModel):
    """A daily value series with the coverage needed to judge whether it can be trusted."""

    portfolio_id: str
    base_currency: str
    start_date: date
    end_date: date
    calculation_version: str
    calculation_method: str
    snapshots: list[SnapshotSummary]
    missing_dates: list[date] = Field(
        description="Dates in range with no snapshot; they are reported, never interpolated."
    )
    partial_snapshots: int
    warnings: list[str]


class RebuildRequest(ApiModel):
    """Request to build snapshots across a date range."""

    start_date: date
    end_date: date
    force_revision: bool = Field(
        default=False,
        description="Rewrite dates that already have a snapshot rather than skipping them.",
    )


class RebuildRead(ApiModel):
    """The outcome of a range rebuild, in enough detail to re-run it safely."""

    portfolio_id: str
    start_date: date
    end_date: date
    calculation_version: str
    created: int
    skipped_existing: int = Field(
        description="Dates left untouched because a snapshot already existed."
    )
    partial: int
    failed: list[str]
    warnings: list[str]


class DailyReturnRead(ApiModel):
    """One sub-period of a return series."""

    valuation_date: date
    beginning_value: Decimal
    ending_value: Decimal
    external_flow: Decimal
    return_percent: Decimal | None = Field(
        description="Null when the day had no value to measure against, never 0 in its place."
    )


class PerformanceCoverageRead(ApiModel):
    """Whether the series behind a return is complete enough to rely on."""

    snapshots_used: int
    missing_dates: list[date]
    partial_snapshots: int
    unclassified_flow_events: int
    is_reliable: bool = Field(
        description=(
            "True only when the period has no gaps, no partial snapshots, and every event's "
            "cash flow could be classified. False means the figures are computable but biased."
        )
    )
    warnings: list[str]


class PerformanceRead(ApiModel):
    """Return over a period, with the method and the data quality behind it."""

    portfolio_id: str
    base_currency: str
    start_date: date
    end_date: date
    beginning_value: Decimal
    ending_value: Decimal
    external_inflows: Decimal = Field(description="Investor capital added, excluding income.")
    external_outflows: Decimal = Field(description="Investor capital withdrawn; negative.")
    income: Decimal
    fees: Decimal
    taxes: Decimal
    twr_percent: Decimal | None = Field(
        description=(
            "Time-weighted return: how the holdings performed, with the effect of money "
            "arriving and leaving removed. Compare this against a benchmark."
        )
    )
    annualized_twr_percent: Decimal | None = Field(
        description="Null for periods under 30 days, where annualizing would magnify noise."
    )
    xirr_percent: Decimal | None = Field(
        description=(
            "Money-weighted return: what the investor earned on the capital they had at risk, "
            "so the timing of contributions moves it. Null when no rate solves the flows."
        )
    )
    xirr_unavailable_reason: str | None = Field(
        description="Why XIRR is null, when it is. Never an opaque failure."
    )
    twr_method: str
    twr_method_description: str
    xirr_method: str
    xirr_method_description: str
    calculation_version: str
    coverage: PerformanceCoverageRead
    daily_returns: list[DailyReturnRead]


class GroupCreate(ApiModel):
    """Request to report several portfolios together."""

    name: str
    reporting_currency: str = Field(description="ISO code the group's totals are expressed in.")
    portfolio_ids: list[str]


class GroupMembersUpdate(ApiModel):
    """Replace a group's membership."""

    portfolio_ids: list[str]


class GroupRead(ApiModel):
    """A group and the portfolios currently in it."""

    id: str
    name: str
    reporting_currency: str
    portfolio_ids: list[str]
    created_at: datetime
    updated_at: datetime


class FxRateRead(ApiModel):
    """One rate used in a conversion, with how it was derived."""

    base_currency: str
    quote_currency: str
    rate: Decimal
    method: str = Field(description="identity, direct, inverse, or cross.")
    conversion_path: list[str] = Field(
        description="The currencies traversed, e.g. ['TWD', 'USD', 'EUR'] for a cross."
    )
    price_as_of: datetime | None
    provider: str | None
    is_stale: bool
    warnings: list[str]


class ConsolidatedPositionRead(ApiModel):
    """One holding in both its own currency and the reporting currency."""

    portfolio_id: str
    portfolio_name: str
    instrument_id: str | None
    ticker: str
    issuer_id: str | None
    quantity: Decimal
    average_cost: Decimal
    local_currency: str
    local_price: Decimal | None
    local_market_value: Decimal | None
    reporting_market_value: Decimal | None = Field(
        description="Null when the currency could not be converted; never a guessed value."
    )
    fx_rate: Decimal | None
    fx_method: str | None
    fx_path: list[str]
    fx_as_of: datetime | None
    weight_percent: Decimal | None
    warnings: list[str]


class CurrencyTotalRead(ApiModel):
    currency: str
    local_amount: Decimal
    reporting_amount: Decimal | None


class IssuerExposureRead(ApiModel):
    """Exposure to one issuer across every listing and portfolio in the group."""

    issuer_id: str
    issuer_name: str
    reporting_value: Decimal
    weight_percent: Decimal | None
    tickers: list[str]


class UnconvertedAmountRead(ApiModel):
    """Value excluded from the totals because its currency could not be converted."""

    currency: str
    amount: Decimal
    reason: str


class ConsolidatedSummaryRead(ApiModel):
    """A group's holdings and cash expressed in one currency."""

    group_id: str
    group_name: str
    reporting_currency: str
    as_of: date
    portfolio_ids: list[str]
    positions: list[ConsolidatedPositionRead]
    cash_by_currency: list[CurrencyTotalRead]
    currency_exposure: list[CurrencyTotalRead]
    issuer_exposure: list[IssuerExposureRead]
    securities_value: Decimal
    cash_value: Decimal
    total_value: Decimal = Field(
        description="Converted value only. Anything in `unconverted` is excluded from this."
    )
    assets_value: Decimal = Field(
        description="What the group owns: `net_value` with the liabilities taken back out."
    )
    liabilities_value: Decimal = Field(
        description=(
            "What the group owes, as a negative number so that `assets_value` + "
            "`liabilities_value` == `net_value`. Zero when the group holds no liability account."
        )
    )
    net_value: Decimal = Field(
        description="Assets less liabilities. Identical to `total_value`, which was always net."
    )
    unconverted: list[UnconvertedAmountRead]
    converted_value_coverage_percent: Decimal = Field(
        description="Share of gross value that reached the reporting currency."
    )
    fx_rates_used: list[FxRateRead]
    calculation_method: str
    warnings: list[str]


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
