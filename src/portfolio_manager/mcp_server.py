"""MCP server exposing the Portfolio Manager API as tools.

This is a thin client over the existing FastAPI service. Each tool maps 1:1 to a REST
endpoint (its `operation_id`) and forwards to `PORTFOLIO_API_BASE_URL`. Business rules,
idempotency, and the `{code, message, details}` error envelope stay in the HTTP API; this
layer only translates. Run it over stdio (default) or Streamable HTTP.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from .taxonomy import (
    CLASSIFICATION_FIELDS,
    PROVENANCE_RANK,
    AssetClass,
    SecurityType,
)

DEFAULT_BASE_URL = "http://127.0.0.1:8001"

# Every code the API can return, so an agent can branch on all of them rather than discovering
# them by hitting each one. Kept beside the tools it documents; `test_mcp_server.py` asserts the
# list stays complete, since a code that exists but is undocumented is worse than none.
ERROR_CODES = {
    "action_not_applicable": "The corporate action does not apply to this holding.",
    "already_reversed": "The event was reversed already; reversals are not repeatable.",
    "cannot_reverse_a_reversal": "Reverse the original event, not the reversal of it.",
    "currency_mismatch": "The instrument is not quoted in the portfolio's base currency.",
    "empty_group": "A portfolio group needs at least one member.",
    "empty_journal_event": "A journal event must have at least one leg.",
    "fx_rate_required": "A cross-currency transfer needs the rate actually executed.",
    "idempotency_conflict": "The request_id was reused with different data.",
    "insufficient_cash": "The withdrawal or purchase exceeds available cash.",
    "insufficient_position": "The sale exceeds the quantity currently held.",
    "invalid_amount": "amount must be positive; direction comes from the transaction type.",
    "invalid_classification_value": "Not a member of the taxonomy; see portfolio://taxonomy.",
    "invalid_date_range": "start_date is after end_date, or the range is unusable.",
    "invalid_tag": "The tag is empty or longer than 50 characters.",
    "journal_out_of_balance": "The legs do not net to zero; a position cannot come from nothing.",
    "market_data_unavailable": "No usable quote or history exists, cached or live.",
    "missing_field": "A field this transaction type requires was omitted.",
    "not_a_securities_account": "This is a cash account; it cannot hold securities.",
    "portfolio_name_exists": "Another portfolio already uses that name.",
    "reverse_the_transfer_instead": "Reverse the whole transfer so both sides unwind together.",
    "self_transfer": "The source and destination portfolios are the same.",
    "transfer_not_found": "No transfer exists with that id.",
    "unexpected_fx_rate": "Both portfolios share a currency, so there is no rate to apply.",
    "unknown_classification_field": "Not a classifiable field; see portfolio://taxonomy.",
    "unsupported_event_type": "That event type cannot be recorded this way.",
    "validation_error": "The request body failed schema validation.",
    "valuation_date_in_future": "A portfolio cannot be valued on a date that has not happened.",
}

# Passed to the FastMCP lifespan so the shared client lives exactly as long as the server.
_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(_server: FastMCP) -> AsyncIterator[None]:
    global _client
    base_url = os.getenv("PORTFOLIO_API_BASE_URL", DEFAULT_BASE_URL)
    async with httpx.AsyncClient(base_url=base_url, timeout=30.0) as client:
        _client = client
        try:
            yield
        finally:
            _client = None


mcp = FastMCP("local-portfolio-manager", lifespan=lifespan)


class ApiError(Exception):
    """Carries the API's machine-readable error envelope to the MCP client."""

    def __init__(self, status_code: int, code: str, message: str, details: dict[str, Any]) -> None:
        super().__init__(f"{code}: {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def _client_or_raise() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError("HTTP client is not initialized; the MCP lifespan is not active")
    return _client


async def _request(method: str, path: str, **kwargs: Any) -> Any:
    """Send a request and return parsed JSON, translating errors to ApiError.

    A non-2xx response reuses the API's `{code, message, details}` body verbatim so agents
    keep branching on `code`. A failed connection (API server not running) surfaces as a
    clear `api_unreachable` error rather than a raw httpx exception.
    """
    client = _client_or_raise()
    try:
        response = await client.request(method, path, **kwargs)
    except httpx.RequestError as exc:
        raise ApiError(
            503,
            "api_unreachable",
            f"Could not reach the Portfolio Manager API at {client.base_url}. Is it running?",
            {"error": str(exc)},
        ) from exc
    if response.is_success:
        if response.status_code == 204:
            return None
        return response.json()
    try:
        body = response.json()
    except ValueError:
        body = {}
    raise ApiError(
        response.status_code,
        body.get("code", "http_error"),
        body.get("message", response.reason_phrase),
        body.get("details", {}),
    )


# --- System -----------------------------------------------------------------


@mcp.tool()
async def get_health() -> dict[str, Any]:
    """Check API and database availability before a longer workflow when it is uncertain."""
    return await _request("GET", "/health")


# --- Portfolios -------------------------------------------------------------


@mcp.tool()
async def create_portfolio(name: str, base_currency: str) -> dict[str, Any]:
    """Create an isolated, single-currency portfolio and return its reusable UUID.

    Use USD for US stocks and *-USD crypto, or TWD for .TW/.TWO stocks. The currency cannot
    be mixed later, so create another portfolio for a different currency.
    """
    return await _request(
        "POST", "/api/v1/portfolios", json={"name": name, "base_currency": base_currency}
    )


@mcp.tool()
async def list_portfolios() -> list[dict[str, Any]]:
    """List all portfolios so you can discover IDs instead of guessing or creating duplicates."""
    return await _request("GET", "/api/v1/portfolios")


@mcp.tool()
async def get_portfolio(portfolio_id: str) -> dict[str, Any]:
    """Get portfolio metadata; read the currency invariant before choosing a ticker to trade."""
    return await _request("GET", f"/api/v1/portfolios/{portfolio_id}")


@mcp.tool()
async def delete_portfolio(portfolio_id: str) -> None:
    """Permanently delete a portfolio and all its positions, trades, and cash history.

    This cannot be undone. Confirm the portfolio_id via list_portfolios first if unsure.
    """
    await _request("DELETE", f"/api/v1/portfolios/{portfolio_id}")


# --- Cash accounts ----------------------------------------------------------


@mcp.tool()
async def create_cash_account(
    name: str, base_currency: str, institution: str | None = None
) -> dict[str, Any]:
    """Open an account that holds cash and never a security: a bank balance or an e-wallet.

    It behaves as a portfolio everywhere else -- same journal, same valuation, same performance --
    except that any transaction naming a ticker is rejected. Record deposits, withdrawals,
    interest, and fees with record_transaction. Use transfer_cash to move money to another
    account you own, never a withdrawal here plus a deposit there.
    """
    return await _request(
        "POST",
        "/api/v1/cash-accounts",
        json={"name": name, "base_currency": base_currency, "institution": institution},
    )


@mcp.tool()
async def list_cash_accounts() -> list[dict[str, Any]]:
    """List cash-only accounts, to total the liquid assets held outside a broker."""
    return await _request("GET", "/api/v1/cash-accounts")


# --- Liability accounts -----------------------------------------------------


@mcp.tool()
async def create_liability_account(
    name: str, base_currency: str, institution: str | None = None
) -> dict[str, Any]:
    """Open an account for money owed -- a personal loan, a mortgage, a credit line.

    It is a cash account with the sign reversed: the balance is the debt outstanding, so it runs
    negative and subtracts from every total it appears in. It cannot hold a security.

    Record the drawdown as transfer_cash from this account to wherever the money landed, and each
    repayment as a transfer back. Interest charged is a fee through record_transaction -- not
    interest, which credits cash and is for interest received.

    Only the balance and the cash that moves are stored. The rate, the schedule, and the
    instalments remaining are not, so do not expect any endpoint to compute with them.
    """
    return await _request(
        "POST",
        "/api/v1/liability-accounts",
        json={"name": name, "base_currency": base_currency, "institution": institution},
    )


@mcp.tool()
async def list_liability_accounts() -> list[dict[str, Any]]:
    """List liability accounts, to total what is owed across lenders.

    Pair this with list_cash_accounts and list_portfolios to answer what someone is actually
    worth: those hold the assets, this holds the claims against them.
    """
    return await _request("GET", "/api/v1/liability-accounts")


# --- Transfers --------------------------------------------------------------


@mcp.tool()
async def transfer_cash(
    from_portfolio_id: str,
    to_portfolio_id: str,
    request_id: str,
    amount: str,
    fx_rate: str | None = None,
    occurred_at: str | None = None,
    source_reference: str | None = None,
    memo: str | None = None,
) -> dict[str, Any]:
    """Move cash between two accounts you own, recording both sides as one indivisible event.

    Always prefer this to a withdrawal in one portfolio plus a deposit in the other. Those are
    two unlinked events: if the second fails the money exists in neither account, and nothing
    records that the two were ever the same movement.

    When the two accounts use different currencies, fx_rate is REQUIRED and must be the rate you
    actually received, expressed as destination units per source unit. This service will not look
    a rate up: a market rate differs from an executed one, and the gap would enter the ledger as
    cash that came from nowhere. Passing fx_rate for a same-currency transfer is rejected.

    Undo it with reverse_transfer, never by reversing one half.
    """
    return await _request(
        "POST",
        "/api/v1/transfers",
        json={
            "from_portfolio_id": from_portfolio_id,
            "to_portfolio_id": to_portfolio_id,
            "request_id": request_id,
            "amount": amount,
            "fx_rate": fx_rate,
            "occurred_at": occurred_at,
            "source_reference": source_reference,
            "memo": memo,
        },
    )


@mcp.tool()
async def get_transfer(transfer_id: str) -> dict[str, Any]:
    """Read both halves of one transfer, including the rate applied between two currencies."""
    return await _request("GET", f"/api/v1/transfers/{transfer_id}")


@mcp.tool()
async def reverse_transfer(
    transfer_id: str, request_id: str, memo: str | None = None
) -> dict[str, Any]:
    """Unwind both sides of a transfer together, leaving the originals in the journal.

    Refused if the destination already spent the money: move the cash back first rather than
    overdrawing it. Reversing a single half through reverse_transaction is refused for the same
    reason -- it would leave the money in neither account or in both.
    """
    return await _request(
        "POST",
        f"/api/v1/transfers/{transfer_id}/reversal",
        json={"request_id": request_id, "memo": memo},
    )


@mcp.tool()
async def get_portfolio_summary(portfolio_id: str) -> dict[str, Any]:
    """Value a portfolio: open positions, cash, total value, weights, and realized/unrealized P&L.

    Cash is part of the allocation denominator. Check each position's `price_stale` and the
    top-level `warnings` before using the valuation for a decision.
    """
    return await _request("GET", f"/api/v1/portfolios/{portfolio_id}/summary")


# --- Positions --------------------------------------------------------------


@mcp.tool()
async def list_positions(
    portfolio_id: str, tags: list[str] | None = None, tag_mode: str = "any"
) -> dict[str, Any]:
    """List open positions (quantity > 0) with current valuation and P&L, optionally by tag.

    With no `tags`, all open positions are returned. `tag_mode` "any" matches at least one
    supplied tag; "all" requires every supplied tag. Filters are normalized like tag writes.
    """
    params: dict[str, Any] = {"tag_mode": tag_mode}
    if tags:
        params["tag"] = tags
    return await _request("GET", f"/api/v1/portfolios/{portfolio_id}/positions", params=params)


@mcp.tool()
async def replace_position_tags(
    portfolio_id: str, ticker: str, tags: list[str]
) -> dict[str, Any]:
    """Atomically set the complete tag set on an open position; this is not an append.

    Read or remember existing tags before sending a partial set. Send [] to remove all tags.
    The position must have positive quantity.
    """
    return await _request(
        "PUT",
        f"/api/v1/portfolios/{portfolio_id}/positions/{ticker}/tags",
        json={"tags": tags},
    )


# --- Market -----------------------------------------------------------------


@mcp.tool()
async def get_market_instrument(ticker: str) -> dict[str, Any]:
    """Resolve a Yahoo-compatible ticker and return its quote, provenance, and indicators.

    Use before recording a trade to validate the ticker (AAPL, 2330.TW, 8069.TWO, BTC-USD).
    Returns metadata, latest OHLCV, daily change, 52-week range, market cap, SMA20/50, RSI14,
    and MACD. Check `currency` against the target portfolio and inspect `quote.stale`,
    `quote.provider_as_of`, `quote.fetched_at`, and `warnings` before relying on the price.
    """
    return await _request("GET", f"/api/v1/market/instruments/{ticker}")


@mcp.tool()
async def get_market_history(
    ticker: str,
    days: int | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    interval: str = "1d",
    adjustment: str = "yfinance_auto_adjust",
) -> dict[str, Any]:
    """Get reproducible Yahoo OHLCV history plus provider and period provenance.

    Use legacy `days` (30-730) or an inclusive ISO `start_date`/`end_date` range, not both.
    `interval` is 1d, 1wk, or 1mo. `adjustment` is yfinance_auto_adjust or unadjusted.
    Check provider, actual observation dates, adjustment, and warnings before using the data.
    Fetched on demand; not a trade execution price source.
    """
    params: dict[str, Any] = {"interval": interval, "adjustment": adjustment}
    if days is not None:
        params["days"] = days
    if start_date is not None:
        params["start_date"] = start_date
    if end_date is not None:
        params["end_date"] = end_date
    return await _request("GET", f"/api/v1/market/instruments/{ticker}/history", params=params)


@mcp.tool()
async def get_technical_snapshot(
    ticker: str,
    as_of: str | None = None,
    benchmark: str | None = None,
    event_date: str | None = None,
    lookback_years: int = 5,
) -> dict[str, Any]:
    """Research technical market state and price risk at a reproducible cutoff.

    Company research should pass its report data cutoff as ISO `as_of`. Check provider, actual
    as-of, adjustment, and warnings before use. Interpret trend, momentum, volatility, volume,
    benchmark, and event evidence together with fundamentals, valuation, and other market evidence.
    Anchored VWAP is an approximation derived from daily OHLCV typical prices.
    """
    params: dict[str, Any] = {"lookback_years": lookback_years}
    if as_of is not None:
        params["as_of"] = as_of
    if benchmark is not None:
        params["benchmark"] = benchmark
    if event_date is not None:
        params["event_date"] = event_date
    return await _request(
        "GET", f"/api/v1/market/instruments/{ticker}/technical-snapshot", params=params
    )


@mcp.tool()
async def clear_market_cache(ticker: str) -> dict[str, Any]:
    """Drop cached price history for a ticker so the next lookup refetches it.

    Call after recording a split or dividend: cached daily bars are auto-adjusted, and the
    provider restates them once the action takes effect, which the cache cannot detect. Clearing
    is always safe -- nothing is held that cannot be fetched again. `cache_enabled` false means
    no cache is configured and the call did nothing.
    """
    return await _request("POST", f"/api/v1/market/instruments/{ticker}/cache-clear")


# --- Journal ----------------------------------------------------------------


@mcp.tool()
async def record_transaction(
    portfolio_id: str,
    request_id: str,
    transaction_type: str,
    ticker: str | None = None,
    quantity: str | None = None,
    unit_price: str | None = None,
    amount: str | None = None,
    fee: str = "0",
    tax: str = "0",
    settlement_amount: str | None = None,
    occurred_at: str | None = None,
    trade_date: str | None = None,
    settlement_date: str | None = None,
    source_reference: str | None = None,
    memo: str | None = None,
) -> dict[str, Any]:
    """Record a completed transaction and its cash effect as one atomic event.

    This is the only way to record activity. Every leg -- security, cash, fee, tax -- commits
    together or not at all, so a position can never move without the cash that paid for it.
    Correct a posted event with `reverse_transaction`; events are never edited or deleted.
    This never places an order.

    `transaction_type` is buy, sell, deposit, withdrawal, transfer_in, transfer_out, dividend,
    interest, fee, or tax. Buys and sells need `ticker`, `quantity`, and the actual execution
    `unit_price`; other types need `amount`. Send `amount` as a positive magnitude -- direction
    comes from the type, so a withdrawal takes a positive number. For dividends and interest,
    `amount` is the gross figure and `tax` is the withholding, so the net cash is derived and the
    tax stays on record. Trade `fee` and `tax` capitalize into cost basis. Pass all decimals as
    strings. `request_id` is an idempotency key: reuse it only to retry the identical transaction.
    """
    payload: dict[str, Any] = {
        "request_id": request_id,
        "transaction_type": transaction_type,
        "fee": fee,
        "tax": tax,
    }
    for key, value in (
        ("ticker", ticker),
        ("quantity", quantity),
        ("unit_price", unit_price),
        ("amount", amount),
        ("settlement_amount", settlement_amount),
        ("occurred_at", occurred_at),
        ("trade_date", trade_date),
        ("settlement_date", settlement_date),
        ("source_reference", source_reference),
        ("memo", memo),
    ):
        if value is not None:
            payload[key] = value
    return await _request(
        "POST", f"/api/v1/portfolios/{portfolio_id}/transactions", json=payload
    )


@mcp.tool()
async def reverse_transaction(
    portfolio_id: str, event_id: str, request_id: str, memo: str | None = None
) -> dict[str, Any]:
    """Undo a posted transaction by writing its mirror image; nothing is deleted.

    Position and cash return to their pre-transaction state. The original event stays in the
    ledger marked reversed and linked to this new event, so the entry and its undo are both
    auditable. An event can only be reversed once; correct a reversed event by posting a
    replacement transaction rather than reversing again.
    """
    payload: dict[str, Any] = {"request_id": request_id}
    if memo is not None:
        payload["memo"] = memo
    return await _request(
        "POST",
        f"/api/v1/portfolios/{portfolio_id}/transactions/{event_id}/reversal",
        json=payload,
    )


@mcp.tool()
async def get_journal_event(portfolio_id: str, event_id: str) -> dict[str, Any]:
    """Inspect exactly what one transaction did, leg by leg.

    Returns every leg, a `balance` block whose zero residual proves the event was consistent, the
    `flow_classification` (external investor money versus internal returns such as dividends), and
    links to any reversal. Use this to explain a cash or position change rather than inferring it.
    """
    return await _request(
        "GET", f"/api/v1/portfolios/{portfolio_id}/transactions/{event_id}"
    )


@mcp.tool()
async def list_journal_events(
    portfolio_id: str,
    event_type: str | None = None,
    ticker: str | None = None,
    source_reference: str | None = None,
    start: str | None = None,
    end: str | None = None,
    offset: int = 0,
    limit: int = 50,
    include_legs: bool = False,
) -> dict[str, Any]:
    """Page the audit ledger, newest first, filtered by type, instrument, date, or broker ID.

    Reversals appear as their own events next to what they reversed, so the history shows what was
    undone instead of hiding it. `start` and `end` are RFC 3339 and inclusive. `source_reference`
    matches exactly and is the fastest way to reconcile against a broker statement.

    Set `include_legs` to see what each event actually moved -- instrument, quantity, unit price,
    and the cash, fee, and tax that settled it. Without it you get headers only, and answering
    "what did I trade that day" would cost a `get_journal_event` call per row.
    """
    params: dict[str, Any] = {"offset": offset, "limit": limit, "include_legs": include_legs}
    for key, value in (
        ("event_type", event_type),
        ("ticker", ticker),
        ("source_reference", source_reference),
        ("start", start),
        ("end", end),
    ):
        if value is not None:
            params[key] = value
    return await _request(
        "GET", f"/api/v1/portfolios/{portfolio_id}/transactions", params=params
    )


# --- Corporate actions ------------------------------------------------------


@mcp.tool()
async def record_corporate_action(
    request_id: str,
    ticker: str,
    action_type: str,
    ex_date: str,
    source: str,
    ratio: str | None = None,
    cash_amount: str | None = None,
    currency: str | None = None,
    withholding_tax: str | None = None,
    new_ticker: str | None = None,
    cost_allocation_percent: str | None = None,
    announcement_date: str | None = None,
    record_date: str | None = None,
    pay_date: str | None = None,
    effective_at: str | None = None,
    source_reference: str | None = None,
) -> dict[str, Any]:
    """Record an announced corporate action. This stores facts and changes no holding.

    `action_type` is cash_dividend, interest, split, reverse_split, stock_dividend,
    return_of_capital, symbol_change, merger, or spinoff. `ratio` is new shares per existing
    share -- 2 for a 2-for-1 split, 0.5 for a 1-for-2 reverse split -- and is required for splits
    and stock dividends. `cash_amount` is **per share**, not the total, and is required for
    dividends, interest, and return of capital. Dates are RFC 3339.

    Supply `cost_allocation_percent` only when the issuer actually disclosed it. Leaving it null
    marks the action cost-basis unresolved, which is the correct outcome for an unknown
    allocation: a guessed number is indistinguishable from a real one and silently corrupts every
    later gain calculation. Apply it in a separate step after previewing.
    """
    payload: dict[str, Any] = {
        "request_id": request_id,
        "ticker": ticker,
        "action_type": action_type,
        "ex_date": ex_date,
        "source": source,
    }
    for key, value in (
        ("ratio", ratio),
        ("cash_amount", cash_amount),
        ("currency", currency),
        ("withholding_tax", withholding_tax),
        ("new_ticker", new_ticker),
        ("cost_allocation_percent", cost_allocation_percent),
        ("announcement_date", announcement_date),
        ("record_date", record_date),
        ("pay_date", pay_date),
        ("effective_at", effective_at),
        ("source_reference", source_reference),
    ):
        if value is not None:
            payload[key] = value
    return await _request("POST", "/api/v1/corporate-actions", json=payload)


@mcp.tool()
async def list_corporate_actions(
    ticker: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 50,
) -> dict[str, Any]:
    """List recorded corporate actions and their status.

    `status` is announced, confirmed, applied, or cancelled. Use this to find an action's ID
    before previewing or applying it, and to see what has already been applied.
    """
    params: dict[str, Any] = {"offset": offset, "limit": limit}
    if ticker is not None:
        params["ticker"] = ticker
    if status is not None:
        params["status"] = status
    return await _request("GET", "/api/v1/corporate-actions", params=params)


@mcp.tool()
async def preview_corporate_action_application(
    portfolio_id: str, action_id: str
) -> dict[str, Any]:
    """Show exactly what applying an action would do to a portfolio, writing nothing.

    Always call this before `apply_corporate_action`. Check `applicable`, `warnings`,
    `fractional_handling`, and `cost_basis_unresolved`, and compare `original_quantity` and
    `original_average_cost` against the resulting values. When this service cannot compute a
    defensible cost basis -- a spin-off with no disclosed allocation, a return of capital
    exceeding basis -- `applicable` is false and the reason is in `warnings`. Report that
    honestly rather than applying the action some other way.
    """
    return await _request(
        "GET",
        f"/api/v1/portfolios/{portfolio_id}/corporate-actions/{action_id}/preview",
    )


@mcp.tool()
async def apply_corporate_action(
    portfolio_id: str, action_id: str, request_id: str
) -> dict[str, Any]:
    """Apply a recorded action to a portfolio atomically.

    The journal event and the holding change commit together. An action can be applied to a given
    portfolio only once, so re-running this cannot double-apply a split. A split changes share
    count and unit cost while leaving total cost basis unchanged; a cash dividend pays net of
    withholding and leaves the share count alone.
    """
    return await _request(
        "POST",
        f"/api/v1/portfolios/{portfolio_id}/corporate-actions/{action_id}/apply",
        json={"request_id": request_id},
    )


# --- Instruments ------------------------------------------------------------


@mcp.tool()
async def get_instrument_profile(reference: str) -> dict[str, Any]:
    """Read an instrument's stable ID, issuer, and how each classification field was decided.

    `reference` is a ticker, a stable instrument_id, or a known provider alias. Use this instead of
    assuming an asset's type: `get_market_instrument.asset_type` is a coarse legacy field that
    reports every non-crypto symbol as "stock", so an ETF like VOO or a commodity trust like GLD is
    indistinguishable there. Each entry in `classification` carries a `provenance` -- prefer
    manual_override and verified_internal over provider and derived. Fields that could not be
    resolved are absent and explained in `warnings`; treat them as unknown, not as a default.
    """
    return await _request("GET", f"/api/v1/instruments/{reference}/profile")


@mcp.tool()
async def set_instrument_classification_override(
    reference: str,
    request_id: str,
    field: str,
    reason: str,
    value: str | None = None,
    effective_at: str | None = None,
    retract: bool = False,
) -> dict[str, Any]:
    """Manually correct one classification field when provider metadata is wrong or too coarse.

    `field` is one of asset_class, security_type, sub_asset_class, country_of_risk, or
    is_cash_equivalent. `value` must be a taxonomy member for that field. The provider's own value
    is never modified, only outranked, so passing `retract=true` restores it. `reason` is retained
    for audit; state the evidence rather than a bare assertion. `effective_at` is RFC 3339.
    """
    payload: dict[str, Any] = {
        "request_id": request_id,
        "field": field,
        "value": value,
        "reason": reason,
        "retract": retract,
    }
    if effective_at is not None:
        payload["effective_at"] = effective_at
    return await _request(
        "PUT", f"/api/v1/instruments/{reference}/classification", json=payload
    )


@mcp.tool()
async def map_instrument_issuer(
    reference: str,
    request_id: str,
    legal_name: str,
    display_name: str | None = None,
    country_of_domicile: str | None = None,
    lei: str | None = None,
    issuer_id: str | None = None,
) -> dict[str, Any]:
    """Link a listing to its issuing entity so cross-listing exposure can be aggregated.

    Use this when one company trades under several symbols, such as the ADR TSM and the local line
    2330.TW. The listings stay separate instruments with their own currencies and prices; only the
    issuer is shared. Pass an existing `issuer_id` to attach to that entity; otherwise an issuer is
    matched or created by `legal_name`. Do not map different companies onto one issuer to group
    them thematically.
    """
    payload: dict[str, Any] = {"request_id": request_id, "legal_name": legal_name}
    for key, value in (
        ("display_name", display_name),
        ("country_of_domicile", country_of_domicile),
        ("lei", lei),
        ("issuer_id", issuer_id),
    ):
        if value is not None:
            payload[key] = value
    return await _request("PUT", f"/api/v1/instruments/{reference}/issuer", json=payload)


@mcp.tool()
async def create_valuation_snapshot(
    portfolio_id: str,
    valuation_date: str,
    force_revision: bool = False,
) -> dict[str, Any]:
    """Record what a portfolio was worth on one date, priced with data available then.

    `valuation_date` is `YYYY-MM-DD` and must not be in the future. Holdings and cash are rebuilt
    from the journal at that date rather than read from current balances, and prices come from
    history bounded by the date, so a snapshot never borrows knowledge from later trading.

    Repeating the call for a date returns the stored snapshot rather than recomputing it; pass
    `force_revision=true` only to deliberately replace figures known to be wrong.

    Read `status` before using the total. A `partial` snapshot means at least one holding had no
    price on that date: it is excluded from `securities_value` and carried at cost in
    `unpriced_market_value`, so the total understates the portfolio by an amount you can see.
    """
    payload = {"valuation_date": valuation_date, "force_revision": force_revision}
    return await _request(
        "POST", f"/api/v1/portfolios/{portfolio_id}/valuation-snapshots", json=payload
    )


@mcp.tool()
async def rebuild_valuation_snapshots(
    portfolio_id: str,
    start_date: str,
    end_date: str,
    force_revision: bool = False,
) -> dict[str, Any]:
    """Build a range of daily snapshots as a re-runnable job.

    Dates are `YYYY-MM-DD` and inclusive. Dates that already have a snapshot are skipped, so an
    interrupted run is recovered by calling this again with the same range. A date that fails is
    listed in `failed` without abandoning the rest of the range.

    Use this before `get_nav_history`, which only reads what has already been built. Prefer one
    call spanning the whole range over many single-date calls: history is fetched once per
    instrument for the range rather than once per day.
    """
    payload = {
        "start_date": start_date,
        "end_date": end_date,
        "force_revision": force_revision,
    }
    return await _request(
        "POST", f"/api/v1/portfolios/{portfolio_id}/valuation-snapshots/rebuild", json=payload
    )


@mcp.tool()
async def get_nav_history(
    portfolio_id: str,
    start_date: str,
    end_date: str,
) -> dict[str, Any]:
    """Read the stored daily value series for a date range.

    This reads snapshots and does not create them: dates never built appear in `missing_dates`
    rather than being interpolated, because an invented value would be indistinguishable from a
    computed one. Call `rebuild_valuation_snapshots` first when the series must be complete.

    Before drawing conclusions from the series, check `missing_dates`, `partial_snapshots`, and
    `warnings`. A series with gaps or partial days is not a valid basis for comparing periods.
    """
    params = {"start_date": start_date, "end_date": end_date}
    return await _request(
        "GET", f"/api/v1/portfolios/{portfolio_id}/nav-history", params=params
    )


@mcp.tool()
async def get_portfolio_performance(
    portfolio_id: str,
    start_date: str,
    end_date: str,
    include_daily: bool = False,
) -> dict[str, Any]:
    """Measure how a portfolio performed between two dates.

    Dates are `YYYY-MM-DD` and inclusive. Two returns come back because they answer different
    questions, and quoting one for the other is a real error:

    - `twr_percent` (time-weighted) removes the effect of deposits and withdrawals, so it
      reflects how the holdings did. This is the figure to compare against a benchmark or
      another portfolio.
    - `xirr_percent` (money-weighted) keeps that effect, so it reflects what the investor
      earned on the capital actually at risk. A large deposit landing before a rally lifts XIRR
      above TWR; neither number is wrong.

    Neither is `total_pnl / cost_basis`, which is not a return and should never be described as
    one.

    This reads stored snapshots rather than building them: both ends of the period need one, and
    gaps in between appear in `coverage.missing_dates`. Call `rebuild_valuation_snapshots` first
    if the series is incomplete.

    Check `coverage.is_reliable` before quoting any figure. When it is false -- gaps, partial
    snapshots, or migrated cash events still awaiting a ruling -- the numbers are computable but
    biased, and the reason is in `coverage.warnings`. Report the caveat alongside the number
    rather than presenting it as settled.
    """
    params = {
        "start_date": start_date,
        "end_date": end_date,
        "include_daily": include_daily,
    }
    return await _request(
        "GET", f"/api/v1/portfolios/{portfolio_id}/performance", params=params
    )


@mcp.tool()
async def create_portfolio_group(
    name: str,
    reporting_currency: str,
    portfolio_ids: list[str],
) -> dict[str, Any]:
    """Group portfolios so they can be reported together in one currency.

    Use this when the user holds assets in several currencies -- a USD account and a TWD account,
    say -- and wants one combined view. The portfolios stay independent single-currency ledgers;
    grouping only affects reporting and changes nothing about them.

    Membership starts at each portfolio's own inception, so a group created now can immediately
    report on history that already exists.
    """
    payload = {
        "name": name,
        "reporting_currency": reporting_currency,
        "portfolio_ids": portfolio_ids,
    }
    return await _request("POST", "/api/v1/portfolio-groups", json=payload)


@mcp.tool()
async def list_portfolio_groups() -> list[dict[str, Any]]:
    """List the portfolio groups that exist, with their reporting currency and members.

    Use this to find a group ID before calling `get_consolidated_summary`, rather than asking
    the user to supply one.
    """
    return await _request("GET", "/api/v1/portfolio-groups")


@mcp.tool()
async def get_portfolio_group(group_id: str) -> dict[str, Any]:
    """Read a group's name, reporting currency, and current members."""
    return await _request("GET", f"/api/v1/portfolio-groups/{group_id}")


@mcp.tool()
async def delete_portfolio_group(group_id: str) -> None:
    """Delete a portfolio group. The portfolios in it are not affected.

    This removes only the grouping: every portfolio, trade, journal event, and snapshot survives,
    and an identical group can be recreated. Nothing recorded is lost.

    To stop reporting one portfolio while keeping the group, call
    `update_portfolio_group_members` with the remaining IDs instead -- that closes the one
    membership and leaves earlier reports intact.
    """
    await _request("DELETE", f"/api/v1/portfolio-groups/{group_id}")


@mcp.tool()
async def update_portfolio_group_members(
    group_id: str, portfolio_ids: list[str]
) -> dict[str, Any]:
    """Replace which portfolios belong to a group.

    Pass the complete desired membership, not just additions. A portfolio removed here keeps its
    historical membership, so reports for earlier dates still include it; membership can never be
    edited in a way that restates a past consolidation.
    """
    return await _request(
        "PUT",
        f"/api/v1/portfolio-groups/{group_id}/members",
        json={"portfolio_ids": portfolio_ids},
    )


@mcp.tool()
async def get_consolidated_summary(
    group_id: str,
    as_of: str | None = None,
    reporting_currency: str | None = None,
) -> dict[str, Any]:
    """Total a group's holdings and cash across currencies.

    This is the tool for "what am I worth in total" when holdings span currencies. Each portfolio
    is valued in its own currency and converted at the rate in force on `as_of` (`YYYY-MM-DD`,
    defaulting to today); rates never come from after that date. Every position keeps its local
    figure alongside the converted one, plus the rate, the conversion path, and the rate's date.

    Before quoting `total_value`, check `converted_value_coverage_percent` and `unconverted`.
    Value whose currency pair could not be resolved is excluded from the total rather than
    converted at a guessed rate, so the total can legitimately understate the group -- those two
    fields are the only way to see by how much. Report that shortfall rather than the total alone.

    `issuer_exposure` combines listings of the same company, so an ADR and its local line read as
    one economic exposure while remaining separate positions.
    """
    params: dict[str, Any] = {}
    if as_of is not None:
        params["as_of"] = as_of
    if reporting_currency is not None:
        params["reporting_currency"] = reporting_currency
    return await _request(
        "GET", f"/api/v1/portfolio-groups/{group_id}/summary", params=params
    )


# --- Resources --------------------------------------------------------------
#
# Tools describe one operation each. These describe the things an agent must know *before*
# choosing a tool -- the legal vocabulary, the conventions, and what already exists -- which no
# single tool docstring is the right place for.


@mcp.resource(
    "portfolio://conventions",
    name="portfolio_conventions",
    title="Conventions and invariants",
    description="Ticker formats, decimal handling, idempotency, and the full error-code list.",
    mime_type="text/markdown",
)
def conventions_resource() -> str:
    """The rules that govern every call, gathered in one place."""
    codes = "\n".join(f"- `{code}` — {text}" for code, text in sorted(ERROR_CODES.items()))
    return f"""# Portfolio Manager conventions

## Recording activity

`record_transaction` is the only way to record activity. It posts the security, cash, fee, and
tax legs of one event together or not at all, so a position can never move without the cash that
paid for it. Correct a posted event with `reverse_transaction`, which writes an opposing event;
posted events are never edited or deleted.

Transaction types: `buy`, `sell`, `deposit`, `withdrawal`, `transfer_in`, `transfer_out`,
`dividend`, `interest`, `fee`, `tax`.

`amount` is always a positive magnitude — direction comes from the type, so a withdrawal takes a
positive amount. `quantity` and `unit_price` are required for `buy` and `sell`. Income is recorded
gross, with withholding in `tax`.

## Tickers and currency

`AAPL` (US), `2330.TW` (TWSE), `8069.TWO` (TPEX), `BTC-USD` (crypto).

Each portfolio has exactly one `base_currency` and rejects any instrument quoted in another, with
`currency_mismatch`. Hold USD and TWD assets in separate portfolios, then group them with
`create_portfolio_group` for a combined view.

## Numbers and time

Send every financial value as a decimal string (`"10.5"`, not `10.5`); responses return decimals
as strings too. Binary floats are never used in accounting paths. Timestamps are UTC RFC 3339.
Omitted event times default to server time — pass `occurred_at` explicitly when recording history.

## Idempotency

Every mutation needs a client-generated `request_id`. Retrying the identical payload with the same
ID is safe and returns the original result. Reusing an ID with different data returns
`idempotency_conflict`.

## Reading a result honestly

This service reports what it cannot determine rather than filling it in. Before using a number,
check the disclosure that comes with it:

- `status` on a snapshot — `partial` means a holding had no price that day and is excluded from
  `securities_value`, carried at cost in `unpriced_market_value`.
- `coverage.is_reliable` on performance — false when the series has gaps, partial valuations, or
  an event whose cash flow could not be classified.
- `converted_value_coverage_percent` and `unconverted` on a consolidated summary — an unresolvable
  currency pair is left out of the total rather than converted at a guess.
- `stale`, `provider_as_of`, and `warnings` on a quote. `stale` means a refresh failed and an
  older quote was returned. It does not mean the quote came from cache: a quote is cached until
  its market reopens, and that price is current because a closed market's last trade is final.
  Judge age with `provider_as_of` against the market's hours, not by assuming a fixed TTL.

## Error codes

Errors return `{{code, message, details}}`. Branch on `code`, never on message text.

{codes}
"""


@mcp.resource(
    "portfolio://taxonomy",
    name="portfolio_taxonomy",
    title="Classification vocabulary",
    description="Legal asset_class, security_type, and provenance values with their precedence.",
    mime_type="text/markdown",
)
def taxonomy_resource() -> str:
    """The only accepted classification values, so an override never has to be guessed."""
    asset_classes = "\n".join(f"- `{item.value}`" for item in AssetClass)
    security_types = "\n".join(f"- `{item.value}`" for item in SecurityType)
    ranked = sorted(PROVENANCE_RANK.items(), key=lambda pair: pair[1], reverse=True)
    provenance = "\n".join(
        f"{position}. `{item.value}`" for position, (item, _) in enumerate(ranked, start=1)
    )
    fields = ", ".join(f"`{name}`" for name in sorted(CLASSIFICATION_FIELDS))
    return f"""# Classification vocabulary

`set_instrument_classification_override` rejects any value outside these lists with
`invalid_classification_value`. Fields: {fields}.

## Two independent axes

`asset_class` is the economic exposure a holding carries. `security_type` is its legal or
structural wrapper. They are deliberately separate: GLD is a `commodity_trust` carrying
`commodity` exposure, and a stablecoin is a `crypto_asset` that behaves as a `cash_equivalent`.
Collapsing them is what makes a provider report an ETF as common stock.

## asset_class

{asset_classes}

## security_type

{security_types}

## Provenance, highest rank first

{provenance}

A higher-ranked source wins without erasing the lower one, so retracting a manual override
restores the provider's original value. A symbol that cannot be resolved stays `unclassified`
rather than being guessed from its spelling — the gap stays visible to everything downstream.

Funds are the common case needing a human: naming a fund's underlying exposure requires a
verified mapping, so ETFs frequently arrive with `security_type` known and `asset_class`
`unclassified`. That is a real gap, not an error.
"""


@mcp.resource(
    "portfolio://portfolios",
    name="portfolio_inventory",
    title="Existing portfolios and groups",
    description="Live list of portfolios with their base currency, and any reporting groups.",
    mime_type="text/markdown",
)
async def portfolios_resource() -> str:
    """What already exists, so a session does not open with discovery calls."""
    try:
        portfolios = await _request("GET", "/api/v1/portfolios")
        groups = await _request("GET", "/api/v1/portfolio-groups")
    except ApiError as exc:
        return f"# Portfolios\n\nCould not reach the API: `{exc.code}` — {exc.message}\n"
    except RuntimeError as exc:
        # A resource that raises leaves the client with no listing at all. Saying the inventory
        # is unavailable is more useful than failing the whole read.
        return f"# Portfolios\n\nInventory unavailable: {exc}\n"

    if not portfolios:
        lines = ["No portfolios exist yet. Create one with `create_portfolio`."]
    else:
        lines = [f"- **{item['name']}** ({item['base_currency']}) — `{item['id']}`"
                 for item in portfolios]

    group_items = groups.get("items", []) if isinstance(groups, dict) else groups
    if group_items:
        lines.append("")
        lines.append("## Reporting groups")
        lines += [
            f"- **{item['name']}** → {item['reporting_currency']} — `{item['id']}`"
            for item in group_items
        ]

    return "# Portfolios\n\n" + "\n".join(lines) + "\n"


# --- Prompts ----------------------------------------------------------------
#
# A prompt carries what the tool list cannot: the order operations go in, and the mistakes that
# order prevents. Tool docstrings say what one call does; these say how the calls fit together.


@mcp.prompt(
    title="Open an account that already holds cash and stock",
    description="Correct sequence for recording an existing brokerage account's opening balance.",
)
def open_account_with_holdings(
    account_name: str = "",
    base_currency: str = "USD",
    opening_date: str = "",
) -> str:
    """Record an existing account without inventing a position or losing its cost basis."""
    name = account_name or "<account name>"
    date_hint = opening_date or "<YYYY-MM-DD, the real opening date>"
    return f"""Set up a portfolio for an account that already holds cash and securities.

Account: {name}
Base currency: {base_currency}
Opening date: {date_hint}

The journal will not let a position appear from nothing — a `buy` whose legs do not balance is
rejected with `journal_out_of_balance`. So the opening cash must include what the holdings
originally cost, and each holding is then bought back out of it.

Follow this order:

1. `create_portfolio(name={name!r}, base_currency={base_currency!r})` and keep the returned id.

2. One `record_transaction` with `transaction_type="transfer_in"`, where `amount` is
   **the cash balance plus the total original cost of every holding**, and `occurred_at` is the
   opening date.

   Use `transfer_in`, not `deposit`: this money was not contributed today. Recording it as a
   deposit tells XIRR that fresh capital arrived on the opening date and distorts the return.

3. One `record_transaction` with `transaction_type="buy"` per holding, using the price actually
   paid and the same `occurred_at`.

   Use the original cost, never today's market price. Valuing an opening position at market
   resets its unrealized gain to zero and erases a gain the investor really has.

4. `get_portfolio_summary` to confirm cash matches the real balance and each position shows its
   true average cost.

Before starting, ask me for anything you do not have: the cash balance, and each holding's
ticker, quantity, and original unit cost. If a cost basis is genuinely unknown, say so and record
it in the transaction `memo` rather than substituting a plausible number — a guessed basis is
indistinguishable from a real one once written, and every P&L figure inherits the error.

If the account has full transaction history and I want returns measured across all of it, tell me
that this opening-balance approach starts the series at the opening date, and that the
alternative is recording each historical transaction in date order instead.
"""


@mcp.prompt(
    title="Record trading activity",
    description="Record buys, sells, dividends, and cash movements with correct settlement.",
)
def record_daily_activity(activity: str = "") -> str:
    """Turn a broker statement or a described trade into balanced journal events."""
    described = activity or "<paste the trades, dividends, or cash movements>"
    return f"""Record this activity in the portfolio:

{described}

Use `record_transaction` for every item — it is the only write path, and it posts the position
and its settlement cash in one transaction so the two can never disagree.

Rules that decide the details:

- Direction comes from `transaction_type`, so `amount` is always positive. A withdrawal takes a
  positive amount.
- Trade `fee` and `tax` capitalize into cost basis on a buy and net against proceeds on a sell.
  Pass them as their own fields rather than adjusting `unit_price`.
- Dividends are recorded gross, with withholding in `tax`. Never record the net figure alone: the
  tax is unrecoverable once collapsed into the amount, and income must stay distinguishable from
  an investor contribution or TWR will treat it as new capital.
- `unit_price` is the actual execution price, not a market quote.
- If the broker reports an exact settlement figure that differs from quantity x price +/- costs,
  pass it as `settlement_amount`; the event must still balance.
- Give every item its own `request_id` and set `occurred_at` to when it actually happened.

Report what you recorded, and call out anything you could not: an ambiguous line is worth one
question, and a guessed one silently corrupts the ledger.
"""


@mcp.prompt(
    title="Measure performance over a period",
    description="Build the snapshots a return needs, then read TWR and XIRR with their coverage.",
)
def analyze_performance(
    portfolio_id: str = "",
    start_date: str = "",
    end_date: str = "",
) -> str:
    """Measure return correctly, including whether the underlying data supports the number."""
    target = portfolio_id or "<portfolio id, or ask me which portfolio>"
    return f"""Measure performance for portfolio {target} from {start_date or "<start>"} to
{end_date or "<end>"}.

Returns are computed from stored daily snapshots, so build them first:

1. `rebuild_valuation_snapshots(portfolio_id, start_date, end_date)`. Safe to re-run — dates that
   already have a snapshot are skipped.
2. `get_portfolio_performance(portfolio_id, start_date, end_date)`.

Then report both returns, because they answer different questions:

- `twr_percent` removes the effect of deposits and withdrawals, so it measures the holdings. This
  is the figure to compare against a benchmark.
- `xirr_percent` keeps that effect, so it measures what the investor earned on the capital
  actually at risk.

Neither is `total_pnl / cost_basis`, which is not a return and should not be presented as one.

Read `coverage` before quoting either number, and state what it says:

- `is_reliable: false` means the figures are computable but biased. Say so alongside them rather
  than presenting them as settled.
- `missing_dates` are gaps the linked return jumps across; they can be filled by rebuilding.
- `partial_snapshots` are days where a holding had no price and was excluded from the total.
- `unclassified_flow_events` are events whose cash sits in neither the capital base nor the
  return.

A return quoted without its coverage is the failure this service is built to prevent.
"""


@mcp.prompt(
    title="Audit data quality",
    description="Find the gaps this service reports rather than fills, and what can be fixed.",
)
def audit_data_quality(portfolio_id: str = "") -> str:
    """Survey what is unresolved, separating what a person can fix from what is inherent."""
    target = portfolio_id or "every portfolio from `list_portfolios`"
    return f"""Audit the data quality of {target} and report what is unresolved.

Gather:

1. `get_portfolio_summary` — check `warnings` and any position with `price_stale: true`.
2. `get_instrument_profile` for each holding — note every field whose `provenance` is
   `unclassified`, and read `portfolio://taxonomy` for the values an override may use.
3. `get_nav_history` — note `missing_dates` and any snapshot with `status: partial`.
4. `get_consolidated_summary` if the portfolios are grouped — check
   `converted_value_coverage_percent` and `unconverted`.

Then separate the findings into two lists, because they call for different responses:

- **Actionable**: gaps a person can close. An ETF whose `asset_class` is `unclassified` can be
  set with `set_instrument_classification_override` once its exposure is verified. Missing
  snapshot dates can be built with `rebuild_valuation_snapshots`.
- **Inherent**: facts about the data that no work will change, such as an instrument the provider
  has never priced. Report these once, plainly, and do not present them as tasks.

Keeping them apart is the point. A warning nobody can ever clear teaches people to ignore the
warnings that matter.

Do not propose a classification you cannot justify from a source. `unclassified` is a correct
answer; an invented one is indistinguishable from a verified one once stored.
"""


def main() -> None:
    transport = os.getenv("PORTFOLIO_MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        mcp.settings.host = os.getenv("PORTFOLIO_MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.getenv("PORTFOLIO_MCP_PORT", "8002"))
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
