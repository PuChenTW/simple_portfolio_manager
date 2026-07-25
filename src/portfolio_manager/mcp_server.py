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
async def get_market_history(ticker: str, days: int = 365) -> dict[str, Any]:
    """Get split/dividend-adjusted daily OHLCV history (30-730 days) for agent-side research.

    Fetched on demand; not a trade execution price source.
    """
    return await _request(
        "GET", f"/api/v1/market/instruments/{ticker}/history", params={"days": days}
    )


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
