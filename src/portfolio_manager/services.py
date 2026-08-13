import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .errors import DomainError, not_found
from .identity import YAHOO_PROVIDER, apply_provider_classification, ensure_alias
from .market import (
    HistoryAdjustment,
    HistoryInterval,
    HistoryResult,
    MarketDataError,
    MarketProvider,
    MarketSnapshot,
)
from .models import (
    CashBalance,
    CorporateActionApplication,
    Instrument,
    JournalEvent,
    Portfolio,
    Position,
    PositionTag,
    QuoteCache,
)
from .schemas import (
    CashPositionRead,
    IndicatorsRead,
    MarketInstrumentRead,
    PortfolioCreate,
    PortfolioRead,
    PortfolioSummary,
    PositionRead,
    QuoteRead,
    TechnicalSnapshotRead,
    utc_now,
)
from .sessions import quote_ttl_for
from .technical import bars_frame, calculate_technical, event_metrics, relative_returns

ZERO = Decimal("0")
HUNDRED = Decimal("100")


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _fingerprint(payload: dict[str, object]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _decimal_string(value: Decimal) -> str:
    return format(value.normalize(), "f")


def get_portfolio(session: Session, portfolio_id: str) -> Portfolio:
    portfolio = session.get(Portfolio, portfolio_id)
    if portfolio is None:
        raise not_found("portfolio", portfolio_id)
    return portfolio


def create_portfolio(session: Session, data: PortfolioCreate) -> Portfolio:
    now = utc_now()
    portfolio = Portfolio(
        id=str(uuid4()), name=data.name, base_currency=data.base_currency, created_at=now
    )
    session.add(portfolio)
    try:
        session.flush()
        session.add(CashBalance(portfolio_id=portfolio.id, amount=ZERO, updated_at=now))
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise DomainError(
            409,
            "portfolio_name_exists",
            "A portfolio with this name already exists",
            {"name": data.name},
        ) from exc
    return portfolio


def delete_portfolio(session: Session, portfolio_id: str) -> None:
    """Delete a portfolio and everything recorded against it.

    Most children cascade from `portfolios.id`, but two references into
    `journal_events` are deliberately `RESTRICT` so that a posted event can never
    be deleted out from under the record that cites it: a corporate-action
    application points at the event it posted, and a reversal points at the event
    it undoes. A plain cascade therefore trips a foreign-key error the moment the
    portfolio has either. Clear those references first, in dependency order, so
    the cascade has nothing left to trip over. Deleting the whole portfolio is the
    one case where dropping the audit trail is the intent.
    """
    portfolio = get_portfolio(session, portfolio_id)

    session.execute(
        delete(CorporateActionApplication).where(
            CorporateActionApplication.portfolio_id == portfolio_id
        )
    )
    # Legs cascade from their event, but a reversal must go before the event it
    # undoes. Deleting newest-first satisfies that without walking the chain,
    # since a reversal is always recorded after its target.
    for event_id in session.scalars(
        select(JournalEvent.id)
        .where(JournalEvent.portfolio_id == portfolio_id)
        .order_by(JournalEvent.created_at.desc(), JournalEvent.id.desc())
    ).all():
        session.execute(delete(JournalEvent).where(JournalEvent.id == event_id))

    session.delete(portfolio)
    session.commit()


@dataclass(frozen=True)
class MarketState:
    instrument: Instrument
    quote: QuoteCache
    stale: bool
    warnings: list[str]


class MarketService:
    def __init__(self, session: Session, provider: MarketProvider, ttl_seconds: int) -> None:
        self.session = session
        self.provider = provider
        self.ttl = timedelta(seconds=ttl_seconds)

    def _ttl_for(self, instrument: Instrument, now: datetime) -> timedelta:
        """A quote is fresh for as long as its price cannot move.

        Within a session that is the configured TTL; outside one it runs to the next open, since
        a closed market's last trade stays the last trade. This is a cache-lifetime decision
        only -- a hit here is a genuinely current price, so it is never marked stale.
        """
        return timedelta(
            seconds=quote_ttl_for(instrument.market, now, int(self.ttl.total_seconds()))
        )

    def get(self, ticker: str) -> MarketState:
        symbol = ticker.strip().upper()
        instrument = self.session.get(Instrument, symbol)
        cached = self.session.get(QuoteCache, symbol)
        now = utc_now()
        if (
            instrument is not None
            and cached is not None
            and now - _aware(cached.fetched_at) <= self._ttl_for(instrument, now)
        ):
            return MarketState(instrument, cached, False, [])

        try:
            snapshot = self.provider.fetch(symbol)
        except MarketDataError as exc:
            if instrument is not None and cached is not None:
                return MarketState(
                    instrument,
                    cached,
                    True,
                    [f"Market refresh failed; cached quote returned: {exc}"],
                )
            raise DomainError(
                503,
                "market_data_unavailable",
                "Market data is unavailable and no cached quote exists",
                {"ticker": symbol},
            ) from exc

        instrument = self._save_instrument(snapshot, now)
        self.session.flush()
        cached = self._save_quote(snapshot, now)
        self.session.flush()
        return MarketState(instrument, cached, False, [])

    def history(
        self,
        ticker: str,
        days: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        interval: HistoryInterval = HistoryInterval.DAILY,
        adjustment: HistoryAdjustment = HistoryAdjustment.AUTO,
    ) -> HistoryResult:
        symbol = ticker.strip().upper()
        try:
            return self.provider.history(
                symbol,
                days=days,
                start_date=start_date,
                end_date=end_date,
                interval=interval,
                adjustment=adjustment,
            )
        except MarketDataError as exc:
            raise DomainError(
                503,
                "market_data_unavailable",
                "Historical market data is unavailable",
                {
                    "ticker": symbol,
                    "requested_period": {
                        "days": days,
                        "start_date": start_date.isoformat() if start_date else None,
                        "end_date": end_date.isoformat() if end_date else None,
                    },
                    "available_observations": 0,
                },
            ) from exc

    def technical_snapshot(
        self,
        ticker: str,
        as_of: date | None,
        benchmark: str | None,
        event_date: date | None,
        lookback_years: int,
    ) -> TechnicalSnapshotRead:
        symbol = ticker.strip().upper()
        requested_end = as_of
        reference = as_of or datetime.now(UTC).date()
        start_date = reference - timedelta(days=lookback_years * 366)
        try:
            history = self.history(
                symbol,
                start_date=start_date,
                end_date=requested_end,
                interval=HistoryInterval.DAILY,
                adjustment=HistoryAdjustment.AUTO,
            )
        except DomainError as exc:
            details = {
                **exc.details,
                "as_of": as_of.isoformat() if as_of else None,
                "benchmark": benchmark.strip().upper() if benchmark else None,
                "event_date": event_date.isoformat() if event_date else None,
            }
            raise DomainError(exc.status_code, exc.code, exc.message, details) from exc
        frame = bars_frame(history.bars, as_of)
        if frame.empty:
            raise DomainError(
                503,
                "market_data_unavailable",
                "Historical market data is unavailable",
                {
                    "ticker": symbol,
                    "as_of": as_of.isoformat() if as_of else None,
                    "requested_period": {
                        "start_date": start_date.isoformat(),
                        "end_date": as_of.isoformat() if as_of else None,
                    },
                    "available_observations": 0,
                },
            )

        analysis = calculate_technical(frame)
        warnings = list(history.warnings) + analysis.warnings
        relative = None
        if benchmark:
            benchmark_symbol = benchmark.strip().upper()
            try:
                benchmark_history = self.history(
                    benchmark_symbol,
                    start_date=start_date,
                    end_date=requested_end,
                    interval=HistoryInterval.DAILY,
                    adjustment=HistoryAdjustment.AUTO,
                )
                benchmark_frame = bars_frame(benchmark_history.bars, as_of)
                relative_values, relative_warnings = relative_returns(frame, benchmark_frame)
                common_count = len(frame.index.intersection(benchmark_frame.index))
                relative = {
                    "benchmark": benchmark_symbol,
                    "common_observation_count": common_count,
                    **relative_values,
                }
                warnings.extend(
                    f"Benchmark {benchmark_symbol}: {warning}" for warning in relative_warnings
                )
            except DomainError:
                relative = {
                    "benchmark": benchmark_symbol,
                    "common_observation_count": 0,
                    **{f"return_{period}d_percent": None for period in (20, 60, 120, 252)},
                }
                warnings.append(
                    f"Benchmark {benchmark_symbol} data is unavailable; relative returns are null"
                )

        event = None
        if event_date:
            event, event_warnings = event_metrics(frame, event_date)
            warnings.extend(
                f"Event {event_date.isoformat()}: {warning}" for warning in event_warnings
            )

        actual_start = frame.index[0].date()
        actual_end = frame.index[-1].date()
        return TechnicalSnapshotRead(
            ticker=symbol,
            provider=history.provider,
            as_of=actual_end,
            interval=history.interval,
            adjustment=history.adjustment,
            actual_start_date=actual_start,
            actual_end_date=actual_end,
            bar_count=len(frame),
            trend=analysis.trend,
            momentum=analysis.momentum,
            volatility=analysis.volatility,
            volume=analysis.volume,
            relative_strength=relative,
            event_analysis=event,
            warnings=warnings,
        )

    def _save_instrument(self, snapshot: MarketSnapshot, now: datetime) -> Instrument:
        data = snapshot.instrument
        instrument = self.session.get(Instrument, data.ticker)
        if instrument is None:
            instrument = Instrument(
                ticker=data.ticker,
                instrument_id=str(uuid4()),
                name=data.name,
                asset_type=data.asset_type,
                market=data.market,
                exchange=data.exchange,
                currency=data.currency,
                is_fund=False,
                active_from=now,
                created_at=now,
                updated_at=now,
            )
            self.session.add(instrument)
            self.session.flush()
            ensure_alias(self.session, instrument, YAHOO_PROVIDER, data.ticker)
        else:
            instrument.name = data.name
            instrument.asset_type = data.asset_type
            instrument.market = data.market
            instrument.exchange = data.exchange
            instrument.currency = data.currency
            instrument.updated_at = now
        # Refreshes only the DERIVED rank; manual overrides and verified mappings still win.
        apply_provider_classification(self.session, instrument, data.quote_type)
        return instrument

    def _save_quote(self, snapshot: MarketSnapshot, now: datetime) -> QuoteCache:
        data = snapshot.quote
        quote = self.session.get(QuoteCache, snapshot.instrument.ticker)
        values = {
            "price": data.price,
            "open": data.open,
            "high": data.high,
            "low": data.low,
            "previous_close": data.previous_close,
            "volume": data.volume,
            "change": data.change,
            "change_percent": data.change_percent,
            "market_cap": data.market_cap,
            "year_high": data.year_high,
            "year_low": data.year_low,
            "sma20": data.sma20,
            "sma50": data.sma50,
            "rsi14": data.rsi14,
            "macd": data.macd,
            "macd_signal": data.macd_signal,
            "macd_histogram": data.macd_histogram,
            "provider_as_of": data.provider_as_of,
            "indicators_as_of": data.indicators_as_of,
            "fetched_at": now,
        }
        if quote is None:
            quote = QuoteCache(ticker=snapshot.instrument.ticker, **values)
            self.session.add(quote)
        else:
            for key, value in values.items():
                setattr(quote, key, value)
        return quote


def market_response(state: MarketState) -> MarketInstrumentRead:
    quote = state.quote
    return MarketInstrumentRead(
        ticker=state.instrument.ticker,
        name=state.instrument.name,
        asset_type=state.instrument.asset_type,
        market=state.instrument.market,
        exchange=state.instrument.exchange,
        currency=state.instrument.currency,
        quote=QuoteRead(
            price=quote.price,
            open=quote.open,
            high=quote.high,
            low=quote.low,
            previous_close=quote.previous_close,
            volume=quote.volume,
            change=quote.change,
            change_percent=quote.change_percent,
            market_cap=quote.market_cap,
            year_high=quote.year_high,
            year_low=quote.year_low,
            provider_as_of=_aware(quote.provider_as_of),
            fetched_at=_aware(quote.fetched_at),
            stale=state.stale,
        ),
        indicators=IndicatorsRead(
            sma20=quote.sma20,
            sma50=quote.sma50,
            rsi14=quote.rsi14,
            macd=quote.macd,
            macd_signal=quote.macd_signal,
            macd_histogram=quote.macd_histogram,
            calculated_as_of=_aware(quote.indicators_as_of),
        ),
        warnings=state.warnings,
    )


def normalize_tag(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    if not normalized or len(normalized) > 50:
        raise DomainError(
            422,
            "invalid_tag",
            "Tags must contain between 1 and 50 characters",
            {"tag": value},
        )
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise DomainError(
            422,
            "invalid_tag",
            "Tags cannot contain control characters",
            {"tag": value},
        )
    return normalized


def replace_tags(
    session: Session, portfolio_id: str, ticker: str, raw_tags: list[str]
) -> list[str]:
    get_portfolio(session, portfolio_id)
    symbol = ticker.strip().upper()
    position = session.get(Position, (portfolio_id, symbol))
    if position is None or position.quantity <= ZERO:
        raise not_found("position", symbol)
    tags = sorted({normalize_tag(tag) for tag in raw_tags})
    current = session.scalars(
        select(PositionTag).where(
            PositionTag.portfolio_id == portfolio_id, PositionTag.ticker == symbol
        )
    ).all()
    for tag in current:
        session.delete(tag)
    session.flush()
    session.add_all(
        [PositionTag(portfolio_id=portfolio_id, ticker=symbol, tag=tag) for tag in tags]
    )
    session.commit()
    return tags


def _tags_for(session: Session, portfolio_id: str) -> dict[str, list[str]]:
    rows = session.execute(
        select(PositionTag.ticker, PositionTag.tag)
        .where(PositionTag.portfolio_id == portfolio_id)
        .order_by(PositionTag.tag)
    ).all()
    result: dict[str, list[str]] = {}
    for ticker, tag in rows:
        result.setdefault(ticker, []).append(tag)
    return result


def build_summary(
    session: Session, market: MarketService, portfolio_id: str
) -> PortfolioSummary:
    portfolio = get_portfolio(session, portfolio_id)
    positions = session.scalars(
        select(Position).where(Position.portfolio_id == portfolio_id).order_by(Position.ticker)
    ).all()
    tags_by_ticker = _tags_for(session, portfolio_id)
    active: list[PositionRead] = []
    warnings: list[str] = []
    securities_value = ZERO
    unrealized_total = ZERO

    for position in positions:
        if position.quantity <= ZERO:
            continue
        state = market.get(position.ticker)
        quote = state.quote
        value = position.quantity * quote.price
        unrealized = position.quantity * (quote.price - position.average_cost)
        percent = (
            (quote.price - position.average_cost) / position.average_cost * HUNDRED
            if position.average_cost != ZERO
            else None
        )
        securities_value += value
        unrealized_total += unrealized
        warnings.extend(state.warnings)
        active.append(
            PositionRead(
                ticker=position.ticker,
                name=state.instrument.name,
                asset_type=state.instrument.asset_type,
                market=state.instrument.market,
                currency=state.instrument.currency,
                quantity=position.quantity,
                average_cost=position.average_cost,
                current_price=quote.price,
                market_value=value,
                realized_pnl=position.realized_pnl,
                unrealized_pnl=unrealized,
                total_pnl=position.realized_pnl + unrealized,
                unrealized_pnl_percent=percent,
                weight_percent=None,
                tags=tags_by_ticker.get(position.ticker, []),
                price_as_of=_aware(quote.provider_as_of),
                price_fetched_at=_aware(quote.fetched_at),
                price_stale=state.stale,
            )
        )

    cash_balance = session.get(CashBalance, portfolio_id)
    cash_value = cash_balance.amount if cash_balance else ZERO
    total_value = securities_value + cash_value
    if total_value != ZERO:
        active = [
            item.model_copy(
                update={"weight_percent": item.market_value / total_value * HUNDRED}
            )
            for item in active
        ]
        cash_weight = cash_value / total_value * HUNDRED
    else:
        cash_weight = None

    realized_total = sum((position.realized_pnl for position in positions), start=ZERO)
    session.commit()
    return PortfolioSummary(
        portfolio=PortfolioRead.model_validate(portfolio),
        positions=active,
        cash=CashPositionRead(
            currency=portfolio.base_currency,
            amount=cash_value,
            weight_percent=cash_weight,
            updated_at=_aware(cash_balance.updated_at) if cash_balance else None,
        ),
        securities_value=securities_value,
        cash_value=cash_value,
        total_value=total_value,
        realized_pnl=realized_total,
        unrealized_pnl=unrealized_total,
        total_pnl=realized_total + unrealized_total,
        valuation_as_of=utc_now(),
        warnings=list(dict.fromkeys(warnings)),
    )
