import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import Depends, FastAPI, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .cache import CachingMarketProvider, build_provider
from .config import settings
from .consolidation import (
    build_consolidated_summary,
    create_group,
    delete_group,
    get_group,
    member_portfolio_ids,
    rename_group,
    replace_members,
)
from .corporate_actions import (
    apply_corporate_action,
    list_actions,
    preview_application,
    record_corporate_action,
)
from .db import get_session
from .errors import DomainError, not_found
from .identity import (
    build_instrument_profile,
    map_issuer,
    resolve_instrument,
    set_classification_override,
)
from .journal import LegType, PortfolioKind, derived_flow
from .market import HistoryAdjustment, HistoryInterval, MarketProvider, YahooMarketProvider
from .models import Portfolio, PortfolioGroup
from .performance import (
    TWR_METHOD_DESCRIPTION,
    XIRR_METHOD_DESCRIPTION,
    calculate_performance,
)
from .postings import (
    TransactionRequest,
    _legs_of,
    event_detail,
    legs_for_events,
    list_events,
    record_transaction,
    reverse_transaction,
    reversed_types_for,
    ticker_index,
)
from .schemas import (
    BalanceRead,
    CacheClearRead,
    CashAccountCreate,
    ClassificationOverrideUpdate,
    ConsolidatedPositionRead,
    ConsolidatedSummaryRead,
    CorporateActionApplicationRead,
    CorporateActionApply,
    CorporateActionCreate,
    CorporateActionPage,
    CorporateActionPreview,
    CorporateActionRead,
    CurrencyTotalRead,
    DailyReturnRead,
    ErrorResponse,
    FxRateRead,
    GroupCreate,
    GroupMembersUpdate,
    GroupRead,
    GroupUpdate,
    HealthRead,
    HistoryBarRead,
    HistoryRead,
    InstrumentProfileRead,
    IssuerExposureRead,
    IssuerMappingUpdate,
    JournalEventDetail,
    JournalEventPage,
    JournalEventRead,
    JournalLegRead,
    LiabilityAccountCreate,
    MarketInstrumentRead,
    NavHistoryRead,
    PerformanceCoverageRead,
    PerformanceRead,
    PortfolioCreate,
    PortfolioRead,
    PortfolioSummary,
    PortfolioUpdate,
    PositionList,
    PositionSnapshotRead,
    RebuildRead,
    RebuildRequest,
    SnapshotCreate,
    SnapshotRead,
    SnapshotSummary,
    TagMode,
    TagsRead,
    TagsUpdate,
    TechnicalSnapshotRead,
    TransactionCreate,
    TransactionReverse,
    TransferCreate,
    TransferRead,
    TransferReversalCreate,
    TransferSideRead,
    UnconvertedAmountRead,
    utc_now,
)
from .services import (
    MarketService,
    build_summary,
    create_cash_account,
    create_liability_account,
    create_portfolio,
    delete_portfolio,
    get_portfolio,
    market_response,
    normalize_tag,
    replace_tags,
    update_portfolio,
)
from .transfers import events_of, reverse_transfer, transfer_cash
from .valuation import (
    CALCULATION_VERSION,
    SnapshotStatus,
    create_snapshot,
    list_snapshots,
    missing_dates,
    rebuild_snapshots,
    snapshot_positions,
    snapshot_warnings,
)

API_DESCRIPTION = """
Local, single-user portfolio accounting API designed for autonomous agents and generated tools.

## Agent contract

- **This records completed transactions; it never places market orders.**
- Every portfolio has exactly one `base_currency`. A trade is rejected unless the instrument's
  quote currency matches it. Use separate portfolios for USD and TWD assets.
- `record_transaction` is the single way to record activity. It posts a settlement atomically:
  security, cash, fee, and tax legs commit together or not at all, so a position can never move
  without the cash that paid for it. Correct a posted event with `reverse_transaction`; events are
  never edited or deleted.
- Send decimal values as JSON strings for exact input. Decimal values in responses are strings.
- All timestamps are UTC RFC 3339 values. Omitted transaction times default to server time.
- Mutation requests require a client-generated `request_id`. Retrying the same body with the same
  ID is safe; reusing an ID for different data returns `409 idempotency_conflict`.
- Spot positions cannot become negative and cash cannot be overdrawn.

## Recommended workflow

1. Call `create_portfolio` once and retain its `id`.
2. Call `record_transaction` with `deposit` to establish cash.
3. Call `get_market_instrument` to validate a ticker and inspect its currency and quote timestamp.
4. Call `get_instrument_profile` when the asset class matters: `asset_type` is a coarse legacy
   field that reports every non-crypto symbol as "stock".
5. Call `record_transaction` with the actual execution price, quantity, and fee.
6. Call `replace_position_tags` to attach strategy context.
7. Call `get_portfolio_summary` for valuation, allocation, and P&L, or `list_positions` to filter
   holdings by tags.
8. When an issuer announces a split or dividend, call `record_corporate_action`, then
   `preview_corporate_action_application`, then `apply_corporate_action`.

## Data transparency

Derived values report where they came from. Classification fields carry a `provenance`; journal
events carry a `balance` proving the legs net to zero and a `flow_classification` distinguishing
investor money from portfolio returns. Values this service cannot determine are reported as
unclassified or unresolved with a warning, never filled with a plausible default.

## Market-data reliability

Quotes are cached for five minutes by default while the instrument's market is open, and until the
next open once it closes, since a closed market's last trade cannot change. Crypto trades
continuously and always uses the five-minute window. A quote served from cache is current, not
stale. If Yahoo refresh fails and an older quote exists, the API returns it with `stale=true` and
a warning — that flag means a refresh failed, never merely that the quote was cached. A
`503 market_data_unavailable` means no usable cached data exists. Do not treat this API as an
execution venue or real-time market-data feed.
Research history and technical snapshots are fetched on demand. Pass the report cutoff as `as_of`;
then inspect the returned actual date, adjustment, provider, and warnings before using results.

## Error handling

Errors use `{code, message, details}`. Agents should branch on `code`, not human-readable text.
Correct `422` input or portfolio-rule errors before retrying. Retry `503` with backoff. A `409`
requires checking whether the same `request_id` was accidentally reused with different content.
"""

OPENAPI_TAGS = [
    {
        "name": "system",
        "description": "Service liveness. Use before a workflow when availability is uncertain.",
    },
    {
        "name": "portfolios",
        "description": (
            "Create isolated single-currency portfolios and read complete valuation summaries."
        ),
    },
    {
        "name": "positions",
        "description": "Read open holdings, filter by tags, and replace position-local tag sets.",
    },
    {
        "name": "market",
        "description": (
            "Resolve Yahoo-compatible tickers and read timestamped quotes, reproducible history, "
            "and technical research snapshots. Indicators may be null when history is insufficient."
        ),
    },
    {
        "name": "instruments",
        "description": (
            "Stable instrument identity, issuer mapping, and asset classification. Every "
            "classification field reports the provenance that produced it; unresolved fields stay "
            "unclassified and are reported in warnings instead of being guessed."
        ),
    },
    {
        "name": "journal",
        "description": (
            "Atomic double-entry ledger. A transaction posts its security, cash, fee, and tax "
            "legs together or not at all, so a position can never move without its settlement. "
            "Posted events are immutable: corrections are reversals, never edits or deletes."
        ),
    },
    {
        "name": "corporate-actions",
        "description": (
            "Splits, dividends, and other issuer events. Recording an action and applying it to a "
            "portfolio are separate steps, with a preview in between. Actions whose cost-basis "
            "treatment depends on undisclosed or jurisdiction-specific rules are reported as "
            "unresolved rather than applied with an invented allocation."
        ),
    },
    {
        "name": "consolidation",
        "description": (
            "Report several portfolios together in one currency. Every converted figure keeps "
            "its original alongside the rate, path, and rate date that produced it. Value whose "
            "currency cannot be converted is excluded from the totals and reported explicitly "
            "rather than converted at a guessed rate."
        ),
    },
    {
        "name": "valuation",
        "description": (
            "Point-in-time snapshots of what a portfolio was worth. Holdings and cash are "
            "rebuilt from the journal at the cutoff and priced with data available on that date, "
            "never with today's quote. A holding that cannot be priced is excluded and the "
            "snapshot reported as partial, rather than being valued at zero."
        ),
    },
]

GLOBAL_RESPONSES = {
    422: {
        "model": ErrorResponse,
        "description": "Invalid request data or a violated portfolio rule; inspect `code`.",
    }
}

