"""Instrument identity, classification provenance, and issuer mapping.

The cases in `test_*_is_not_reported_as_common_stock` and below come from plan section 4.6: real
instruments the previous coarse `asset_type` field misreported. The rule under test throughout is
that an unresolved classification stays unclassified and visible, and is never guessed.
"""

from portfolio_manager.taxonomy import AssetClass, Provenance, SecurityType


def profile(harness, reference: str) -> dict:
    response = harness.client.get(f"/api/v1/instruments/{reference}/profile")
    assert response.status_code == 200, response.text
    return response.json()


def classification(harness, reference: str, field: str) -> dict | None:
    return profile(harness, reference)["classification"].get(field)


def test_profile_assigns_a_stable_instrument_id_and_provider_alias(harness) -> None:
    first = profile(harness, "AAPL")
    assert first["instrument_id"], "an instrument must receive a stable ID"
    assert first["ticker"] == "AAPL"
    assert [alias["provider_symbol"] for alias in first["aliases"]] == ["AAPL"]

    # The ID must survive later refreshes; downstream records will reference it.
    assert profile(harness, "AAPL")["instrument_id"] == first["instrument_id"]


def test_instrument_resolves_by_ticker_and_by_stable_id(harness) -> None:
    instrument_id = profile(harness, "AAPL")["instrument_id"]
    assert profile(harness, instrument_id)["ticker"] == "AAPL"


def test_unknown_ticker_reports_market_data_unavailable(harness) -> None:
    response = harness.client.get("/api/v1/instruments/NOPE/profile")
    assert response.status_code == 503
    assert response.json()["code"] == "market_data_unavailable"


def test_etfs_are_not_reported_as_common_stock(harness) -> None:
    """VOO, VT, and SOXX are funds; the legacy asset_type field calls all three "stock"."""
    for ticker in ("VOO", "VT", "SOXX"):
        result = profile(harness, ticker)
        security_type = result["classification"]["security_type"]
        assert security_type["value"] == SecurityType.ETF.value, ticker
        assert result["is_fund"] is True, ticker
        # The legacy field is deliberately unchanged for backward compatibility.
        assert result["asset_type"] == "stock", ticker


def test_fund_asset_class_is_unresolved_rather_than_assumed_to_be_equity(harness) -> None:
    """Provider metadata names the wrapper, not the holdings. GLD is the counterexample that
    makes assuming equity wrong: it is an ETF whose exposure is a commodity."""
    result = profile(harness, "GLD")
    assert result["classification"]["security_type"]["value"] == SecurityType.ETF.value
    assert result["classification"]["asset_class"]["value"] == AssetClass.UNCLASSIFIED.value
    assert any(
        "asset class" in warning or "unclassified" in warning
        for warning in result["warnings"]
    )


def test_boxx_classification_is_not_inferred_from_ticker_spelling(harness) -> None:
    """BOXX must be classified from metadata, never from the letters in its symbol."""
    result = profile(harness, "BOXX")
    assert result["classification"]["security_type"]["value"] == SecurityType.ETF.value
    assert result["classification"]["asset_class"]["value"] == AssetClass.UNCLASSIFIED.value


def test_gld_commodity_exposure_is_recorded_as_an_auditable_override(harness) -> None:
    override = harness.client.put(
        "/api/v1/instruments/GLD/classification",
        json={
            "request_id": "gld-1",
            "field": "asset_class",
            "value": AssetClass.COMMODITY.value,
            "reason": "SPDR Gold Shares holds allocated gold bullion",
        },
    )
    assert override.status_code == 200, override.text

    asset_class = classification(harness, "GLD", "asset_class")
    assert asset_class["value"] == AssetClass.COMMODITY.value
    assert asset_class["provenance"] == Provenance.MANUAL_OVERRIDE.value
    assert "bullion" in asset_class["note"]
    # The wrapper is untouched by an exposure override.
    assert classification(harness, "GLD", "security_type")["value"] == SecurityType.ETF.value


