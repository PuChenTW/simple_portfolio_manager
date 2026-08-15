"""Reporting several portfolios together in one currency.

Holdings priced in different currencies cannot simply be added. This converts each portfolio's
positions and cash into a single reporting currency and reports what that took: the rate used for
every currency, how each rate was derived, and how much value could not be converted at all.

The last part matters most. When a pair cannot be resolved, the amount is not converted at a
guessed rate and not silently dropped -- it stays in its own currency, is excluded from the
converted total, and appears in `unconverted` with the coverage percentage. A total that quietly
omitted it would look complete while understating the portfolio, and nobody reading it could
tell.

Group membership is effective-dated, so a report for a past date includes the portfolios that
were in the group then, not the ones in it now.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .errors import DomainError, not_found
from .fx import Conversion, FxService, FxUnavailable
from .identity import resolve_field_for_many
from .journal import PortfolioKind
from .market import MarketProvider
from .models import (
    Instrument,
    Portfolio,
    PortfolioGroup,
    PortfolioGroupMember,
)
from .schemas import utc_now
from .services import ZERO, get_portfolio
from .taxonomy import AssetClass, Provenance
from .valuation import HistoricalPricer

HUNDRED = Decimal("100")


@dataclass
class ConsolidatedPosition:
    """One holding, in both its own currency and the reporting currency."""

    portfolio_id: str
    portfolio_name: str
    instrument_id: str | None
    ticker: str
    issuer_id: str | None
    quantity: Decimal
    average_cost: Decimal
    local_currency: str
    local_price: Decimal | None
    local_market_value: Decimal | None
    reporting_market_value: Decimal | None
    fx_rate: Decimal | None
    fx_method: str | None
    fx_path: list[str]
    fx_as_of: datetime | None
    weight_percent: Decimal | None
    # The economic exposure behind the holding, not its legal wrapper: an ETF's asset class is
    # what it holds, which provider metadata never states. Stays `unclassified` until someone
    # resolves it, so a gap in the allocation view is visible rather than absorbed into equity.
    asset_class: str = AssetClass.UNCLASSIFIED.value
    asset_class_provenance: str = Provenance.UNCLASSIFIED.value
    warnings: list[str] = field(default_factory=list)


@dataclass
class CurrencyTotal:
    currency: str
    local_amount: Decimal
    reporting_amount: Decimal | None


@dataclass
class IssuerExposure:
    """Economic exposure to one issuer, aggregated across listings and portfolios."""

    issuer_id: str
    issuer_name: str
    reporting_value: Decimal
    weight_percent: Decimal | None
    tickers: list[str]


@dataclass
class UnconvertedAmount:
    currency: str
    amount: Decimal
    reason: str


@dataclass
class ConsolidatedSummary:
    group_id: str
    group_name: str
    reporting_currency: str
    as_of: date
    portfolio_ids: list[str]
    positions: list[ConsolidatedPosition]
    cash_by_currency: list[CurrencyTotal]
    currency_exposure: list[CurrencyTotal]
    issuer_exposure: list[IssuerExposure]
    securities_value: Decimal
    cash_value: Decimal
    total_value: Decimal
    # `total_value` was always the net figure -- every total here is a plain signed sum, so a
    # liability member subtracted correctly before these three existed. They split that one
    # number into what is owned and what is owed, because a net worth alone cannot distinguish
    # holding 5M in cash from holding 15M against a 10M loan.
    assets_value: Decimal
    liabilities_value: Decimal
    net_value: Decimal
    unconverted: list[UnconvertedAmount]
    converted_value_coverage_percent: Decimal
    fx_rates_used: list[Conversion]
    calculation_method: str
    warnings: list[str] = field(default_factory=list)


CALCULATION_METHOD = (
    "Each portfolio's holdings and cash are valued in their own currency, then converted to the "
    "reporting currency at the rate in force on the as-of date. Rates never come from after that "
    "date. An amount whose currency pair cannot be resolved is excluded from the converted "
    "totals and reported under `unconverted` rather than being converted at a guessed rate."
)


def create_group(
    session: Session,
    name: str,
    reporting_currency: str,
    portfolio_ids: list[str],
    *,
    effective_from: datetime | None = None,
) -> PortfolioGroup:
    """Create a group and open a membership interval for each portfolio.

    `effective_from` defaults to each portfolio's own inception rather than now, so a group
    created today can report on the history its portfolios already have. Grouping is a reporting
    decision about existing data, not an event that changes when that data began.
    """
    if not portfolio_ids:
        raise DomainError(
            422, "empty_group", "A group needs at least one portfolio", {"name": name}
        )
    portfolios = [get_portfolio(session, portfolio_id) for portfolio_id in portfolio_ids]

    now = utc_now()
    starts = {
        portfolio.id: effective_from or _aware(portfolio.created_at)
        for portfolio in portfolios
    }
    group = PortfolioGroup(
        id=str(uuid.uuid4()),
        name=name,
        reporting_currency=reporting_currency.upper(),
        created_at=now,
        updated_at=now,
    )
    session.add(group)
    for portfolio_id in dict.fromkeys(portfolio_ids):  # de-duplicate, preserve order
        session.add(
            PortfolioGroupMember(
                id=str(uuid.uuid4()),
                group_id=group.id,
                portfolio_id=portfolio_id,
                effective_from=starts[portfolio_id],
                effective_to=None,
                created_at=now,
            )
        )
    session.commit()
    return group


def rename_group(session: Session, group_id: str, name: str) -> PortfolioGroup:
    """Retitle a group.

    Only the label moves. `reporting_currency` is fixed at creation: every stored consolidation
    was converted into it, so changing it would reinterpret those totals rather than rename them.
    Group names are not unique, so a rename has no collision to report.
    """
    group = get_group(session, group_id)
    group.name = name
    group.updated_at = utc_now()
    session.commit()
    session.refresh(group)
    return group


def replace_members(
    session: Session, group_id: str, portfolio_ids: list[str]
) -> PortfolioGroup:
    """Set the group's membership, closing intervals rather than deleting them.

    A portfolio dropped today stays in yesterday's report, because its interval is closed rather
    than removed. Editing membership in place would silently restate every past consolidation.
    """
    group = get_group(session, group_id)
    for portfolio_id in portfolio_ids:
        get_portfolio(session, portfolio_id)

    now = utc_now()
    wanted = set(portfolio_ids)
    current = {
        member.portfolio_id: member
        for member in _members_at(session, group_id, now)
    }

    for portfolio_id, member in current.items():
        if portfolio_id not in wanted:
            member.effective_to = now
    for portfolio_id in portfolio_ids:
        if portfolio_id not in current:
            session.add(
                PortfolioGroupMember(
                    id=str(uuid.uuid4()),
                    group_id=group_id,
                    portfolio_id=portfolio_id,
                    effective_from=now,
                    effective_to=None,
                    created_at=now,
                )
            )
    group.updated_at = now
    session.commit()
    return group


def delete_group(session: Session, group_id: str) -> None:
    """Remove a group and its membership intervals.

    Safe to delete outright, unlike a posted event: a group is a reporting lens over portfolios
    that exist independently of it. Nothing recorded is lost -- the portfolios, their journals,
    and their snapshots are untouched, and the same group can be recreated with the same members.
    The membership rows go with it because they describe only this grouping.
    """
    session.delete(get_group(session, group_id))
    session.commit()


def member_portfolio_ids(session: Session, group_id: str, as_of: date) -> list[str]:
    """Portfolios that were in the group on a date."""
    moment = _end_of_day(as_of)
    return [
        member.portfolio_id for member in _members_at(session, group_id, moment)
    ]


def build_consolidated_summary(
    session: Session,
    group_id: str,
    provider: MarketProvider,
    *,
    as_of: date | None = None,
    reporting_currency: str | None = None,
) -> ConsolidatedSummary:
    """Value every portfolio in the group and express the result in one currency."""
    group = get_group(session, group_id)
    valuation_date = as_of or utc_now().date()
    if valuation_date > utc_now().date():
        raise DomainError(
            422,
            "valuation_date_in_future",
            "A group cannot be valued on a date that has not happened",
            {"as_of": valuation_date.isoformat()},
        )

    currency = (reporting_currency or group.reporting_currency).upper()
    portfolio_ids = member_portfolio_ids(session, group_id, valuation_date)
    warnings: list[str] = []
    if not portfolio_ids:
        warnings.append(
            f"No portfolio was a member of this group on {valuation_date.isoformat()}"
        )

    fx = FxService(session, provider)
    pricer = HistoricalPricer(provider)
    rates: dict[str, Conversion] = {}
    positions: list[ConsolidatedPosition] = []
    cash_local: dict[str, Decimal] = {}
    # The liability share of the same cash, kept per currency so it converts by the same path.
    # Splitting after conversion would have to guess which currency the debt was in.
    debt_local: dict[str, Decimal] = {}
    unconverted: list[UnconvertedAmount] = []

    securities_value = ZERO
    unconverted_value = ZERO

    for portfolio_id in portfolio_ids:
        portfolio = session.get(Portfolio, portfolio_id)
        if portfolio is None:
            continue
        state = _state_of(session, portfolio_id, valuation_date)
        cash_local[portfolio.base_currency] = (
            cash_local.get(portfolio.base_currency, ZERO) + state.cash
        )
        if portfolio.kind == PortfolioKind.LIABILITY.value:
            debt_local[portfolio.base_currency] = (
                debt_local.get(portfolio.base_currency, ZERO) + state.cash
            )

        for holding in state.positions:
            if holding.quantity <= ZERO:
                continue
            row, converted, missing = _consolidate_position(
                session, portfolio, holding, pricer, fx, currency, valuation_date, rates
            )
            positions.append(row)
            if converted is not None:
                securities_value += converted
            if missing is not None:
                unconverted_value += missing.amount
                unconverted.append(missing)

    # One query for every holding's asset class, not one per row. Applied after the loop because
    # the instrument ids are only all known once it finishes.
    _apply_asset_class(session, positions)

    before_cash = len(unconverted)
    cash_value, cash_totals = _consolidate_cash(
        fx, cash_local, currency, valuation_date, rates, unconverted
    )
    # Unconvertible cash counts toward the shortfall exactly like unconvertible holdings.
    unconverted_value += sum(
        (item.amount for item in unconverted[before_cash:]), start=ZERO
    )

    total_value = securities_value + cash_value
    # Reuses the rates resolved above, so a liability converts exactly as its cash already did
    # and the split can never disagree with the total it came from.
    liabilities_value = _converted_debt(fx, debt_local, currency, valuation_date, rates)
    coverage = _coverage(total_value, unconverted_value)
    _describe(warnings, unconverted, rates, coverage)

    return ConsolidatedSummary(
        group_id=group.id,
        group_name=group.name,
        reporting_currency=currency,
        as_of=valuation_date,
        portfolio_ids=portfolio_ids,
        positions=_weighted(positions, total_value),
        cash_by_currency=cash_totals,
        currency_exposure=_currency_exposure(positions, cash_totals, currency),
        issuer_exposure=_issuer_exposure(session, positions, total_value),
        securities_value=securities_value,
        cash_value=cash_value,
        total_value=total_value,
        # Assets are what is left after the debt is taken back out, so the three always satisfy
        # assets + liabilities == net. Liabilities stay negative rather than being flipped to a
        # magnitude: every other figure here is signed, and one that is not invites a reader to
        # add where they should subtract.
        assets_value=total_value - liabilities_value,
        liabilities_value=liabilities_value,
        net_value=total_value,
        unconverted=unconverted,
        converted_value_coverage_percent=coverage,
        fx_rates_used=sorted(rates.values(), key=lambda item: item.base_currency),
        calculation_method=CALCULATION_METHOD,
        warnings=warnings,
    )


def _apply_asset_class(session: Session, positions: list[ConsolidatedPosition]) -> None:
    """Fill each row's asset class from the winning classification, in one query.

    A row whose instrument is unknown, or whose asset class nobody has resolved, keeps the
    `unclassified` default it was constructed with. That is the honest answer: the provider
    reports an ETF's wrapper, never what it holds, so defaulting a fund to equity here would
    invent an exposure and make the gap unfixable because nothing would show it exists.
    """
    resolved = resolve_field_for_many(
        session,
        [row.instrument_id for row in positions if row.instrument_id],
        field="asset_class",
    )
    for row in positions:
        winner = resolved.get(row.instrument_id) if row.instrument_id else None
        if winner is None or not winner.value:
            continue
        row.asset_class = winner.value
        row.asset_class_provenance = winner.provenance.value


def _consolidate_position(
    session: Session,
    portfolio: Portfolio,
    holding,
    pricer: HistoricalPricer,
    fx: FxService,
    currency: str,
    valuation_date: date,
    rates: dict[str, Conversion],
) -> tuple[ConsolidatedPosition, Decimal | None, UnconvertedAmount | None]:
    instrument = session.get(Instrument, holding.ticker)
    local_currency = instrument.currency if instrument else portfolio.base_currency
    price = pricer.price_at(holding.ticker, valuation_date)
    warnings: list[str] = []

    local_value = holding.quantity * price.price if price else None
    if price is None:
        warnings.append(
            f"{holding.ticker} could not be priced on {valuation_date.isoformat()}, so it is "
            "excluded from the totals rather than valued at zero"
        )

    conversion = _rate_for(fx, local_currency, currency, valuation_date, rates)
    reporting_value: Decimal | None = None
    missing: UnconvertedAmount | None = None

    if local_value is not None:
        if isinstance(conversion, FxUnavailable):
            warnings.append(conversion.reason)
            missing = UnconvertedAmount(
                currency=local_currency, amount=local_value, reason=conversion.reason
            )
        else:
            reporting_value = conversion.apply(local_value)
            warnings.extend(conversion.warnings)

    row = ConsolidatedPosition(
        portfolio_id=portfolio.id,
        portfolio_name=portfolio.name,
        instrument_id=instrument.instrument_id if instrument else None,
        ticker=holding.ticker,
        issuer_id=instrument.issuer_id if instrument else None,
        quantity=holding.quantity,
        average_cost=holding.average_cost,
        local_currency=local_currency,
        local_price=price.price if price else None,
        local_market_value=local_value,
        reporting_market_value=reporting_value,
        fx_rate=None if isinstance(conversion, FxUnavailable) else conversion.rate,
        fx_method=None if isinstance(conversion, FxUnavailable) else conversion.method,
        fx_path=[] if isinstance(conversion, FxUnavailable) else conversion.conversion_path,
        fx_as_of=None if isinstance(conversion, FxUnavailable) else conversion.price_as_of,
        weight_percent=None,
        warnings=warnings,
    )
    return row, reporting_value, missing


def _consolidate_cash(
    fx: FxService,
    cash_local: dict[str, Decimal],
    currency: str,
    valuation_date: date,
    rates: dict[str, Conversion],
    unconverted: list[UnconvertedAmount],
) -> tuple[Decimal, list[CurrencyTotal]]:
    total = ZERO
    rows: list[CurrencyTotal] = []
    for local_currency, amount in sorted(cash_local.items()):
        conversion = _rate_for(fx, local_currency, currency, valuation_date, rates)
        if isinstance(conversion, FxUnavailable):
            rows.append(CurrencyTotal(local_currency, amount, None))
            if amount != ZERO:
                unconverted.append(
                    UnconvertedAmount(local_currency, amount, conversion.reason)
                )
            continue
        converted = conversion.apply(amount)
        total += converted
        rows.append(CurrencyTotal(local_currency, amount, converted))
    return total, rows


def _converted_debt(
    fx: FxService,
    debt_local: dict[str, Decimal],
    currency: str,
    valuation_date: date,
    rates: dict[str, Conversion],
) -> Decimal:
    """The liability share of cash, in the reporting currency.

    Every rate needed here was already resolved while converting the same balances as cash, so
    this reads from the cache and cannot reach a different answer. A pair that was unavailable
    then is skipped now for the same reason -- it was already excluded from `total_value` and
    listed under `unconverted`, and converting it here would put a guessed figure into the split
    of a total that never contained it.
    """
    total = ZERO
    for local_currency, amount in sorted(debt_local.items()):
        conversion = _rate_for(fx, local_currency, currency, valuation_date, rates)
        if isinstance(conversion, FxUnavailable):
            continue
        total += conversion.apply(amount)
    return total


def _rate_for(
    fx: FxService,
    base: str,
    quote: str,
    valuation_date: date,
    rates: dict[str, Conversion],
) -> Conversion | FxUnavailable:
    """Resolve once per currency so one report uses one rate throughout."""
    if base in rates:
        return rates[base]
    resolved = fx.convert(base, quote, valuation_date)
    if isinstance(resolved, Conversion):
        rates[base] = resolved
    return resolved


def _weighted(
    positions: list[ConsolidatedPosition], total: Decimal
) -> list[ConsolidatedPosition]:
    """Weights are of the converted total, so an unconverted holding has no weight, not zero."""
    for row in positions:
        if row.reporting_market_value is not None and total != ZERO:
            row.weight_percent = row.reporting_market_value / total * HUNDRED
    return sorted(
        positions,
        key=lambda item: (item.reporting_market_value or ZERO),
        reverse=True,
    )


def _currency_exposure(
    positions: list[ConsolidatedPosition],
    cash_totals: list[CurrencyTotal],
    reporting: str,
) -> list[CurrencyTotal]:
    local: dict[str, Decimal] = {}
    converted: dict[str, Decimal] = {}
    for row in positions:
        if row.local_market_value is None:
            continue
        local[row.local_currency] = local.get(row.local_currency, ZERO) + row.local_market_value
        if row.reporting_market_value is not None:
            converted[row.local_currency] = (
                converted.get(row.local_currency, ZERO) + row.reporting_market_value
            )
    for item in cash_totals:
        local[item.currency] = local.get(item.currency, ZERO) + item.local_amount
        if item.reporting_amount is not None:
            converted[item.currency] = (
                converted.get(item.currency, ZERO) + item.reporting_amount
            )
    return [
        CurrencyTotal(currency, amount, converted.get(currency))
        for currency, amount in sorted(local.items())
    ]


def _issuer_exposure(
    session: Session, positions: list[ConsolidatedPosition], total: Decimal
) -> list[IssuerExposure]:
    """Aggregate by issuer so one company held through several listings reads as one exposure.

    Only holdings with a mapped issuer appear. An unmapped listing is left out rather than
    treated as its own issuer, which would invent an entity that does not exist.
    """
    from .models import Issuer

    grouped: dict[str, tuple[Decimal, list[str]]] = {}
    for row in positions:
        if not row.issuer_id or row.reporting_market_value is None:
            continue
        amount, tickers = grouped.get(row.issuer_id, (ZERO, []))
        grouped[row.issuer_id] = (amount + row.reporting_market_value, [*tickers, row.ticker])

    exposures: list[IssuerExposure] = []
    for issuer_id, (amount, tickers) in grouped.items():
        issuer = session.get(Issuer, issuer_id)
        exposures.append(
            IssuerExposure(
                issuer_id=issuer_id,
                issuer_name=issuer.display_name or issuer.legal_name if issuer else issuer_id,
                reporting_value=amount,
                weight_percent=(amount / total * HUNDRED) if total != ZERO else None,
                tickers=sorted(set(tickers)),
            )
        )
    return sorted(exposures, key=lambda item: item.reporting_value, reverse=True)


def _coverage(total: Decimal, unconverted: Decimal) -> Decimal:
    """Share of value that reached the reporting currency."""
    gross = total + unconverted
    if gross == ZERO:
        return HUNDRED
    return total / gross * HUNDRED


def _describe(
    warnings: list[str],
    unconverted: list[UnconvertedAmount],
    rates: dict[str, Conversion],
    coverage: Decimal,
) -> None:
    if unconverted:
        currencies = sorted({item.currency for item in unconverted})
        warnings.append(
            f"Value in {', '.join(currencies)} could not be converted and is excluded from the "
            f"totals; converted coverage is {coverage:.2f}% of gross value"
        )
    for conversion in rates.values():
        if conversion.is_stale:
            warnings.append(
                f"The {conversion.base_currency}/{conversion.quote_currency} rate is stale: "
                f"it comes from {conversion.price_as_of.date().isoformat()}"
                if conversion.price_as_of
                else f"The {conversion.base_currency} rate has no observation date"
            )


def _state_of(session: Session, portfolio_id: str, valuation_date: date):
    from .replay import replay_state

    return replay_state(session, portfolio_id, _end_of_day(valuation_date))


def _members_at(
    session: Session, group_id: str, moment: datetime
) -> list[PortfolioGroupMember]:
    return list(
        session.scalars(
            select(PortfolioGroupMember)
            .where(
                PortfolioGroupMember.group_id == group_id,
                PortfolioGroupMember.effective_from <= moment,
                or_(
                    PortfolioGroupMember.effective_to.is_(None),
                    PortfolioGroupMember.effective_to > moment,
                ),
            )
            .order_by(PortfolioGroupMember.effective_from)
        ).all()
    )


def get_group(session: Session, group_id: str) -> PortfolioGroup:
    group = session.get(PortfolioGroup, group_id)
    if group is None:
        raise not_found("portfolio_group", group_id)
    return group


def _end_of_day(value: date) -> datetime:
    return datetime.combine(value, time(23, 59, 59, 999999), UTC)


def _aware(value: datetime) -> datetime:
    # SQLite returns naive datetimes even for timezone-aware columns.
    return value if value.tzinfo else value.replace(tzinfo=UTC)