AGENT_SKILL_METADATA = {
    "name": "local-portfolio-manager",
    "purpose": (
        "Track completed trades, cash, allocation, and P&L, and research historical market state."
    ),
    "instructions": [
        "Never describe recording a transaction as placing or executing an order.",
        "Keep every portfolio single-currency and verify ticker currency before a trade.",
        "Use record_transaction, which posts a position and its settlement cash atomically.",
        "Correct a posted event with reverse_transaction; never edit or delete one.",
        "Generate one unique request_id per logical mutation and reuse it only for exact retries.",
        "Check stale, provider_as_of, fetched_at, and warnings before using a market price.",
        "Send exact quantities and monetary values as decimal strings.",
        "Branch on the error code and retry only transient market_data_unavailable failures.",
        "For company research, pass the report cutoff as technical-snapshot as_of.",
        "Inspect provider, actual as-of, adjustment, and warnings before interpreting indicators.",
        "Use get_instrument_profile for asset class; asset_type reports every non-crypto symbol "
        "as stock, so an ETF and a commodity trust are indistinguishable there.",
        "Prefer a classification with manual_override or verified_internal provenance over "
        "provider or derived.",
        "Report unclassified and cost_basis_unresolved values as unknown; never substitute an "
        "assumed value.",
        "Preview a corporate action before applying it, and never invent a cost allocation.",
    ],
    "workflow": [
        "list_portfolios or create_portfolio",
        "get_market_instrument and get_instrument_profile",
        "get_market_history or get_technical_snapshot for market research",
        "record_transaction for deposits, trades, and income",
        "replace_position_tags",
        "get_portfolio_summary or list_positions",
        "record_corporate_action, preview_corporate_action_application, apply_corporate_action",
    ],
}


app = FastAPI(
    title="Local Portfolio Manager",
    version="0.8.0",
    summary="Agent-friendly accounting for cash, stocks, and crypto portfolios",
    description=API_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
    servers=[{"url": "/", "description": "Default local server"}],
    responses=GLOBAL_RESPONSES,
)

STATIC_DIR = FilePath(__file__).parent / "static"

_fastapi_openapi = app.openapi


def agent_friendly_openapi():
    schema = _fastapi_openapi()
    schema["x-agent-skill"] = AGENT_SKILL_METADATA
    return schema


app.openapi = agent_friendly_openapi


_market_provider = build_provider(YahooMarketProvider())


def get_market_provider() -> MarketProvider:
    return _market_provider


SessionDep = Annotated[Session, Depends(get_session)]
ProviderDep = Annotated[MarketProvider, Depends(get_market_provider)]
PortfolioId = Annotated[
    str,
    Path(
        description="Portfolio UUID returned by `create_portfolio`.",
        examples=["8b83aa9a-629f-4a40-a167-cb980724a888"],
    ),
]
GroupId = Annotated[
    str,
    Path(
        description="Portfolio group UUID returned by `create_portfolio_group`.",
        examples=["3f1c2d5e-8a4b-4c6d-9e0f-1a2b3c4d5e6f"],
    ),
]
TickerPath = Annotated[
    str,
    Path(
        description="Yahoo-compatible ticker such as AAPL, 2330.TW, 8069.TWO, or BTC-USD.",
        examples=["AAPL"],
    ),
]


def market_service(session: SessionDep, provider: ProviderDep) -> MarketService:
    return MarketService(session, provider, settings.quote_ttl_seconds)


MarketDep = Annotated[MarketService, Depends(market_service)]


@app.exception_handler(DomainError)
async def handle_domain_error(_request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message, "details": exc.details},
    )


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = []
    for error in exc.errors():
        errors.append(
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "message": error["msg"],
                "type": error["type"],
            }
        )
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "Request validation failed",
            "details": {"errors": errors},
        },
    )


@app.get(
    "/health",
    response_model=HealthRead,
    operation_id="get_health",
    summary="Check API and database availability",
    response_description="Current liveness state",
    tags=["system"],
)
def health(session: SessionDep) -> HealthRead:
    """Use this lightweight endpoint before a longer agent workflow when availability is unknown."""
    session.execute(text("SELECT 1"))
    return HealthRead(status="ok", database="ok", timestamp=utc_now())


@app.post(
    "/api/v1/portfolios",
    response_model=PortfolioRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_portfolio",
    summary="Create a single-currency portfolio",
    response_description="The new portfolio and its reusable UUID",
    responses={
        409: {
            "model": ErrorResponse,
            "description": "`portfolio_name_exists`: the name is already in use.",
        }
    },
    tags=["portfolios"],
)
def add_portfolio(data: PortfolioCreate, session: SessionDep) -> Portfolio:
    """
    Create the isolation boundary for positions, cash, trades, and tags.

    Choose `USD` for US stocks and `*-USD` crypto, or `TWD` for `.TW`/`.TWO` stocks. The
    currency cannot be mixed later, so create another portfolio for a different currency.
    """
    return create_portfolio(session, data)


@app.get(
    "/api/v1/portfolios",
    response_model=list[PortfolioRead],
    operation_id="list_portfolios",
    summary="List all portfolios",
    response_description="Portfolios ordered by creation time",
    tags=["portfolios"],
)
def list_portfolios(session: SessionDep) -> list[Portfolio]:
    """Use this to discover portfolio IDs instead of guessing or creating duplicates."""
    return list(session.scalars(select(Portfolio).order_by(Portfolio.created_at)).all())


@app.get(
    "/api/v1/portfolios/{portfolio_id}",
    response_model=PortfolioRead,
    operation_id="get_portfolio",
    summary="Get portfolio metadata",
    response_description="Portfolio identity and base currency",
    responses={404: {"model": ErrorResponse, "description": "`portfolio_not_found`."}},
    tags=["portfolios"],
)
def read_portfolio(portfolio_id: PortfolioId, session: SessionDep) -> Portfolio:
    """Read the currency invariant before selecting a ticker for a trade."""
    return get_portfolio(session, portfolio_id)


@app.patch(
    "/api/v1/portfolios/{portfolio_id}",
    response_model=PortfolioRead,
    operation_id="update_portfolio",
    summary="Rename a portfolio or set its institution",
    response_description="The portfolio with its updated labels",
    responses={
        404: {"model": ErrorResponse, "description": "`portfolio_not_found`."},
        409: {
            "model": ErrorResponse,
            "description": "`portfolio_name_exists`: another portfolio already has that name.",
        },
    },
    tags=["portfolios"],
)
def patch_portfolio(
    portfolio_id: PortfolioId, data: PortfolioUpdate, session: SessionDep
) -> Portfolio:
    """
    Change what a portfolio is called, or record who holds it.

    Renaming is safe at any time: everything recorded points at the portfolio's ID, so no
    position, event, or snapshot is touched. The base currency and kind are deliberately not
    editable -- they are the terms every posted leg was recorded under, and changing one would
    reinterpret history rather than relabel it. Move the money to a new book instead.

    This works for cash and liability accounts too, which are portfolios of a different kind.
    """
    return update_portfolio(session, portfolio_id, data)


@app.delete(
    "/api/v1/portfolios/{portfolio_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_portfolio",
    summary="Delete a portfolio and all its data",
    responses={404: {"model": ErrorResponse, "description": "`portfolio_not_found`."}},
    tags=["portfolios"],
)
def remove_portfolio(portfolio_id: PortfolioId, session: SessionDep) -> None:
    """
    Permanently delete the portfolio and cascade-delete its positions, trades, cash
    transactions, and cash balance. This cannot be undone.
    """
    delete_portfolio(session, portfolio_id)


# --- Cash accounts ---------------------------------------------------------------------------


@app.post(
    "/api/v1/cash-accounts",
    response_model=PortfolioRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_cash_account",
    summary="Open a cash-only account",
    response_description="The new account and its reusable UUID",
    responses={
        409: {
            "model": ErrorResponse,
            "description": "`portfolio_name_exists`: the name is already in use.",
        }
    },
    tags=["cash-accounts"],
)
def add_cash_account(data: CashAccountCreate, session: SessionDep) -> Portfolio:
    """
    Track a bank balance, an e-wallet, or any pool of money held outside a broker.

    The account is a portfolio in every respect except one: it cannot hold a security, so any
    transaction naming a ticker is rejected. Deposits, withdrawals, interest, and fees post
    through `record_transaction` exactly as they do for an investment portfolio, and the balance
    appears in valuations, group summaries, and performance alongside everything else.

    Use `transfer_cash` rather than a withdrawal plus a deposit when money moves to another
    account you own: it records both sides as one event and cannot leave half the movement.
    """
    return create_cash_account(session, data)


@app.get(
    "/api/v1/cash-accounts",
    response_model=list[PortfolioRead],
    operation_id="list_cash_accounts",
    summary="List cash-only accounts",
    response_description="Cash accounts ordered by creation time",
    tags=["cash-accounts"],
)
def list_cash_accounts(session: SessionDep) -> list[Portfolio]:
    """The cash subset of `list_portfolios`, for totalling liquid assets held outside a broker."""
    return list(
        session.scalars(
            select(Portfolio)
            .where(Portfolio.kind == PortfolioKind.CASH.value)
            .order_by(Portfolio.created_at)
        ).all()
    )