def test_override_outranks_provider_without_destroying_it(harness) -> None:
    """Retracting an override must restore the provider's value, proving it was never lost."""
    profile(harness, "VOO")
    harness.client.put(
        "/api/v1/instruments/VOO/classification",
        json={
            "request_id": "voo-1",
            "field": "security_type",
            "value": SecurityType.MUTUAL_FUND.value,
            "reason": "deliberate miscategorization to prove retraction restores provider data",
        },
    )
    overridden = classification(harness, "VOO", "security_type")
    assert overridden["value"] == SecurityType.MUTUAL_FUND.value

    retract = harness.client.put(
        "/api/v1/instruments/VOO/classification",
        json={
            "request_id": "voo-2",
            "field": "security_type",
            "reason": "retracting the test override",
            "retract": True,
        },
    )
    assert retract.status_code == 200, retract.text

    restored = classification(harness, "VOO", "security_type")
    assert restored["value"] == SecurityType.ETF.value
    assert restored["provenance"] == Provenance.DERIVED.value


def test_override_rejects_values_outside_the_taxonomy(harness) -> None:
    profile(harness, "AAPL")
    response = harness.client.put(
        "/api/v1/instruments/AAPL/classification",
        json={
            "request_id": "bad-1",
            "field": "asset_class",
            "value": "definitely_not_a_taxonomy_member",
            "reason": "typo protection",
        },
    )
    assert response.status_code == 422
    assert response.json()["code"] == "invalid_classification_value"


def test_usdt_is_a_stablecoin_holding_crypto_security_type(harness) -> None:
    """A stablecoin may act as a cash equivalent while remaining a crypto asset structurally."""
    result = profile(harness, "USDT-USD")
    assert result["classification"]["security_type"]["value"] == SecurityType.CRYPTO_ASSET.value
    assert result["classification"]["asset_class"]["value"] == AssetClass.CRYPTO.value

    for request_id, field, value in (
        ("usdt-1", "security_type", SecurityType.STABLECOIN.value),
        ("usdt-2", "is_cash_equivalent", "true"),
    ):
        response = harness.client.put(
            "/api/v1/instruments/USDT-USD/classification",
            json={
                "request_id": request_id,
                "field": field,
                "value": value,
                "reason": "Tether is a fiat-referenced stablecoin",
            },
        )
        assert response.status_code == 200, response.text

    final = profile(harness, "USDT-USD")
    assert final["classification"]["security_type"]["value"] == SecurityType.STABLECOIN.value
    assert final["classification"]["is_cash_equivalent"]["value"] == "true"
    # Cash-equivalent role must not erase the crypto exposure.
    assert final["classification"]["asset_class"]["value"] == AssetClass.CRYPTO.value


def test_tsm_and_2330_share_an_issuer_but_stay_separate_instruments(harness) -> None:
    """Cross-listing aggregation happens at the issuer level; the listings never merge."""
    payload = {
        "request_id": "tsmc-1",
        "legal_name": "Taiwan Semiconductor Manufacturing Company Limited",
        "display_name": "TSMC",
        "country_of_domicile": "TW",
    }
    adr = harness.client.put("/api/v1/instruments/TSM/issuer", json=payload)
    assert adr.status_code == 200, adr.text
    local = harness.client.put(
        "/api/v1/instruments/2330.TW/issuer", json={**payload, "request_id": "tsmc-2"}
    )
    assert local.status_code == 200, local.text

    adr_profile = profile(harness, "TSM")
    local_profile = profile(harness, "2330.TW")

    assert adr_profile["issuer"]["id"] == local_profile["issuer"]["id"]
    assert adr_profile["issuer"]["display_name"] == "TSMC"
    # Distinct listings: separate identity, currency, and price.
    assert adr_profile["instrument_id"] != local_profile["instrument_id"]
    assert adr_profile["currency"] == "USD"
    assert local_profile["currency"] == "TWD"


def test_unmapped_issuer_is_reported_as_a_warning(harness) -> None:
    result = profile(harness, "MSFT")
    assert result["issuer"] is None
    assert any("issuer is unmapped" in warning for warning in result["warnings"])
