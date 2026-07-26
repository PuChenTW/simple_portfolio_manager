from datetime import date
from pathlib import Path as FilePath
from typing import Annotated

from fastapi import Depends, FastAPI, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from .config import settings
from .db import get_session
from .errors import DomainError
from .market import HistoryAdjustment, HistoryInterval, MarketProvider, YahooMarketProvider
from .models import CashTransaction, Portfolio, Trade
from .schemas import (
    CashTransactionCreate,
    CashTransactionPage,
    CashTransactionRead,
    ErrorResponse,
    HealthRead,
    HistoryBarRead,
    HistoryRead,
    MarketInstrumentRead,
    PortfolioCreate,
    PortfolioRead,
    PortfolioSummary,
    PositionList,
    TagMode,
    TagsRead,
    TagsUpdate,
    TechnicalSnapshotRead,
    TradeCreate,
    TradePage,
    TradeRead,
    utc_now,
)
from .services import (
    MarketService,
    build_summary,
    create_cash_transaction,
    create_portfolio,
    create_trade,
    delete_portfolio,
    get_portfolio,
    market_response,
    normalize_tag,
    page_total,
    replace_tags,
)

API_DESCRIPTION = """
Local, single-user portfolio accounting API designed for autonomous agents and generated tools.

## Agent contract

- **This records completed transactions; it never places market orders.**
- Every portfolio has exactly one `base_currency`. A trade is rejected unless the instrument's
  quote currency matches it. Use separate portfolios for USD and TWD assets.
- Asset trades and cash are independent ledgers. Recording a buy does **not** deduct cash, and
  recording a sell does **not** deposit proceeds. Record cash events separately when needed.
- Send decimal values as JSON strings for exact input. Decimal values in responses are strings.
- All timestamps are UTC RFC 3339 values. Omitted transaction times default to server time.
- Mutation requests require a client-generated `request_id`. Retrying the same body with the same
  ID is safe; reusing an ID for different data returns `409 idempotency_conflict`.
- Spot positions cannot become negative and cash cannot be overdrawn.

## Recommended workflow

1. Call `create_portfolio` once and retain its `id`.
2. Optionally call `record_cash_transaction` to establish cash.
3. Call `get_market_instrument` to validate a ticker and inspect its currency and quote timestamp.
4. Call `record_trade` with the actual execution price, quantity, and fee.
5. Call `replace_position_tags` to attach strategy context.
6. Call `get_portfolio_summary` for valuation, allocation, and P&L, or `list_positions` to filter
   holdings by tags.

## Market-data reliability

Quotes are cached for five minutes by default. If Yahoo refresh fails and an older quote exists,
the API returns it with `stale=true` and a warning. A `503 market_data_unavailable` means no usable
cached data exists. Do not treat this API as an execution venue or real-time market-data feed.
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
        "name": "trades",
        "description": (
            "Record immutable executed spot trades. Trades update quantity, average cost, and P&L "
            "but never change cash."
        ),
    },
    {
        "name": "cash",
        "description": "Record deposits and withdrawals in a portfolio's base currency.",
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
        "Never describe record_trade as placing or executing an order.",
        "Keep every portfolio single-currency and verify ticker currency before a trade.",
        "Treat trades and cash as independent ledgers; update both only when explicitly intended.",
        "Generate one unique request_id per logical mutation and reuse it only for exact retries.",
        "Check stale, provider_as_of, fetched_at, and warnings before using a market price.",
        "Send exact quantities and monetary values as decimal strings.",
        "Branch on the error code and retry only transient market_data_unavailable failures.",
        "For company research, pass the report cutoff as technical-snapshot as_of.",
        "Inspect provider, actual as-of, adjustment, and warnings before interpreting indicators.",
    ],
    "workflow": [
        "list_portfolios or create_portfolio",
        "get_market_instrument",
        "get_market_history or get_technical_snapshot for market research",
        "record_trade and optionally record_cash_transaction",
        "replace_position_tags",
        "get_portfolio_summary or list_positions",
    ],
}

app = FastAPI(
    title="Local Portfolio Manager",
    version="0.1.0",
    summary="Agent-friendly accounting for cash, stocks, and crypto portfolios",
    description=API_DESCRIPTION,
    openapi_tags=OPENAPI_TAGS,
    servers=[{"url": "/", "description": "Default local server"}],
    responses=GLOBAL_RESPONSES,
)

STATIC_DIR = FilePath(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_fastapi_openapi = app.openapi


def agent_friendly_openapi():
    schema = _fastapi_openapi()
    schema["x-agent-skill"] = AGENT_SKILL_METADATA
    return schema


app.openapi = agent_friendly_openapi


@app.get("/", include_in_schema=False)
def dashboard() -> FileResponse:
    """Serve the local, read-only portfolio dashboard."""
    return FileResponse(STATIC_DIR / "index.html")


_market_provider = YahooMarketProvider()


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


@app.post(
    "/api/v1/portfolios/{portfolio_id}/trades",
    response_model=TradeRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="record_trade",
    summary="Record an executed buy or sell",
    response_description="The immutable trade ledger entry",
    responses={
        404: {"model": ErrorResponse, "description": "`portfolio_not_found`."},
        409: {"model": ErrorResponse, "description": "`idempotency_conflict`."},
        503: {
            "model": ErrorResponse,
            "description": "Ticker metadata is unavailable and not cached.",
        },
    },
    tags=["trades"],
)
def add_trade(
    portfolio_id: PortfolioId, data: TradeCreate, session: SessionDep, market: MarketDep
) -> Trade:
    """
    Record a completed spot transaction and update the position in O(1).

    Provide the actual execution price rather than the quote returned by the market endpoint.
    Buys recalculate moving-average cost; sells realize P&L and cannot exceed current quantity.
    This operation never changes cash. Retry the exact request with the same `request_id`.
    """
    return create_trade(session, market, portfolio_id, data)


@app.get(
    "/api/v1/portfolios/{portfolio_id}/trades",
    response_model=TradePage,
    operation_id="list_trades",
    summary="List the trade ledger",
    response_description="A reverse-chronological page of immutable trades",
    responses={404: {"model": ErrorResponse, "description": "`portfolio_not_found`."}},
    tags=["trades"],
)
def list_trades(
    portfolio_id: PortfolioId,
    session: SessionDep,
    offset: Annotated[int, Query(ge=0, description="Zero-based number of entries to skip.")] = 0,
    limit: Annotated[
        int, Query(ge=1, le=200, description="Maximum entries to return, from 1 to 200.")
    ] = 50,
) -> TradePage:
    """Audit recorded executions. This endpoint does not return live valuation data."""
    get_portfolio(session, portfolio_id)
    items = session.scalars(
        select(Trade)
        .where(Trade.portfolio_id == portfolio_id)
        .order_by(Trade.executed_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return TradePage(
        items=[TradeRead.model_validate(item) for item in items],
        offset=offset,
        limit=limit,
        total=page_total(session, Trade, portfolio_id),
    )


@app.post(
    "/api/v1/portfolios/{portfolio_id}/cash-transactions",
    response_model=CashTransactionRead,
    status_code=status.HTTP_201_CREATED,
    operation_id="record_cash_transaction",
    summary="Record a cash deposit or withdrawal",
    response_description="The immutable cash ledger entry",
    responses={
        404: {"model": ErrorResponse, "description": "`portfolio_not_found`."},
        409: {"model": ErrorResponse, "description": "`idempotency_conflict`."},
    },
    tags=["cash"],
)
def add_cash_transaction(
    portfolio_id: PortfolioId, data: CashTransactionCreate, session: SessionDep
) -> CashTransaction:
    """
    Adjust cash in the portfolio's base currency without touching asset positions.

    Withdrawals cannot exceed available cash. Asset trades never call this operation implicitly,
    so record settlement cash separately only when that matches the source account.
    """
    return create_cash_transaction(session, portfolio_id, data)


@app.get(
    "/api/v1/portfolios/{portfolio_id}/cash-transactions",
    response_model=CashTransactionPage,
    operation_id="list_cash_transactions",
    summary="List the cash ledger",
    response_description="A reverse-chronological page of cash events",
    responses={404: {"model": ErrorResponse, "description": "`portfolio_not_found`."}},
    tags=["cash"],
)
def list_cash_transactions(
    portfolio_id: PortfolioId,
    session: SessionDep,
    offset: Annotated[int, Query(ge=0, description="Zero-based number of entries to skip.")] = 0,
    limit: Annotated[
        int, Query(ge=1, le=200, description="Maximum entries to return, from 1 to 200.")
    ] = 50,
) -> CashTransactionPage:
    """Audit deposits and withdrawals. Current cash is available from the portfolio summary."""
    get_portfolio(session, portfolio_id)
    items = session.scalars(
        select(CashTransaction)
        .where(CashTransaction.portfolio_id == portfolio_id)
        .order_by(CashTransaction.occurred_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return CashTransactionPage(
        items=[CashTransactionRead.model_validate(item) for item in items],
        offset=offset,
        limit=limit,
        total=page_total(session, CashTransaction, portfolio_id),
    )


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