@app.post(
    "/api/v1/liability-accounts",
    response_model=PortfolioRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_liability_account",
    summary="Open an account for money owed",
    response_description="The new account and its reusable UUID",
    responses={
        409: {
            "model": ErrorResponse,
            "description": "`portfolio_name_exists`: the name is already in use.",
        }
    },
    tags=["liability-accounts"],
)
def add_liability_account(data: LiabilityAccountCreate, session: SessionDep) -> Portfolio:
    """
    Track a loan, so net worth reflects what is owed and not only what is held.

    The account is a cash account with the sign reversed: its balance is the outstanding debt, so
    it runs negative and subtracts from every total it appears in. It cannot hold a security.

    Record the drawdown as a `transfer_cash` from this account to wherever the money landed, and
    each repayment as a transfer back. Interest charged is a `fee` posted through
    `record_transaction` -- not `interest`, which credits cash and is for interest received.

    Only the balance and the cash that moves are recorded here. The rate, the schedule, and the
    instalments remaining are not stored, and no figure in the API is derived from them.

    `get_portfolio_performance` returns no return figure for this account, by design: a rate of
    return divides a gain by the capital that produced it, and a debt is not capital at work.
    """
    return create_liability_account(session, data)


@app.get(
    "/api/v1/liability-accounts",
    response_model=list[PortfolioRead],
    operation_id="list_liability_accounts",
    summary="List liability accounts",
    response_description="Liability accounts ordered by creation time",
    tags=["liability-accounts"],
)
def list_liability_accounts(session: SessionDep) -> list[Portfolio]:
    """The debt subset of `list_portfolios`, for totalling what is owed across lenders."""
    return list(
        session.scalars(
            select(Portfolio)
            .where(Portfolio.kind == PortfolioKind.LIABILITY.value)
            .order_by(Portfolio.created_at)
        ).all()
    )


@app.get(
    "/api/v1/portfolios/{portfolio_id}/summary",
    response_model=PortfolioSummary,
    operation_id="get_portfolio_summary",
    summary="Value a portfolio and calculate allocation and P&L",
    response_description="Complete valuation using the latest available quotes",
    responses={
        404: {"model": ErrorResponse, "description": "`portfolio_not_found`."},
        503: {
            "model": ErrorResponse,
            "description": "`market_data_unavailable` and no cached quote can value a position.",
        },
    },
    tags=["portfolios"],
)
def portfolio_summary(portfolio_id: PortfolioId, session: SessionDep, market: MarketDep):
    """
    Return open positions, cash, total value, weights, and realized/unrealized P&L.

    Cash is included in the allocation denominator. Check each position's `price_stale` and the
    top-level `warnings` before using the valuation for a decision.
    """
    return build_summary(session, market, portfolio_id)


@app.get(
    "/api/v1/portfolios/{portfolio_id}/positions",
    response_model=PositionList,
    operation_id="list_positions",
    summary="List or filter open positions",
    response_description="Valued open positions that satisfy the tag filter",
    responses={
        404: {"model": ErrorResponse, "description": "`portfolio_not_found`."},
        503: {
            "model": ErrorResponse,
            "description": "A position cannot be valued because no quote is available.",
        },
    },
    tags=["positions"],
)
def list_positions(
    portfolio_id: PortfolioId,
    session: SessionDep,
    market: MarketDep,
    tags: Annotated[
        list[str] | None,
        Query(
            alias="tag",
            description="Repeat this parameter to filter by multiple normalized tags.",
            examples=["core"],
        ),
    ] = None,
    tag_mode: Annotated[
        TagMode,
        Query(
            description=(
                "`any` matches at least one supplied tag; `all` requires every supplied tag."
            )
        ),
    ] = TagMode.ANY,
) -> PositionList:
    """
    Read only open positions (`quantity > 0`) with current valuation and P&L.

    With no `tag`, all open positions are returned. Tag identity is portfolio-local and filters
    are normalized using the same rules as tag writes.
    """
    summary = build_summary(session, market, portfolio_id)
    items = summary.positions
    if tags:
        normalized = {normalize_tag(tag) for tag in tags}
        if tag_mode == TagMode.ALL:
            items = [item for item in items if normalized.issubset(set(item.tags))]
        else:
            items = [item for item in items if normalized.intersection(item.tags)]
    return PositionList(items=items, warnings=summary.warnings)


@app.put(
    "/api/v1/portfolios/{portfolio_id}/positions/{ticker}/tags",
    response_model=TagsRead,
    operation_id="replace_position_tags",
    summary="Replace all tags on an open position",
    response_description="The canonical replacement tag set",
    responses={
        404: {
            "model": ErrorResponse,
            "description": "`portfolio_not_found` or no open `position` for the ticker.",
        }
    },
    tags=["positions"],
)
def set_position_tags(
    portfolio_id: PortfolioId, ticker: TickerPath, data: TagsUpdate, session: SessionDep
) -> TagsRead:
    """
    Atomically set the complete desired tag collection; this is not an append operation.

    Read or remember existing tags before sending a partial set. Send an empty list intentionally
    to remove all tags. A position must have positive quantity.
    """
    normalized = replace_tags(session, portfolio_id, ticker, data.tags)
    return TagsRead(portfolio_id=portfolio_id, ticker=ticker.upper(), tags=normalized)


@app.get(
    "/api/v1/market/instruments/{ticker}",
    response_model=MarketInstrumentRead,
    operation_id="get_market_instrument",
    summary="Resolve a ticker and return quote and indicators",
    response_description="Canonical metadata, quote provenance, and fixed technical indicators",
    responses={
        503: {
            "model": ErrorResponse,
            "description": "`market_data_unavailable`: Yahoo failed and no cached quote exists.",
        }
    },
    tags=["market"],
)
def read_market_instrument(ticker: TickerPath, session: SessionDep, market: MarketDep):
    """
    Validate and canonicalize a ticker before recording a trade.

    Returns market metadata, latest OHLCV, daily change, 52-week range, market cap, SMA20/50,
    RSI14, and MACD. Inspect `currency` against the target portfolio and check `quote.stale`,
    `quote.provider_as_of`, `quote.fetched_at`, and `warnings` before relying on the price.
    """
    response = market_response(market.get(ticker))
    session.commit()
    return response


@app.get(
    "/api/v1/market/instruments/{ticker}/history",
    response_model=HistoryRead,
    operation_id="get_market_history",
    summary="Get reproducible OHLCV history through an inclusive end date",
    response_description="Chronological bars plus request and provider provenance",
    responses={
        503: {
            "model": ErrorResponse,
            "description": "`market_data_unavailable`: historical data could not be fetched.",
        }
    },
    tags=["market"],
)
def read_market_history(
    ticker: TickerPath,
    market: MarketDep,
    days: Annotated[
        int | None,
        Query(
            ge=30,
            le=730,
            description=(
                "Legacy bar lookback. Mutually exclusive with start_date/end_date; defaults to "
                "365 only when no period parameter is supplied."
            ),
        ),
    ] = None,
    start_date: Annotated[
        date | None,
        Query(description="Inclusive requested start date; mutually exclusive with days."),
    ] = None,
    end_date: Annotated[
        date | None,
        Query(
            description=(
                "Inclusive research cutoff. The Yahoo adapter adds one day for yfinance's "
                "exclusive end parameter and removes observations after this date."
            )
        ),
    ] = None,
    interval: Annotated[
        HistoryInterval,
        Query(description="Yahoo history interval: daily, weekly, or monthly."),
    ] = HistoryInterval.DAILY,
    adjustment: Annotated[
        HistoryAdjustment,
        Query(
            description=(
                "yfinance_auto_adjust adjusts OHLC for splits/dividends; unadjusted returns "
                "reported OHLC without automatic adjustment."
            )
        ),
    ] = HistoryAdjustment.AUTO,
) -> HistoryRead:
    """
    Return chronological OHLCV bars for agent-side research.

    `end_date` is inclusive at this API boundary. Bars reflect actual provider observations, not
    calendar-day placeholders. This endpoint fetches on demand and is not an execution-price source.
    """
    if days is not None and (start_date is not None or end_date is not None):
        raise DomainError(
            422,
            "validation_error",
            "days is mutually exclusive with start_date and end_date",
            {
                "days": days,
                "start_date": start_date.isoformat() if start_date else None,
                "end_date": end_date.isoformat() if end_date else None,
            },
        )
    if start_date is not None and end_date is not None and start_date > end_date:
        raise DomainError(
            422,
            "validation_error",
            "start_date must be on or before end_date",
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )
    default_days = 365 if start_date is None and end_date is None else None
    result = market.history(
        ticker,
        days=days if days is not None else default_days,
        start_date=start_date,
        end_date=end_date,
        interval=interval,
        adjustment=adjustment,
    )
    return HistoryRead(
        ticker=result.ticker,
        provider=result.provider,
        interval=result.interval,
        adjustment=result.adjustment,
        adjusted=result.adjustment == HistoryAdjustment.AUTO,
        requested_start_date=result.requested_start_date,
        requested_end_date=result.requested_end_date,
        actual_first_observation=result.bars[0].timestamp.date() if result.bars else None,
        actual_last_observation=result.bars[-1].timestamp.date() if result.bars else None,
        fetched_at=result.fetched_at,
        warnings=result.warnings,
        bars=[HistoryBarRead.model_validate(bar) for bar in result.bars],
    )


