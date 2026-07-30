"""Instrument taxonomy and the rules that assign it.

`asset_class` (economic exposure) and `security_type` (legal/structural wrapper) are
deliberately separate axes: GLD is a Commodity Trust that carries Commodity exposure, and a
stablecoin is a Crypto Asset that behaves as a cash equivalent. Collapsing them is what makes
a provider's `quoteType` misreport an ETF as a common stock.

Nothing here guesses from ticker spelling. A symbol we cannot resolve stays UNCLASSIFIED so the
gap is visible downstream rather than silently filled.
"""

from dataclasses import dataclass
from enum import StrEnum


class AssetClass(StrEnum):
    """Economic exposure a holding carries, independent of its legal wrapper."""

    EQUITY = "equity"
    FIXED_INCOME = "fixed_income"
    CASH = "cash"
    CASH_EQUIVALENT = "cash_equivalent"
    COMMODITY = "commodity"
    REAL_ESTATE = "real_estate"
    CRYPTO = "crypto"
    MULTI_ASSET = "multi_asset"
    ALTERNATIVE = "alternative"
    UNCLASSIFIED = "unclassified"


class SecurityType(StrEnum):
    """Legal or structural form of the instrument, independent of its exposure."""

    COMMON_STOCK = "common_stock"
    DEPOSITARY_RECEIPT = "depositary_receipt"
    ETF = "etf"
    MUTUAL_FUND = "mutual_fund"
    BOND = "bond"
    TREASURY = "treasury"
    MONEY_MARKET = "money_market"
    COMMODITY_TRUST = "commodity_trust"
    REIT = "reit"
    CRYPTO_ASSET = "crypto_asset"
    STABLECOIN = "stablecoin"
    CASH = "cash"
    OTHER = "other"
    UNCLASSIFIED = "unclassified"


class Provenance(StrEnum):
    """Where a classification value came from, ordered by trust in `PROVENANCE_RANK`."""

    MANUAL_OVERRIDE = "manual_override"
    VERIFIED_INTERNAL = "verified_internal"
    PROVIDER = "provider"
    DERIVED = "derived"
    UNCLASSIFIED = "unclassified"


# Higher wins. Plan 4.2: manual override > verified internal > provider > derived > unclassified.
PROVENANCE_RANK: dict[Provenance, int] = {
    Provenance.MANUAL_OVERRIDE: 40,
    Provenance.VERIFIED_INTERNAL: 30,
    Provenance.PROVIDER: 20,
    Provenance.DERIVED: 10,
    Provenance.UNCLASSIFIED: 0,
}

CLASSIFICATION_FIELDS = frozenset(
    {
        "asset_class",
        "security_type",
        "sub_asset_class",
        "country_of_risk",
        "is_cash_equivalent",
    }
)


@dataclass(frozen=True)
class DerivedClassification:
    """A best-effort reading of provider metadata, always at DERIVED trust."""

    asset_class: AssetClass
    security_type: SecurityType
    is_fund: bool
    is_cash_equivalent: bool
    warnings: tuple[str, ...] = ()


# Yahoo `quoteType` is the only structural signal the current provider gives us. It distinguishes
# funds from equities but says nothing about what a fund holds, so it maps to security_type and
# only a provisional asset_class.
_QUOTE_TYPE_MAP: dict[str, tuple[AssetClass, SecurityType, bool]] = {
    "EQUITY": (AssetClass.EQUITY, SecurityType.COMMON_STOCK, False),
    "ETF": (AssetClass.UNCLASSIFIED, SecurityType.ETF, True),
    "MUTUALFUND": (AssetClass.UNCLASSIFIED, SecurityType.MUTUAL_FUND, True),
    "CRYPTOCURRENCY": (AssetClass.CRYPTO, SecurityType.CRYPTO_ASSET, False),
    "CURRENCY": (AssetClass.CASH, SecurityType.CASH, False),
    "INDEX": (AssetClass.UNCLASSIFIED, SecurityType.OTHER, False),
    "FUTURE": (AssetClass.COMMODITY, SecurityType.OTHER, False),
}


def derive_from_provider(
    quote_type: str | None,
    *,
    ticker: str,
) -> DerivedClassification:
    """Read provider metadata into the taxonomy without inventing what it does not say.

    An ETF's `asset_class` stays UNCLASSIFIED here on purpose: `quoteType` tells us the wrapper,
    not the underlying exposure. Naming a fund's asset class requires a verified internal mapping
    or a manual override, which outrank this result.
    """
    normalized = (quote_type or "").strip().upper()
    warnings: list[str] = []

    if not normalized:
        return DerivedClassification(
            asset_class=AssetClass.UNCLASSIFIED,
            security_type=SecurityType.UNCLASSIFIED,
            is_fund=False,
            is_cash_equivalent=False,
            warnings=(
                f"Provider returned no quoteType for {ticker}; classification is unresolved",
            ),
        )

    mapped = _QUOTE_TYPE_MAP.get(normalized)
    if mapped is None:
        return DerivedClassification(
            asset_class=AssetClass.UNCLASSIFIED,
            security_type=SecurityType.UNCLASSIFIED,
            is_fund=False,
            is_cash_equivalent=False,
            warnings=(
                f"Provider quoteType {normalized!r} for {ticker} is not in the taxonomy; "
                "classification is unresolved",
            ),
        )

    asset_class, security_type, is_fund = mapped
    if is_fund:
        warnings.append(
            f"{ticker} is a {security_type.value}; provider metadata does not state its "
            "underlying asset class, so it requires a verified mapping or manual override"
        )
    return DerivedClassification(
        asset_class=asset_class,
        security_type=security_type,
        is_fund=is_fund,
        is_cash_equivalent=False,
        warnings=tuple(warnings),
    )
