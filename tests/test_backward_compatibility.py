"""The published API and MCP surface must not change without a version bump.

`legacy_api_baseline.json` is a frozen capture of the 32 operations, 67 response models, and 32
MCP tools published at version 0.2.0. Agents and generated clients are built against that
contract, so a change here breaks callers that cannot be updated in lockstep.

Adding operations, models, or tools is fine and expected. Changing or removing an existing one is
not: it needs an explicit version bump, not a passing test suite. Regenerate this baseline only
when deliberately shipping a breaking change.

0.2.0 was one such change. It removed `record_trade`, `list_trades`, `record_cash_transaction`,
and `list_cash_transactions` along with their tables. Those were independent ledgers: a buy moved
the position without touching cash, so the two could disagree and nothing recorded which was
right. `record_transaction` is now the only write path, and it posts a position and its settlement
in one transaction or not at all.
"""

import asyncio
import json
from pathlib import Path

import pytest

from portfolio_manager.api import app
from portfolio_manager.mcp_server import mcp

BASELINE = json.loads((Path(__file__).parent / "legacy_api_baseline.json").read_text())


def current_operations() -> dict:
    schema = app.openapi()
    return {
        route["operationId"]: {
            "path": path,
            "method": method,
            "requestBody": route.get("requestBody"),
            "responses": dict(route.get("responses", {})),
            # Lists, not tuples: the baseline is JSON, where a tuple round-trips to a list.
            "parameters": sorted(
                [item["name"], item["in"]] for item in route.get("parameters", [])
            ),
        }
        for path, item in schema["paths"].items()
        for method, route in item.items()
        if isinstance(route, dict) and "operationId" in route
    }


@pytest.fixture(scope="module")
def operations() -> dict:
    return current_operations()


def test_no_legacy_operation_was_removed(operations) -> None:
    missing = set(BASELINE["operations"]) - set(operations)
    assert not missing, f"operations removed from the public API: {sorted(missing)}"


@pytest.mark.parametrize("operation_id", sorted(BASELINE["operations"]))
def test_legacy_operation_contract_is_unchanged(operation_id: str, operations) -> None:
    """Route, request body, responses, and parameters must all match the frozen contract."""
    expected = BASELINE["operations"][operation_id]
    actual = operations[operation_id]

    assert (actual["path"], actual["method"]) == (expected["path"], expected["method"])
    assert actual["requestBody"] == expected["requestBody"]
    assert actual["parameters"] == expected["parameters"]
    for code, response in expected["responses"].items():
        assert code in actual["responses"], f"{operation_id} dropped response {code}"
        assert actual["responses"][code] == response


def test_legacy_response_models_are_unchanged() -> None:
    current = app.openapi()["components"]["schemas"]
    for name, model in BASELINE["models"].items():
        assert name in current, f"response model {name} was removed"
        assert current[name] == model, f"response model {name} changed shape"


def test_legacy_mcp_tools_keep_their_signatures() -> None:
    tools = {tool.name: tool.inputSchema for tool in asyncio.run(mcp.list_tools())}
    for name, schema in BASELINE["tools"].items():
        assert name in tools, f"MCP tool {name} was removed"
        assert tools[name] == schema, f"MCP tool {name} changed its input signature"


def test_the_baseline_matches_the_declared_version(operations) -> None:
    """A regenerated baseline without a version bump is the failure this file exists to catch."""
    assert app.openapi()["info"]["version"] == BASELINE["version"]


def test_recording_a_purchase_moves_cash_and_position_together(harness) -> None:
    """The invariant that replaced the legacy ledgers.

    The removed `record_trade` deliberately left cash untouched, which let a portfolio's position
    and cash disagree with nothing recording which was right. Every write now settles atomically.
    """
    portfolio_id = harness.portfolio()
    endpoint = f"/api/v1/portfolios/{portfolio_id}/transactions"
    harness.client.post(
        endpoint,
        json={"request_id": "c-1", "transaction_type": "deposit", "amount": "10000"},
    )
    harness.client.post(
        endpoint,
        json={
            "request_id": "t-1",
            "transaction_type": "buy",
            "ticker": "AAPL",
            "quantity": "10",
            "unit_price": "140",
        },
    )

    summary = harness.client.get(f"/api/v1/portfolios/{portfolio_id}/summary").json()
    assert summary["cash_value"] == "8600", "the purchase settled against cash"
    assert summary["positions"][0]["quantity"] == "10"
