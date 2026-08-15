"""The published API and MCP surface must not change without a version bump.

`legacy_api_baseline.json` is a frozen capture of the operations, response models, and MCP tools
published at version 0.2.0 -- 32, 67, and 32 of them respectively at that time. Agents and
generated clients are built against that contract, so a change here breaks callers that cannot be
updated in lockstep. Later versions add to the capture; the frozen entries never move.

Adding operations, models, or tools is fine and expected. Changing or removing an existing one is
not: it needs an explicit version bump, not a passing test suite. Regenerate this baseline only
when deliberately shipping a breaking change.

0.2.0 was one such change. It removed `record_trade`, `list_trades`, `record_cash_transaction`,
and `list_cash_transactions` along with their tables. Those were independent ledgers: a buy moved
the position without touching cash, so the two could disagree and nothing recorded which was
right. `record_transaction` is now the only write path, and it posts a position and its settlement
in one transaction or not at all.

0.3.0 added `include_legs` to `list_journal_events`, a `legs` field to `JournalEventRead`, and a
`ticker` field to `JournalLegRead`, so reading a day of activity costs one request instead of one
per event. Every addition is optional and defaults to the previous behavior, so no caller has to
change -- but the assertions below compare parameter sets and schemas for equality, not
containment, and a purely additive change still has to be declared. That strictness is the point:
it forces the addition to be a decision rather than a diff nobody noticed.

0.4.0 added `clear_market_cache` -- one operation, one model, one MCP tool -- alongside the Redis
price-history cache. Nothing existing moved, so only the version line of the baseline changed.
The tool exists because cached bars are auto-adjusted and a provider silently restates them after
a split or dividend: rather than expire entries on a guess about when that happened, recording
such an action warns the operator and this drops the affected symbol on request.

0.5.0 added cash accounts and transfers: five operations, five models, five MCP tools, and one
change to an existing model. `PortfolioRead` gained `kind` and `institution`, which is the only
frozen shape that moved. That was a deliberate trade. `list_portfolios` is the documented way to
discover portfolios, and without `kind` on the response a caller cannot tell a bank account from a
brokerage account without a second request per portfolio -- a worse contract than the break. Both
fields carry defaults, so a client that ignores them reads exactly what it read before.

Transfers exist because moving money between two accounts was previously two unlinked events, one
per portfolio, with nothing recording that they were the same movement. If the second failed the
money existed in neither account. `transfer_cash` writes both halves in one database transaction,
and `reverse_transfer` unwinds both; reversing one side alone is now refused. Each half still
balances on its own in its own currency, so replay, valuation, and performance were unchanged --
a cross-currency transfer is two single-currency events, not one event holding two currencies,
because the balance validator nets legs into a single functional currency and an event that
balances only after conversion is an unbalanced event wearing an exchange rate.

0.6.0 added liability accounts: two operations, one model, two MCP tools, and three fields on
`ConsolidatedSummaryRead`. The fields are the reason for the bump. `total_value` was always a
net figure -- every total in that response is a signed sum, so a loan subtracted correctly before
any of this existed -- but a single net number cannot distinguish 5M in cash from 15M held
against a 10M loan, and that distinction is the whole point of recording a debt. `assets_value`,
`liabilities_value`, and `net_value` split the number that was already there rather than
introducing a second opinion on it: `net_value` is `total_value`, and the three always reconcile.

`PortfolioKind` gained `liability`. That member is not itself frozen -- `PortfolioRead.kind`
captures a `$ref`, not the enum's contents -- so a client switching on the value sees a new one
without the baseline noticing. Anything reading `kind` should treat an unrecognized value as a
book it cannot interpret rather than assuming `investment`.

0.7.0 added portfolio and group renaming: no frozen shape moved, only the version line.

0.8.0 added `asset_class` and `asset_class_provenance` to `ConsolidatedPositionRead`. Both
default to `unclassified`, so a client that ignores them reads exactly what it read before, and
no operation, model count, or tool changed. The bump exists because the assertions compare
schemas for equality rather than containment -- an addition still has to be a decision.

The fields are on the consolidated summary rather than behind a per-instrument lookup because
the summary is where every holding is already listed together. Asking "what is this group's
exposure by asset class" through `get_instrument_profile` costs one request per holding, and the
answer would still have to be joined client-side against the values the summary just returned.
`asset_class_provenance` travels with the value for the reason `ClassificationFieldRead` carries
it: a `derived` equity read off a provider's `quoteType` and a `manual_override` someone
verified are not equally trustworthy, and a reader that cannot tell them apart cannot know which
of its numbers rest on a guess the provider made.

The value is deliberately not defaulted to something plausible. A provider reports a fund's
wrapper, never what it holds, so every ETF arrives `unclassified` -- reading them as equity
would have made the allocation view look complete while silently misfiling gold and bond funds,
and nothing downstream could have detected it.
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