@app.get(
    "/api/v1/market/instruments/{ticker}/technical-snapshot",
    response_model=TechnicalSnapshotRead,
    operation_id="get_technical_snapshot",
    summary="Calculate a reproducible technical market snapshot",
    response_description="Technical state, provenance, optional benchmark and event analysis",
    responses={
        503: {
            "model": ErrorResponse,
            "description": (
                "`market_data_unavailable`: primary ticker history could not be fetched."
            ),
        }
    },
    tags=["market"],
)
def read_technical_snapshot(
    ticker: TickerPath,
    market: MarketDep,
    as_of: Annotated[
        date | None,
        Query(
            description=(
                "Inclusive research cutoff. All bars and calculations end on or before this date. "
                "Omit to use and return the provider's last valid observation."
            )
        ),
    ] = None,
    benchmark: Annotated[
        str | None,
        Query(description="Optional Yahoo ticker for common-date relative returns."),
    ] = None,
    event_date: Annotated[
        date | None,
        Query(
            description=(
                "Optional event date. The first observation on or after it becomes the anchor. "
                "Anchored VWAP is a daily OHLCV approximation using typical price."
            )
        ),
    ] = None,
    lookback_years: Annotated[
        int,
        Query(ge=1, le=10, description="Calendar-year history window used for calculations."),
    ] = 5,
) -> TechnicalSnapshotRead:
    """
    Research trend, momentum, volatility, volume, relative strength, and event price behavior.

    Pass a company-research report cutoff as `as_of`. Results use auto-adjusted Yahoo daily bars.
    Insufficient inputs leave individual metrics null and add warnings; interpret technical
    evidence together with fundamentals, valuation, and other market evidence.
    """
    return market.technical_snapshot(ticker, as_of, benchmark, event_date, lookback_years)


@app.post(
    "/api/v1/market/instruments/{ticker}/cache-clear",
    response_model=CacheClearRead,
    operation_id="clear_market_cache",
    summary="Drop cached price history for one instrument",
    response_description="How many cached entries were removed",
    tags=["market"],
)
def clear_market_cache(ticker: TickerPath, provider: ProviderDep) -> CacheClearRead:
    """
    Force the next price lookup for this ticker to go back to the provider.

    Cached daily bars are auto-adjusted, and a provider restates them after a split or dividend.
    The cache cannot detect that restatement, so recording a corporate action warns you to call
    this instead of guessing when a cached bar went out of date. Clearing is always safe: the
    cache holds nothing that cannot be fetched again.

    `cache_enabled` is false when no cache is configured, meaning the call did nothing.
    """
    symbol = ticker.strip().upper()
    if not isinstance(provider, CachingMarketProvider):
        return CacheClearRead(
            ticker=symbol,
            cleared_keys=0,
            cache_enabled=False,
            warnings=["No market cache is configured, so there was nothing to clear"],
        )
    return CacheClearRead(
        ticker=symbol, cleared_keys=provider.clear_ticker(symbol), cache_enabled=True
    )


@app.post(
    "/api/v1/portfolios/{portfolio_id}/transactions",
    response_model=JournalEventDetail,
    status_code=status.HTTP_201_CREATED,
    operation_id="record_transaction",
    summary="Post one balanced transaction and its cash effect atomically",
    response_description="The posted event with its legs and balance proof",
    responses={
        404: {"model": ErrorResponse, "description": "`portfolio_not_found`."},
        409: {
            "model": ErrorResponse,
            "description": "`idempotency_conflict`: the request_id was reused with other data.",
        },
    },
    tags=["journal"],
)
def post_transaction(
    portfolio_id: PortfolioId,
    data: TransactionCreate,
    session: SessionDep,
    market: MarketDep,
) -> JournalEventDetail:
    """
    Record a completed transaction as one atomic event; this never places an order.

    This posts the security, fee, tax, and settlement-cash legs together: either every leg and
    both projections commit, or none does, so a position never moves without its settlement.
    Fees and taxes on a trade capitalize into cost basis. Income is recorded gross with its
    withholding split out, and is never treated as an investor contribution.
    """
    if data.ticker:
        _resolve_or_fetch(session, market, data.ticker)
    event = record_transaction(
        session,
        portfolio_id,
        TransactionRequest(
            request_id=data.request_id,
            event_type=data.transaction_type,
            ticker=data.ticker,
            quantity=data.quantity,
            unit_price=data.unit_price,
            amount=data.amount,
            fee=data.fee,
            tax=data.tax,
            settlement_amount=data.settlement_amount,
            occurred_at=data.occurred_at,
            trade_date=data.trade_date,
            settlement_date=data.settlement_date,
            source_reference=data.source_reference,
            memo=data.memo,
        ),
    )
    return _event_detail_response(session, portfolio_id, event.id)


@app.post(
    "/api/v1/portfolios/{portfolio_id}/transactions/{event_id}/reversal",
    response_model=JournalEventDetail,
    status_code=status.HTTP_201_CREATED,
    operation_id="reverse_transaction",
    summary="Reverse a posted transaction without deleting it",
    response_description="The reversal event with its opposing legs",
    responses={
        404: {"model": ErrorResponse, "description": "`journal_event_not_found`."},
        409: {
            "model": ErrorResponse,
            "description": "`already_reversed` or `cannot_reverse_a_reversal`.",
        },
    },
    tags=["journal"],
)
def post_transaction_reversal(
    portfolio_id: PortfolioId,
    event_id: Annotated[str, Path(description="The posted event to undo.")],
    data: TransactionReverse,
    session: SessionDep,
) -> JournalEventDetail:
    """
    Undo a posted event by writing its mirror image, restoring the prior position and cash.

    The original event is never modified or deleted; it is marked reversed and linked to the new
    event, so both the entry and the fact that it was undone remain auditable. Correct a reversed
    event by posting a replacement, not by reversing again.
    """
    reversal = reverse_transaction(
        session, portfolio_id, event_id, request_id=data.request_id, memo=data.memo
    )
    return _event_detail_response(session, portfolio_id, reversal.id)


@app.get(
    "/api/v1/portfolios/{portfolio_id}/transactions",
    response_model=JournalEventPage,
    operation_id="list_journal_events",
    summary="Page the journal with audit filters",
    response_description="Matching events, newest first",
    responses={404: {"model": ErrorResponse, "description": "`portfolio_not_found`."}},
    tags=["journal"],
)
def read_journal_events(
    portfolio_id: PortfolioId,
    session: SessionDep,
    event_type: Annotated[
        str | None, Query(description="Filter to one transaction type.")
    ] = None,
    ticker: Annotated[
        str | None, Query(description="Only events with a leg in this instrument.")
    ] = None,
    source_reference: Annotated[
        str | None, Query(description="Exact broker confirmation or statement ID.")
    ] = None,
    start: Annotated[datetime | None, Query(description="Inclusive lower bound.")] = None,
    end: Annotated[datetime | None, Query(description="Inclusive upper bound.")] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    include_legs: Annotated[
        bool,
        Query(
            description=(
                "Return each event's legs inline, instead of one detail request per row. Legs "
                "for the whole page load in a single query."
            )
        ),
    ] = False,
) -> JournalEventPage:
    """
    Read the audit ledger. Reversals appear as their own events alongside what they reversed.
    """
    events, total = list_events(
        session,
        portfolio_id,
        event_type=event_type,
        instrument_reference=ticker,
        source_reference=source_reference,
        start=start,
        end=end,
        offset=offset,
        limit=limit,
    )
    # A reversal is classified by the event it undoes, resolved for the page in one query.
    reversed_types = reversed_types_for(session, events)
    items = [_journal_event_read(event, reversed_types.get(event.id)) for event in events]
    if include_legs:
        _attach_legs(session, events, items)
    return JournalEventPage(items=items, total=total, offset=offset, limit=limit)


def _attach_legs(session: Session, events: list, items: list[JournalEventRead]) -> None:
    """Attach every event's legs, at a cost that does not grow with the number of events."""
    legs_by_event = legs_for_events(session, [event.id for event in events])
    tickers = ticker_index(session)
    for event, item in zip(events, items, strict=True):
        item.legs = [
            _journal_leg_read(leg, tickers.get(leg.instrument_id))
            for leg in legs_by_event.get(event.id, [])
        ]


@app.get(
    "/api/v1/portfolios/{portfolio_id}/transactions/{event_id}",
    response_model=JournalEventDetail,
    operation_id="get_journal_event",
    summary="Read one event with its legs and balance proof",
    response_description="Event header, legs, balance validation, and reversal chain",
    responses={404: {"model": ErrorResponse, "description": "`journal_event_not_found`."}},
    tags=["journal"],
)
def read_journal_event(
    portfolio_id: PortfolioId,
    event_id: Annotated[str, Path(description="The event to read.")],
    session: SessionDep,
) -> JournalEventDetail:
    """
    Inspect exactly what an event did: every leg, the residual proving it balanced, whether it
    counts as an external flow, and its links to any reversal.
    """
    return _event_detail_response(session, portfolio_id, event_id)


