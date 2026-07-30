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

DEFAULT_BASE_URL = "http://127.0.0.1:8001"

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


@mcp.tool()
async def get_portfolio_summary(portfolio_id: str) -> dict[str, Any]:
    """Value a portfolio: open positions, cash, total value, weights, and realized/unrealized P&L.

    Cash is part of the allocation denominator. Check each position's `price_stale` and the
    top-level `warnings` before using the valuation for a decision.
    """
    return await _request("GET", f"/api/v1/portfolios/{portfolio_id}/summary")


# --- Trades -----------------------------------------------------------------


@mcp.tool()
async def record_trade(
    portfolio_id: str,
    request_id: str,
    ticker: str,
    side: str,
    quantity: str,
    unit_price: str,
    fee: str = "0",
    executed_at: str | None = None,
) -> dict[str, Any]:
    """Record a completed spot buy or sell; this never places an order and never changes cash.

    `request_id` is a client-generated idempotency key: reuse it only to retry the exact same
    trade. `side` is "buy" or "sell". Send `quantity`, `unit_price`, and `fee` as decimal
    strings for exact input. Provide the actual execution price rather than a market quote.
    Buys recalculate moving-average cost; sells realize P&L and cannot exceed current quantity.
    `executed_at` is RFC 3339; omit it to use server time.
    """
    payload: dict[str, Any] = {
        "request_id": request_id,
        "ticker": ticker,
        "side": side,
        "quantity": quantity,
        "unit_price": unit_price,
        "fee": fee,
    }
    if executed_at is not None:
        payload["executed_at"] = executed_at
    return await _request("POST", f"/api/v1/portfolios/{portfolio_id}/trades", json=payload)


@mcp.tool()
async def list_trades(portfolio_id: str, offset: int = 0, limit: int = 50) -> dict[str, Any]:
    """List the reverse-chronological trade ledger. Audit only; no live valuation is returned."""
    params = {"offset": offset, "limit": limit}
    return await _request("GET", f"/api/v1/portfolios/{portfolio_id}/trades", params=params)


# --- Cash -------------------------------------------------------------------


@mcp.tool()
async def record_cash_transaction(
    portfolio_id: str,
    request_id: str,
    action: str,
    amount: str,
    occurred_at: str | None = None,
) -> dict[str, Any]:
    """Record a cash deposit or withdrawal in the base currency, independent of asset trades.

    `request_id` is a client-generated idempotency key: reuse it only for exact retries.
    `action` is "deposit" or "withdraw". Send `amount` as a decimal string. Withdrawals cannot
    exceed available cash. Asset trades never adjust cash, so record settlement cash separately
    only when that matches the source account. `occurred_at` is RFC 3339; omit for server time.
    """
    payload: dict[str, Any] = {
        "request_id": request_id,
        "action": action,
        "amount": amount,
    }
    if occurred_at is not None:
        payload["occurred_at"] = occurred_at
    return await _request(
        "POST", f"/api/v1/portfolios/{portfolio_id}/cash-transactions", json=payload
    )


@mcp.tool()
async def list_cash_transactions(
    portfolio_id: str, offset: int = 0, limit: int = 50
) -> dict[str, Any]:
    """List the reverse-chronological cash ledger. Current cash is in the portfolio summary."""
    params = {"offset": offset, "limit": limit}
    return await _request(
        "GET", f"/api/v1/portfolios/{portfolio_id}/cash-transactions", params=params
    )


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

    Prefer this over `record_trade` plus `record_cash_transaction`: those are two independent
    writes that can leave a position updated with cash untouched, while this posts every leg
    together or not at all. This never places an order.

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
) -> dict[str, Any]:
    """Page the audit ledger, newest first, filtered by type, instrument, date, or broker ID.

    Reversals appear as their own events next to what they reversed, so the history shows what was
    undone instead of hiding it. `start` and `end` are RFC 3339 and inclusive. `source_reference`
    matches exactly and is the fastest way to reconcile against a broker statement.
    """
    params: dict[str, Any] = {"offset": offset, "limit": limit}
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
