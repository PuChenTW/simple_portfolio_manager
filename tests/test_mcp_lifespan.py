"""Exercise real MCP HTTP sessions without sockets or a running API."""

from collections.abc import AsyncIterator

import anyio
import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from portfolio_manager import mcp_server

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class ApiTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.closed = anyio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok"})

    async def aclose(self) -> None:
        # Real transport cleanup awaits I/O; cancellation must not interrupt it.
        await anyio.sleep(0)
        self.closed.set()


@pytest.fixture
async def mcp_http(monkeypatch) -> AsyncIterator[tuple[httpx.AsyncClient, list[ApiTransport]]]:
    real_client = httpx.AsyncClient
    transports = []

    def api_client(**kwargs) -> httpx.AsyncClient:
        transport = ApiTransport()
        transports.append(transport)
        return real_client(transport=transport, **kwargs)

    server = FastMCP("lifespan-test", lifespan=mcp_server.lifespan, json_response=True)
    server.add_tool(mcp_server.get_health)
    monkeypatch.setattr(mcp_server, "mcp", server)
    app = server.streamable_http_app()
    async with (
        real_client(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002"
        ) as client,
        app.router.lifespan_context(app),
    ):
        monkeypatch.setattr(mcp_server.httpx, "AsyncClient", api_client)
        yield client, transports


def _headers(session: str) -> dict[str, str]:
    return {"Accept": "application/json, text/event-stream", "mcp-session-id": session}


async def _initialize_session(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
    )
    response.raise_for_status()
    session = response.headers["mcp-session-id"]
    initialized = await client.post(
        "/mcp",
        headers=_headers(session),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
    )
    initialized.raise_for_status()
    return session


async def _call_health(client: httpx.AsyncClient, session: str) -> httpx.Response:
    return await client.post(
        "/mcp",
        headers=_headers(session),
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "get_health", "arguments": {}},
        },
    )


@pytest.mark.parametrize("closed_session", [0, 1])
async def test_closing_one_session_preserves_the_other(mcp_http, closed_session) -> None:
    client, transports = mcp_http
    sessions = [await _initialize_session(client), await _initialize_session(client)]
    surviving = sessions[1 - closed_session]
    before = await _call_health(client, surviving)
    assert not before.json()["result"]["isError"]

    response = await client.delete("/mcp", headers=_headers(sessions[closed_session]))
    response.raise_for_status()
    with anyio.fail_after(2):
        await transports[closed_session].closed.wait()

    after = await _call_health(client, surviving)
    assert not after.json()["result"]["isError"], after.text
    assert not transports[1 - closed_session].closed.is_set()


async def test_idle_session_releases_client_and_expires(mcp_http) -> None:
    client, transports = mcp_http
    mcp_server.mcp.session_manager.session_idle_timeout = 0.05
    session = await _initialize_session(client)
    with anyio.fail_after(2):
        await transports[0].closed.wait()

    response = await _call_health(client, session)
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "Session not found"
    replacement = await _initialize_session(client)
    response = await _call_health(client, replacement)
    assert not response.json()["result"]["isError"], response.text


async def test_activity_renews_idle_deadline(mcp_http) -> None:
    client, transports = mcp_http
    mcp_server.mcp.session_manager.session_idle_timeout = 0.2
    session = await _initialize_session(client)
    for _ in range(4):
        await anyio.sleep(0.07)
        response = await _call_health(client, session)
        assert not response.json()["result"]["isError"], response.text
        assert not transports[0].closed.is_set()
    with anyio.fail_after(2):
        await transports[0].closed.wait()


async def test_idle_cleanup_does_not_interrupt_another_session(mcp_http) -> None:
    client, transports = mcp_http
    mcp_server.mcp.session_manager.session_idle_timeout = 0.2
    idle = await _initialize_session(client)
    active = await _initialize_session(client)
    for _ in range(4):
        await anyio.sleep(0.07)
        response = await _call_health(client, active)
        assert not response.json()["result"]["isError"], response.text
    with anyio.fail_after(2):
        await transports[0].closed.wait()
    assert not transports[1].closed.is_set()
    response = await _call_health(client, idle)
    assert response.status_code == 404


async def test_shutdown_releases_session_clients(monkeypatch) -> None:
    real_client = httpx.AsyncClient
    transports = []

    def api_client(**kwargs) -> httpx.AsyncClient:
        transport = ApiTransport()
        transports.append(transport)
        return real_client(transport=transport, **kwargs)

    server = FastMCP("shutdown-test", lifespan=mcp_server.lifespan, json_response=True)
    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", api_client)
    app = server.streamable_http_app()
    async with (
        real_client(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8002"
        ) as client,
        app.router.lifespan_context(app),
    ):
        await _initialize_session(client)
        await _initialize_session(client)
    assert len(transports) == 2
    assert all(transport.closed.is_set() for transport in transports)


async def test_stdio_session_uses_and_releases_lifespan_client(monkeypatch) -> None:
    real_client = httpx.AsyncClient
    transport = ApiTransport()
    monkeypatch.setattr(
        mcp_server.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    async with create_connected_server_and_client_session(mcp_server.mcp) as session:
        result = await session.call_tool("get_health")
        assert not result.isError
        assert not transport.closed.is_set()
    assert transport.closed.is_set()


async def test_tool_outside_request_reports_inactive_lifespan() -> None:
    with pytest.raises(RuntimeError, match="HTTP client is not initialized"):
        await mcp_server.get_health()


@pytest.mark.parametrize("timeout", [None, "60"])
def test_http_startup_configures_idle_timeout(monkeypatch, timeout) -> None:
    server = FastMCP("startup-test", lifespan=mcp_server.lifespan)
    monkeypatch.setattr(mcp_server, "mcp", server)
    monkeypatch.setenv("PORTFOLIO_MCP_TRANSPORT", "streamable-http")
    monkeypatch.delenv("PORTFOLIO_MCP_SESSION_IDLE_TIMEOUT_SECONDS", raising=False)
    if timeout is not None:
        monkeypatch.setenv("PORTFOLIO_MCP_SESSION_IDLE_TIMEOUT_SECONDS", timeout)
    monkeypatch.setattr(server, "run", lambda **kwargs: None)

    mcp_server.main()

    assert server.session_manager.session_idle_timeout == (int(timeout) if timeout else 1800)


@pytest.mark.parametrize("timeout", ["0", "-1", "invalid"])
def test_http_startup_rejects_invalid_idle_timeout(monkeypatch, timeout) -> None:
    monkeypatch.setenv("PORTFOLIO_MCP_TRANSPORT", "streamable-http")
    monkeypatch.setenv("PORTFOLIO_MCP_SESSION_IDLE_TIMEOUT_SECONDS", timeout)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda **kwargs: None)
    with pytest.raises(ValueError):
        mcp_server.main()


def test_stdio_startup_does_not_configure_http_expiry(monkeypatch) -> None:
    server = FastMCP("stdio-startup-test", lifespan=mcp_server.lifespan)
    monkeypatch.setattr(mcp_server, "mcp", server)
    monkeypatch.setenv("PORTFOLIO_MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("PORTFOLIO_MCP_SESSION_IDLE_TIMEOUT_SECONDS", "invalid")
    started = []
    monkeypatch.setattr(server, "run", lambda **kwargs: started.append(kwargs))
    mcp_server.main()
    assert started == [{"transport": "stdio"}]