def _event_detail_response(
    session: Session, portfolio_id: str, event_id: str
) -> JournalEventDetail:
    detail = event_detail(session, portfolio_id, event_id)
    report = detail["balance"]
    event_row = detail["event"]
    tickers = ticker_index(session)
    event_read = _journal_event_read(event_row)
    # event_detail already resolved the reversal chain; reuse it so the header and the detail's
    # own field cannot disagree about the same event.
    event_read.flow_classification = detail["flow_classification"]
    return JournalEventDetail(
        event=event_read,
        legs=[
            _journal_leg_read(leg, tickers.get(leg.instrument_id))
            for leg in detail["legs"]
        ],
        balance=(
            BalanceRead(
                balanced=report.balanced,
                residual=report.residual,
                functional_currency=report.functional_currency,
                leg_count=report.leg_count,
                warnings=report.warnings,
            )
            if report
            else None
        ),
        flow_classification=detail["flow_classification"],
        reverses_event_id=detail["reverses_event_id"],
        reversed_by_event_id=detail["reversed_by_event_id"],
    )


# --- Transfers -------------------------------------------------------------------------------


def _transfer_response(session: Session, transfer_id: str) -> TransferRead:
    """Assemble both halves of a transfer from the events carrying its id."""
    events = events_of(session, transfer_id)
    if not events:
        raise not_found("transfer", transfer_id)

    originals = [event for event in events if event.reverses_event_id is None]
    out_event = next(event for event in originals if event.transfer_role == "out")
    in_event = next(event for event in originals if event.transfer_role == "in")

    def side(event, role: str) -> TransferSideRead:
        cash = sum(
            (
                leg.amount_delta or Decimal("0")
                for leg in _legs_of(session, event.id)
                if leg.leg_type is LegType.CASH
            ),
            start=Decimal("0"),
        )
        return TransferSideRead(
            portfolio_id=event.portfolio_id,
            event_id=event.id,
            currency=event.functional_currency,
            amount=cash,
            role=role,
        )

    sent = side(out_event, "out")
    received = side(in_event, "in")
    rate = None
    if sent.currency != received.currency and sent.amount:
        rate = received.amount / -sent.amount

    return TransferRead(
        transfer_id=transfer_id,
        status=out_event.status,
        occurred_at=out_event.occurred_at,
        fx_rate=rate,
        sent=sent,
        received=received,
    )


@app.post(
    "/api/v1/transfers",
    response_model=TransferRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="transfer_cash",
    summary="Move cash between two portfolios",
    response_description="Both halves of the transfer and the rate between them",
    responses={
        404: {"model": ErrorResponse, "description": "`portfolio_not_found`."},
        409: {
            "model": ErrorResponse,
            "description": "`idempotency_conflict`: the request_id was reused with other data.",
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "`self_transfer`, `invalid_amount`, `insufficient_cash`, `fx_rate_required` "
                "when the currencies differ, or `unexpected_fx_rate` when they do not."
            ),
        },
    },
    tags=["transfers"],
)
def post_transfer(data: TransferCreate, session: SessionDep) -> TransferRead:
    """
    Record money moving between two accounts you own, as one indivisible event.

    Prefer this to a withdrawal in one portfolio and a deposit in the other. Those are two
    unlinked events: if the second fails, the money exists in neither account, and nothing in
    the journal says the two were ever related. A transfer writes both halves in one database
    transaction, so either both exist or neither does.

    When the two portfolios use different currencies, `fx_rate` is required and must be the rate
    you actually got. This service will not look one up: a market rate differs from an executed
    rate, and the difference would land in the ledger as cash from nowhere.

    Reverse it with `reverse_transfer`, never by reversing one side.
    """
    out_event, _ = transfer_cash(
        session,
        data.from_portfolio_id,
        data.to_portfolio_id,
        data.request_id,
        data.amount,
        fx_rate=data.fx_rate,
        occurred_at=data.occurred_at,
        source_reference=data.source_reference,
        memo=data.memo,
    )
    return _transfer_response(session, out_event.transfer_id)


@app.get(
    "/api/v1/transfers/{transfer_id}",
    response_model=TransferRead,
    operation_id="get_transfer",
    summary="Read both sides of a transfer",
    response_description="The sent and received halves with their conversion",
    responses={404: {"model": ErrorResponse, "description": "`transfer_not_found`."}},
    tags=["transfers"],
)
def read_transfer(transfer_id: str, session: SessionDep) -> TransferRead:
    """Audit one movement end to end, including the rate applied between two currencies."""
    return _transfer_response(session, transfer_id)


@app.post(
    "/api/v1/transfers/{transfer_id}/reversal",
    response_model=TransferRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="reverse_transfer",
    summary="Unwind both sides of a transfer",
    response_description="The transfer, now reversed",
    responses={
        404: {"model": ErrorResponse, "description": "`transfer_not_found`."},
        409: {
            "model": ErrorResponse,
            "description": "`already_reversed`: this transfer was undone already.",
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "`insufficient_cash`: the destination no longer holds the money to send back."
            ),
        },
    },
    tags=["transfers"],
)
def post_transfer_reversal(
    transfer_id: str, data: TransferReversalCreate, session: SessionDep
) -> TransferRead:
    """
    Undo both halves together, leaving the originals and their reversals in the journal.

    If the destination already spent the money this is refused rather than allowed to overdraw:
    move the cash back first. Reversing only one side is refused by `reverse_transaction`.
    """
    reverse_transfer(session, transfer_id, data.request_id, memo=data.memo)
    return _transfer_response(session, transfer_id)


@app.post(
    "/api/v1/corporate-actions",
    response_model=CorporateActionRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="record_corporate_action",
    summary="Record an announced corporate action",
    response_description="The stored action, including whether its cost basis is unresolved",
    tags=["corporate-actions"],
)
def post_corporate_action(
    data: CorporateActionCreate, session: SessionDep, market: MarketDep
) -> CorporateActionRead:
    """
    Store the facts of an announced action. This records only; it does not change any holding.

    Recording and applying are deliberately separate: an action is a fact about an instrument,
    while applying it changes a specific portfolio. Supply `cost_allocation_percent` only when the
    issuer disclosed it -- leaving it null marks the action cost-basis unresolved, which is
    reported honestly rather than filled with a guess that would corrupt later gain calculations.

    After recording a split or dividend, call `clear_market_cache` for the ticker. Cached daily
    bars are auto-adjusted, and the provider restates them once the action takes effect; the
    cache cannot detect that restatement, so it is dropped on request rather than on a guess.
    """
    _resolve_or_fetch(session, market, data.ticker)
    if data.new_ticker:
        _resolve_or_fetch(session, market, data.new_ticker)
    action = record_corporate_action(
        session,
        request_id=data.request_id,
        instrument_reference=data.ticker,
        action_type=data.action_type,
        ex_date=data.ex_date,
        source=data.source,
        ratio=data.ratio,
        cash_amount=data.cash_amount,
        currency=data.currency,
        withholding_tax=data.withholding_tax,
        new_instrument_reference=data.new_ticker,
        cost_allocation_percent=data.cost_allocation_percent,
        announcement_date=data.announcement_date,
        record_date=data.record_date,
        pay_date=data.pay_date,
        effective_at=data.effective_at,
        source_reference=data.source_reference,
    )
    return CorporateActionRead.model_validate(action)


@app.get(
    "/api/v1/corporate-actions",
    response_model=CorporateActionPage,
    operation_id="list_corporate_actions",
    summary="List recorded corporate actions",
    response_description="Matching actions, most recent ex-date first",
    tags=["corporate-actions"],
)
def read_corporate_actions(
    session: SessionDep,
    ticker: Annotated[str | None, Query(description="Filter to one instrument.")] = None,
    action_status: Annotated[
        str | None,
        Query(
            alias="status",
            description="announced, confirmed, applied, or cancelled.",
        ),
    ] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> CorporateActionPage:
    """List announced actions and their current status."""
    actions, total = list_actions(
        session,
        instrument_reference=ticker,
        status=action_status,
        offset=offset,
        limit=limit,
    )
    return CorporateActionPage(
        items=[CorporateActionRead.model_validate(action) for action in actions],
        total=total,
        offset=offset,
        limit=limit,
    )


@app.get(
    "/api/v1/portfolios/{portfolio_id}/corporate-actions/{action_id}/preview",
    response_model=CorporateActionPreview,
    operation_id="preview_corporate_action_application",
    summary="Preview an action's effect without applying it",
    response_description="Before and after values, legs, rounding, and unresolved questions",
    responses={
        404: {
            "model": ErrorResponse,
            "description": "`portfolio_not_found` or `corporate_action_not_found`.",
        }
    },
    tags=["corporate-actions"],
)
def read_corporate_action_preview(
    portfolio_id: PortfolioId,
    action_id: Annotated[str, Path(description="The recorded action to evaluate.")],
    session: SessionDep,
) -> CorporateActionPreview:
    """
    Compute exactly what applying an action would do, writing nothing.

    Inspect `applicable`, `warnings`, `fractional_handling`, and `cost_basis_unresolved` before
    applying. An action this service cannot compute a defensible basis for returns
    `applicable: false` with the reason, rather than an approximate result.
    """
    return _preview_response(preview_application(session, portfolio_id, action_id))


@app.post(
    "/api/v1/portfolios/{portfolio_id}/corporate-actions/{action_id}/apply",
    response_model=CorporateActionApplicationRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="apply_corporate_action",
    summary="Apply a recorded action to a portfolio",
    response_description="The application record with before and after values",
    responses={
        404: {
            "model": ErrorResponse,
            "description": "`portfolio_not_found` or `corporate_action_not_found`.",
        },
        422: {
            "model": ErrorResponse,
            "description": (
                "`action_not_applicable`: not held, already applied, or basis is unresolved."
            ),
        },
    },
    tags=["corporate-actions"],
)
def post_corporate_action_application(
    portfolio_id: PortfolioId,
    action_id: Annotated[str, Path(description="The recorded action to apply.")],
    data: CorporateActionApply,
    session: SessionDep,
) -> CorporateActionApplicationRead:
    """
    Apply an action atomically: the journal event and the holding change commit together.

    An action can be applied to a portfolio only once, so a repeated run cannot double-apply a
    split. Call the preview endpoint first to see the rounding and any unresolved treatment.
    """
    application = apply_corporate_action(
        session, portfolio_id, action_id, request_id=data.request_id
    )
    return CorporateActionApplicationRead.model_validate(application)


def _preview_response(preview) -> CorporateActionPreview:
    return CorporateActionPreview(
        portfolio_id=preview.portfolio_id,
        action_id=preview.action_id,
        action_type=preview.action_type,
        applicable=preview.applicable,
        original_quantity=preview.original_quantity,
        original_average_cost=preview.original_average_cost,
        resulting_quantity=preview.resulting_quantity,
        resulting_average_cost=preview.resulting_average_cost,
        cash_amount=preview.cash_amount,
        withholding_tax=preview.withholding_tax,
        cash_in_lieu=preview.cash_in_lieu,
        fractional_handling=preview.fractional_handling,
        cost_basis_unresolved=preview.cost_basis_unresolved,
        legs=[_journal_leg_read(leg) for leg in preview.legs],
        warnings=preview.warnings,
    )


InstrumentRef = Annotated[
    str,
    Path(
        description="Ticker, stable instrument_id, or a known provider alias.",
        examples=["GLD"],
    ),
]


def _resolve_or_fetch(session: Session, market: MarketService, reference: str) -> None:
    """Ensure an instrument exists locally, resolving a first-time ticker via the provider.

    Classifying or mapping a symbol the service has not traded yet is a normal first step, so all
    instrument routes accept an unseen ticker rather than forcing a lookup call first.
    """
    try:
        resolve_instrument(session, reference)
    except DomainError:
        market.get(reference)  # Persists identity, or raises market_data_unavailable.


@app.get(
    "/api/v1/instruments/{reference}/profile",
    response_model=InstrumentProfileRead,
    operation_id="get_instrument_profile",
    summary="Read stable identity, issuer, and classification provenance",
    response_description="Instrument identity with per-field classification sources",
    responses={
        404: {"model": ErrorResponse, "description": "`instrument_not_found` for the reference."}
    },
    tags=["instruments"],
)
def read_instrument_profile(reference: InstrumentRef, session: SessionDep, market: MarketDep):
    """
    Resolve an instrument and inspect how each classification field was decided.

    Every field reports the `provenance` that won it, so a provider guess is distinguishable from
    a verified mapping or a manual override. Fields that could not be resolved are absent and
    listed in `warnings` rather than being filled with an assumed value. An unknown ticker is
    resolved through the market provider first so a first-time symbol still returns a profile.
    """
    _resolve_or_fetch(session, market, reference)
    profile = build_instrument_profile(session, reference)
    session.commit()
    return profile


@app.put(
    "/api/v1/instruments/{reference}/classification",
    response_model=InstrumentProfileRead,
    operation_id="set_instrument_classification_override",
    summary="Manually correct one classification field",
    response_description="The instrument profile after applying the override",
    responses={
        404: {
            "model": ErrorResponse,
            "description": "`instrument_not_found`, or `classification_override_not_found`.",
        }
    },
    tags=["instruments"],
)
def put_instrument_classification(
    reference: InstrumentRef,
    data: ClassificationOverrideUpdate,
    session: SessionDep,
    market: MarketDep,
) -> InstrumentProfileRead:
    """
    Override one classification field when provider metadata is wrong or too coarse.

    The provider's own value is never modified or deleted; the override simply outranks it, and
    setting `retract` to true restores the provider-derived value. `reason` is retained for audit.
    """
    _resolve_or_fetch(session, market, reference)
    set_classification_override(
        session,
        reference,
        field=data.field,
        value=data.value,
        reason=data.reason,
        effective_at=data.effective_at,
        retract=data.retract,
    )
    profile = build_instrument_profile(session, reference)
    session.commit()
    return profile


@app.put(
    "/api/v1/instruments/{reference}/issuer",
    response_model=InstrumentProfileRead,
    operation_id="map_instrument_issuer",
    summary="Map an instrument to its issuing entity",
    response_description="The instrument profile after linking the issuer",
    responses={
        404: {
            "model": ErrorResponse,
            "description": "`instrument_not_found` or `issuer_not_found` for an explicit id.",
        }
    },
    tags=["instruments"],
)
def put_instrument_issuer(
    reference: InstrumentRef,
    data: IssuerMappingUpdate,
    session: SessionDep,
    market: MarketDep,
) -> InstrumentProfileRead:
    """
    Link a listing to the entity that issued it so cross-listing exposure can be aggregated.

    Separate listings of one company (an ADR and its local line) remain separate instruments with
    their own prices and currencies; only the issuer link is shared. Supplying an existing
    `issuer_id` attaches to that entity; otherwise one is matched or created by `legal_name`.
    """
    _resolve_or_fetch(session, market, reference)
    map_issuer(
        session,
        reference,
        legal_name=data.legal_name,
        display_name=data.display_name,
        country_of_domicile=data.country_of_domicile,
        lei=data.lei,
        issuer_id=data.issuer_id,
    )
    profile = build_instrument_profile(session, reference)
    session.commit()
    return profile


CALCULATION_METHOD = (
    "Holdings and cash are rebuilt by folding the journal to the valuation cutoff, then priced "
    "with the last close on or before that date. Prices after the cutoff are never used. "
    "Holdings without an available price are excluded from securities_value and reported in "
    "unpriced_market_value at cost, which makes the snapshot partial."
)


def _snapshot_payload(session: Session, snapshot) -> SnapshotRead:
    return SnapshotRead(
        id=snapshot.id,
        portfolio_id=snapshot.portfolio_id,
        valuation_date=snapshot.valuation_date.date(),
        valuation_as_of=snapshot.valuation_as_of,
        base_currency=snapshot.base_currency,
        securities_value=snapshot.securities_value,
        unpriced_market_value=snapshot.unpriced_market_value,
        cash_value=snapshot.cash_value,
        total_value=snapshot.total_value,
        cost_basis=snapshot.cost_basis,
        external_flow_amount=snapshot.external_flow_amount,
        income_amount=snapshot.income_amount,
        fee_amount=snapshot.fee_amount,
        tax_amount=snapshot.tax_amount,
        pricing_coverage_percent=snapshot.pricing_coverage_percent,
        positions_total=snapshot.positions_total,
        positions_priced=snapshot.positions_priced,
        calculation_version=snapshot.calculation_version,
        status=snapshot.status,
        calculation_method=CALCULATION_METHOD,
        warnings=snapshot_warnings(snapshot),
        positions=[
            PositionSnapshotRead(
                instrument_id=row.instrument_id,
                ticker_at_time=row.ticker_at_time,
                quantity=row.quantity,
                average_cost=row.average_cost,
                cost_basis=row.cost_basis,
                local_currency=row.local_currency,
                price=row.price,
                market_value=row.market_value,
                price_as_of=row.price_as_of,
                price_provider=row.price_provider,
                price_stale=row.price_stale,
                warnings=json.loads(row.warnings) if row.warnings else [],
            )
            for row in snapshot_positions(session, snapshot.id)
        ],
        created_at=snapshot.created_at,
    )


@app.post(
    "/api/v1/portfolios/{portfolio_id}/valuation-snapshots",
    response_model=SnapshotRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_valuation_snapshot",
    summary="Value a portfolio on one date",
    response_description="The stored snapshot with its per-holding pricing detail",
    tags=["valuation"],
)
def post_valuation_snapshot(
    portfolio_id: PortfolioId,
    data: SnapshotCreate,
    session: SessionDep,
    provider: ProviderDep,
) -> SnapshotRead:
    """
    Record what this portfolio was worth on a date, using only data available then.

    Positions and cash are rebuilt from the journal rather than read from current balances, and
    prices come from history bounded by the valuation date, so a backfilled series cannot borrow
    knowledge from later trading. Repeating the call for a date returns the stored snapshot;
    pass `force_revision` to replace it deliberately.

    A holding with no price on or before the date is excluded from `securities_value`, carried at
    cost in `unpriced_market_value`, and makes `status` partial. Check `status` and `warnings`
    before treating the total as authoritative.
    """
    snapshot = create_snapshot(
        session,
        portfolio_id,
        data.valuation_date,
        provider,
        force_revision=data.force_revision,
    )
    return _snapshot_payload(session, snapshot)


@app.post(
    "/api/v1/portfolios/{portfolio_id}/valuation-snapshots/rebuild",
    response_model=RebuildRead,
    status_code=status.HTTP_200_OK,
    operation_id="rebuild_valuation_snapshots",
    summary="Build snapshots across a date range",
    response_description="Counts of what was created, skipped, partial, and failed",
    tags=["valuation"],
)
def post_rebuild_snapshots(
    portfolio_id: PortfolioId,
    data: RebuildRequest,
    session: SessionDep,
    provider: ProviderDep,
) -> RebuildRead:
    """
    Fill in a range of daily snapshots as a bounded, re-runnable job.

    Dates that already have a snapshot are skipped, so an interrupted run is recovered by simply
    repeating it. A date that fails is recorded in `failed` and does not abandon the rest of the
    range. Each instrument's history is fetched once for the whole range rather than once per day.
    """
    report = rebuild_snapshots(
        session,
        portfolio_id,
        data.start_date,
        data.end_date,
        provider,
        force_revision=data.force_revision,
    )
    return RebuildRead(
        portfolio_id=report.portfolio_id,
        start_date=report.start_date,
        end_date=report.end_date,
        calculation_version=report.calculation_version,
        created=report.created,
        skipped_existing=report.skipped_existing,
        partial=report.partial,
        failed=report.failed,
        warnings=report.warnings,
    )


@app.get(
    "/api/v1/portfolios/{portfolio_id}/nav-history",
    response_model=NavHistoryRead,
    operation_id="get_nav_history",
    summary="Read the daily value series",
    response_description="Stored snapshots in the range, with the dates that have none",
    tags=["valuation"],
)
def get_nav_history(
    portfolio_id: PortfolioId,
    session: SessionDep,
    start_date: Annotated[date, Query(description="First valuation date, inclusive.")],
    end_date: Annotated[date, Query(description="Last valuation date, inclusive.")],
) -> NavHistoryRead:
    """
    Return the stored daily series for a range.

    This reads snapshots; it does not create them. Dates without a snapshot are listed in
    `missing_dates` rather than interpolated, because a filled-in value would be
    indistinguishable from one that was actually computed. Build them with
    `rebuild_valuation_snapshots` first if the series needs to be complete.
    """
    portfolio = get_portfolio(session, portfolio_id)
    if start_date > end_date:
        raise DomainError(
            422,
            "invalid_date_range",
            "start_date must not be after end_date",
            {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()},
        )

    snapshots = list_snapshots(session, portfolio_id, start_date, end_date)
    absent = missing_dates(snapshots, start_date, end_date)
    partial = [item for item in snapshots if item.status == SnapshotStatus.PARTIAL]

    warnings: list[str] = []
    if absent:
        warnings.append(
            f"{len(absent)} dates in this range have no snapshot and are reported as missing "
            "rather than interpolated"
        )
    if partial:
        warnings.append(
            f"{len(partial)} snapshots are partial because at least one holding could not be "
            "priced on that date"
        )
    return NavHistoryRead(
        portfolio_id=portfolio_id,
        base_currency=portfolio.base_currency,
        start_date=start_date,
        end_date=end_date,
        calculation_version=CALCULATION_VERSION,
        calculation_method=CALCULATION_METHOD,
        snapshots=[
            SnapshotSummary(
                id=item.id,
                valuation_date=item.valuation_date.date(),
                valuation_as_of=item.valuation_as_of,
                securities_value=item.securities_value,
                unpriced_market_value=item.unpriced_market_value,
                cash_value=item.cash_value,
                total_value=item.total_value,
                external_flow_amount=item.external_flow_amount,
                pricing_coverage_percent=item.pricing_coverage_percent,
                status=item.status,
            )
            for item in snapshots
        ],
        missing_dates=absent,
        partial_snapshots=len(partial),
        warnings=warnings,
    )


@app.get(
    "/api/v1/portfolios/{portfolio_id}/performance",
    response_model=PerformanceRead,
    operation_id="get_portfolio_performance",
    summary="Measure return over a period",
    response_description="TWR, XIRR, flow breakdown, method, and data coverage",
    tags=["valuation"],
)
def get_portfolio_performance(
    portfolio_id: PortfolioId,
    session: SessionDep,
    start_date: Annotated[date, Query(description="First valuation date, inclusive.")],
    end_date: Annotated[date, Query(description="Last valuation date, inclusive.")],
    include_daily: Annotated[
        bool, Query(description="Return the per-day series as well as the period totals.")
    ] = False,
) -> PerformanceRead:
    """
    Measure how a portfolio performed between two dates.

    Two returns are reported because they answer different questions. `twr_percent` removes the
    effect of money arriving and leaving, so it reflects the holdings and is what you compare
    against a benchmark. `xirr_percent` keeps that effect, so it reflects what the investor
    earned on the capital they actually had at risk. A large deposit landing before a rally
    lifts XIRR above TWR, and neither figure is wrong.

    This reads stored snapshots; it does not build them. Both ends of the period need a
    snapshot, and gaps in between are reported in `coverage.missing_dates` rather than
    interpolated. Check `coverage.is_reliable` before quoting a number: it is false when the
    series has gaps, contains partial snapshots, or the portfolio still has migrated cash events
    awaiting a ruling, any of which biases the result in a direction this service cannot correct.
    """
    result = calculate_performance(
        session, portfolio_id, start_date, end_date, include_daily=include_daily
    )
    return PerformanceRead(
        portfolio_id=result.portfolio_id,
        base_currency=result.base_currency,
        start_date=result.start_date,
        end_date=result.end_date,
        beginning_value=result.beginning_value,
        ending_value=result.ending_value,
        external_inflows=result.external_inflows,
        external_outflows=result.external_outflows,
        income=result.income,
        fees=result.fees,
        taxes=result.taxes,
        twr_percent=result.twr_percent,
        annualized_twr_percent=result.annualized_twr_percent,
        xirr_percent=result.xirr_percent,
        xirr_unavailable_reason=result.xirr_unavailable_reason,
        twr_method=result.twr_method,
        twr_method_description=TWR_METHOD_DESCRIPTION,
        xirr_method=result.xirr_method,
        xirr_method_description=XIRR_METHOD_DESCRIPTION,
        calculation_version=result.calculation_version,
        coverage=PerformanceCoverageRead(
            snapshots_used=result.coverage.snapshots_used,
            missing_dates=result.coverage.missing_dates,
            partial_snapshots=result.coverage.partial_snapshots,
            unclassified_flow_events=result.coverage.unclassified_flow_events,
            is_reliable=result.coverage.is_reliable,
            warnings=result.coverage.warnings,
        ),
        daily_returns=[
            DailyReturnRead(
                valuation_date=item.valuation_date,
                beginning_value=item.beginning_value,
                ending_value=item.ending_value,
                external_flow=item.external_flow,
                return_percent=item.return_percent,
            )
            for item in result.daily_returns
        ],
    )


def _journal_leg_read(leg, ticker: str | None = None) -> JournalLegRead:
    """Serialize one leg. `ticker` is resolved by the caller, which holds the whole page's index."""
    return JournalLegRead(
        leg_type=leg.leg_type.value,
        account_role=leg.account_role,
        currency=leg.currency,
        instrument_id=leg.instrument_id,
        quantity_delta=leg.quantity_delta,
        amount_delta=leg.amount_delta,
        unit_price=leg.unit_price,
        fx_rate=leg.fx_rate,
        metadata=leg.metadata,
        ticker=ticker,
    )


# Fields the ORM row does not carry: one is derived from the event type, the other is attached
# by the caller only when legs were requested.
_NOT_ON_EVENT_ROW = frozenset({"flow_classification", "legs"})


def _journal_event_read(event, reversed_type: str | None = None) -> JournalEventRead:
    """A journal event with its flow classification.

    `reversed_type` is the type of the event this one reverses; without it every reversal reports
    `unknown`, which reads as a data-quality problem nobody can act on.
    """
    return JournalEventRead(
        **{
            field: getattr(event, field)
            for field in JournalEventRead.model_fields
            if field not in _NOT_ON_EVENT_ROW
        },
        flow_classification=derived_flow(event.event_type, reversed_type),
    )


def _group_payload(session: Session, group) -> GroupRead:
    return GroupRead(
        id=group.id,
        name=group.name,
        reporting_currency=group.reporting_currency,
        portfolio_ids=member_portfolio_ids(session, group.id, utc_now().date()),
        created_at=group.created_at,
        updated_at=group.updated_at,
    )


@app.post(
    "/api/v1/portfolio-groups",
    response_model=GroupRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="create_portfolio_group",
    summary="Group portfolios for combined reporting",
    response_description="The group and its current members",
    tags=["consolidation"],
)
def post_portfolio_group(data: GroupCreate, session: SessionDep) -> GroupRead:
    """
    Report several portfolios together in one currency.

    Grouping is a reporting decision about data that already exists, so membership starts at each
    portfolio's own inception: a group created today can immediately report on the history its
    portfolios already have. Portfolios keep their own base currency and stay independent
    ledgers; nothing about them changes.
    """
    group = create_group(session, data.name, data.reporting_currency, data.portfolio_ids)
    return _group_payload(session, group)


@app.get(
    "/api/v1/portfolio-groups",
    response_model=list[GroupRead],
    operation_id="list_portfolio_groups",
    summary="List every portfolio group",
    response_description="All groups with their current members",
    tags=["consolidation"],
)
def list_portfolio_groups(session: SessionDep) -> list[GroupRead]:
    """List the groups that exist, so a client can offer them without knowing an ID."""
    groups = session.scalars(select(PortfolioGroup).order_by(PortfolioGroup.created_at)).all()
    return [_group_payload(session, group) for group in groups]


@app.get(
    "/api/v1/portfolio-groups/{group_id}",
    response_model=GroupRead,
    operation_id="get_portfolio_group",
    summary="Read a group and its members",
    response_description="The group with the portfolios currently in it",
    tags=["consolidation"],
)
def read_portfolio_group(group_id: GroupId, session: SessionDep) -> GroupRead:
    """Read a group's metadata and the portfolios that are members today."""
    return _group_payload(session, get_group(session, group_id))


@app.patch(
    "/api/v1/portfolio-groups/{group_id}",
    response_model=GroupRead,
    operation_id="update_portfolio_group",
    summary="Rename a portfolio group",
    response_description="The group with its new name and current members",
    responses={404: {"model": ErrorResponse, "description": "`portfolio_group_not_found`."}},
    tags=["consolidation"],
)
def patch_portfolio_group(
    group_id: GroupId, data: GroupUpdate, session: SessionDep
) -> GroupRead:
    """
    Change what a group is called.

    A group is only a reporting lens, so its name carries no accounting meaning and a rename
    touches nothing else: membership intervals, the portfolios themselves, and every past
    consolidation are unaffected. The reporting currency is not editable -- stored totals were
    converted into it, so changing it would reinterpret them. Create another group instead.

    To change which portfolios are in the group, use `update_portfolio_group_members`.
    """
    group = rename_group(session, group_id, data.name)
    return _group_payload(session, group)


@app.delete(
    "/api/v1/portfolio-groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    operation_id="delete_portfolio_group",
    summary="Delete a portfolio group",
    responses={404: {"model": ErrorResponse, "description": "`portfolio_group_not_found`."}},
    tags=["consolidation"],
)
def remove_portfolio_group(group_id: GroupId, session: SessionDep) -> None:
    """
    Delete the group and its membership intervals.

    The portfolios themselves are untouched, along with their journals, snapshots, and every
    other record: a group is only a lens for reporting them together, so removing it destroys
    no facts and the same group can be recreated with the same members. To stop reporting one
    portfolio while keeping the group, use `update_portfolio_group_members` instead, which
    closes that membership rather than deleting the group.
    """
    delete_group(session, group_id)


@app.put(
    "/api/v1/portfolio-groups/{group_id}/members",
    response_model=GroupRead,
    operation_id="update_portfolio_group_members",
    summary="Replace a group's membership",
    response_description="The group with its updated membership",
    tags=["consolidation"],
)
def put_portfolio_group_members(
    group_id: GroupId, data: GroupMembersUpdate, session: SessionDep
) -> GroupRead:
    """
    Set which portfolios belong to the group from now on.

    A portfolio removed here keeps its historical membership: its interval is closed rather than
    deleted, so a report for an earlier date still contains it. Membership can never be edited in
    a way that silently restates a past consolidation.
    """
    group = replace_members(session, group_id, data.portfolio_ids)
    return _group_payload(session, group)


@app.get(
    "/api/v1/portfolio-groups/{group_id}/summary",
    response_model=ConsolidatedSummaryRead,
    operation_id="get_consolidated_summary",
    summary="Value a group in one currency",
    response_description="Converted totals with the rates used and the coverage achieved",
    tags=["consolidation"],
)
def get_consolidated_summary(
    group_id: GroupId,
    session: SessionDep,
    provider: ProviderDep,
    as_of: Annotated[
        date | None, Query(description="Report date. Omit for today. Never uses later data.")
    ] = None,
    reporting_currency: Annotated[
        str | None, Query(description="Override the group's own reporting currency.")
    ] = None,
) -> ConsolidatedSummaryRead:
    """
    Add up holdings and cash across currencies.

    Each portfolio is valued in its own currency and then converted at the rate in force on the
    report date; rates are never taken from after that date. Every position keeps both figures,
    plus the rate, the path it took, and that rate's own date, so any total can be checked.

    Read `converted_value_coverage_percent` and `unconverted` before using `total_value`. When a
    currency pair cannot be resolved, the affected value is excluded from the total and listed
    under `unconverted` rather than converted at a guessed rate -- so the total may legitimately
    understate the group, and only these fields reveal by how much.
    """
    summary = build_consolidated_summary(
        session,
        group_id,
        provider,
        as_of=as_of,
        reporting_currency=reporting_currency,
    )
    return ConsolidatedSummaryRead(
        group_id=summary.group_id,
        group_name=summary.group_name,
        reporting_currency=summary.reporting_currency,
        as_of=summary.as_of,
        portfolio_ids=summary.portfolio_ids,
        positions=[
            ConsolidatedPositionRead(
                portfolio_id=row.portfolio_id,
                portfolio_name=row.portfolio_name,
                instrument_id=row.instrument_id,
                ticker=row.ticker,
                issuer_id=row.issuer_id,
                quantity=row.quantity,
                average_cost=row.average_cost,
                local_currency=row.local_currency,
                local_price=row.local_price,
                local_market_value=row.local_market_value,
                reporting_market_value=row.reporting_market_value,
                fx_rate=row.fx_rate,
                fx_method=row.fx_method,
                fx_path=row.fx_path,
                fx_as_of=row.fx_as_of,
                weight_percent=row.weight_percent,
                asset_class=row.asset_class,
                asset_class_provenance=row.asset_class_provenance,
                warnings=row.warnings,
            )
            for row in summary.positions
        ],
        cash_by_currency=[
            CurrencyTotalRead(
                currency=item.currency,
                local_amount=item.local_amount,
                reporting_amount=item.reporting_amount,
            )
            for item in summary.cash_by_currency
        ],
        currency_exposure=[
            CurrencyTotalRead(
                currency=item.currency,
                local_amount=item.local_amount,
                reporting_amount=item.reporting_amount,
            )
            for item in summary.currency_exposure
        ],
        issuer_exposure=[
            IssuerExposureRead(
                issuer_id=item.issuer_id,
                issuer_name=item.issuer_name,
                reporting_value=item.reporting_value,
                weight_percent=item.weight_percent,
                tickers=item.tickers,
            )
            for item in summary.issuer_exposure
        ],
        securities_value=summary.securities_value,
        cash_value=summary.cash_value,
        total_value=summary.total_value,
        assets_value=summary.assets_value,
        liabilities_value=summary.liabilities_value,
        net_value=summary.net_value,
        unconverted=[
            UnconvertedAmountRead(
                currency=item.currency, amount=item.amount, reason=item.reason
            )
            for item in summary.unconverted
        ],
        converted_value_coverage_percent=summary.converted_value_coverage_percent,
        fx_rates_used=[
            FxRateRead(
                base_currency=item.base_currency,
                quote_currency=item.quote_currency,
                rate=item.rate,
                method=item.method,
                conversion_path=item.conversion_path,
                price_as_of=item.price_as_of,
                provider=item.provider,
                is_stale=item.is_stale,
                warnings=item.warnings,
            )
            for item in summary.fx_rates_used
        ],
        calculation_method=summary.calculation_method,
        warnings=summary.warnings,
    )


# The dashboard, built from `frontend/` by Vite. It mounts last, and at the root, because a mount
# matches by prefix and would shadow every route declared after it -- so this must stay the final
# statement in the module.
#
# Its asset paths are relative (`./assets/...`) and resolve against the document's own URL, so one
# build serves the domain root and a prefix-stripping proxy alike. Never rewrite them to absolute
# paths: that breaks whichever deployment it was not written for. See docs/ARCHITECTURE.md,
# "Sub-path deployment".
#
# `html=True` serves index.html for the root. The directory is absent until the frontend is built,
# and mounting a missing one crashes the app at import time -- so an API-only build, and a
# checkout that has never run `bun run build`, still serve every endpoint.
if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="dashboard")
