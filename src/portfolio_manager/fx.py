"""Point-in-time currency conversion.

A consolidated total is only as trustworthy as the rate behind it, so every conversion reports
the rate it used, that rate's own date, the provider, and how it was derived -- directly, by
inverting the opposite pair, or by crossing through an intermediary. A caller can then check the
number rather than take it on faith.

Two rules keep a historical report honest. A conversion for a past date never uses a rate later
than that date, or a backfilled series would convert the past at today's rate and look like it
predicted the currency. And a pair that cannot be resolved yields no number at all: the amount
stays in its own currency and is excluded from the converted total, with the shortfall reported
as coverage. Substituting 1.0, or the nearest rate from any date, would produce a total that
looks complete and is wrong.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .market import MarketDataError, MarketProvider, clean_provider_value
from .models import FxRate
from .schemas import utc_now
from .services import ZERO

# Currencies to try when no direct or inverse pair exists. USD first: it is one side of nearly
# every liquid pair, so a cross through it is usually available and tightly quoted.
CROSS_CURRENCIES = ("USD", "EUR")

# A rate carried forward further than this is still usable but no longer reflects the date.
STALE_RATE_DAYS = 5

ONE = Decimal("1")


class ConversionMethod:
    IDENTITY = "identity"
    DIRECT = "direct"
    INVERSE = "inverse"
    CROSS = "cross"


@dataclass(frozen=True)
class Conversion:
    """A resolved rate and everything needed to audit it."""

    base_currency: str
    quote_currency: str
    rate: Decimal
    method: str
    conversion_path: list[str]
    price_as_of: datetime | None
    provider: str | None
    is_stale: bool
    warnings: list[str] = field(default_factory=list)

    def apply(self, amount: Decimal) -> Decimal:
        return amount * self.rate


@dataclass
class FxUnavailable:
    """Why a pair could not be resolved. Returned instead of a fabricated rate."""

    base_currency: str
    quote_currency: str
    reason: str


class FxService:
    """Resolves rates for a cutoff, caching what it fetches so a report is self-consistent."""

    def __init__(
        self,
        session: Session,
        provider: MarketProvider,
        *,
        lookback_days: int = 30,
    ) -> None:
        self.session = session
        self.provider = provider
        self.lookback_days = lookback_days
        self._failed: set[tuple[str, str]] = set()

    def convert(
        self, base: str, quote: str, as_of: date
    ) -> Conversion | FxUnavailable:
        """The rate to multiply a `base` amount by to express it in `quote`, as of a date."""
        base, quote = base.upper(), quote.upper()
        if base == quote:
            return Conversion(
                base_currency=base,
                quote_currency=quote,
                rate=ONE,
                method=ConversionMethod.IDENTITY,
                conversion_path=[base],
                price_as_of=None,
                provider=None,
                is_stale=False,
            )

        direct = self._observed(base, quote, as_of)
        if direct is not None:
            return self._as_conversion(
                base, quote, direct, ConversionMethod.DIRECT, [base, quote], as_of
            )

        inverse = self._observed(quote, base, as_of)
        if inverse is not None and inverse.rate != ZERO:
            flipped = self._quantize(ONE / inverse.rate)
            return self._as_conversion(
                base,
                quote,
                inverse,
                ConversionMethod.INVERSE,
                [base, quote],
                as_of,
                rate_override=flipped,
            )

        return self._cross(base, quote, as_of)

    def _cross(self, base: str, quote: str, as_of: date) -> Conversion | FxUnavailable:
        """Route through an intermediary currency, reporting the full path."""
        for middle in CROSS_CURRENCIES:
            if middle in {base, quote}:
                continue
            first = self._leg(base, middle, as_of)
            second = self._leg(middle, quote, as_of)
            if first is None or second is None:
                continue

            rate = self._quantize(first[0] * second[0])
            as_of_dates = [item for item in (first[1], second[1]) if item is not None]
            oldest = min(as_of_dates) if as_of_dates else None
            stale = self._is_stale(oldest, as_of)
            warnings = [
                f"No direct {base}/{quote} rate was available, so the conversion crosses through "
                f"{middle}; the result carries the error of both legs"
            ]
            if stale:
                warnings.append(
                    f"The oldest leg of this cross is from {oldest.date().isoformat()}, "
                    f"more than {STALE_RATE_DAYS} days before {as_of.isoformat()}"
                    if oldest
                    else "A leg of this cross has no observation date"
                )
            return Conversion(
                base_currency=base,
                quote_currency=quote,
                rate=rate,
                method=ConversionMethod.CROSS,
                conversion_path=[base, middle, quote],
                price_as_of=oldest,
                provider=first[2] or second[2],
                is_stale=stale,
                warnings=warnings,
            )

        return FxUnavailable(
            base_currency=base,
            quote_currency=quote,
            reason=(
                f"No {base}/{quote} rate on or before {as_of.isoformat()}, directly, inverted, "
                f"or crossed through {' or '.join(CROSS_CURRENCIES)}"
            ),
        )

    def _leg(
        self, base: str, quote: str, as_of: date
    ) -> tuple[Decimal, datetime | None, str | None] | None:
        """One side of a cross, in either direction."""
        if base == quote:
            return (ONE, None, None)
        found = self._observed(base, quote, as_of)
        if found is not None:
            return (found.rate, _aware(found.price_as_of), found.provider)
        flipped = self._observed(quote, base, as_of)
        if flipped is not None and flipped.rate != ZERO:
            return (ONE / flipped.rate, _aware(flipped.price_as_of), flipped.provider)
        return None

    def _observed(self, base: str, quote: str, as_of: date) -> FxRate | None:
        """The newest stored rate at or before the cutoff, fetching when the cache falls short.

        A stored rate older than the cutoff is not automatically the right answer: it may simply
        be the last one fetched for an earlier report. Refetching when the cache does not reach
        the requested date keeps a later conversion from silently reusing an old rate forever.
        """
        stored = self._stored(base, quote, as_of)
        if stored is not None and _aware(stored.price_as_of).date() >= as_of:
            return stored
        if (base, quote) not in self._failed:
            self._fetch(base, quote, as_of)
            refreshed = self._stored(base, quote, as_of)
            if refreshed is not None:
                return refreshed
        # Nothing newer is available: an older stored rate is still better than no answer, and
        # the staleness it carries is reported to the caller.
        return stored

    def _stored(self, base: str, quote: str, as_of: date) -> FxRate | None:
        return self.session.scalar(
            select(FxRate)
            .where(
                FxRate.base_currency == base,
                FxRate.quote_currency == quote,
                # Strictly at or before the cutoff: a later rate would be look-ahead.
                FxRate.price_as_of <= _end_of_day(as_of),
            )
            .order_by(FxRate.price_as_of.desc())
            .limit(1)
        )

    def _fetch(self, base: str, quote: str, as_of: date) -> None:
        """Ask the provider for a pair's history and store every bar it returns."""
        symbol = f"{base}{quote}=X"
        try:
            result = self.provider.history(
                symbol,
                start_date=as_of - timedelta(days=self.lookback_days),
                end_date=as_of,
            )
        except MarketDataError:
            self._failed.add((base, quote))
            return

        now = utc_now()
        for bar in result.bars:
            observed = bar.timestamp.date()
            if observed > as_of:
                continue  # Defensive: a provider ignoring end_date must not leak a future rate.
            moment = _start_of_day(observed)
            exists = self.session.scalar(
                select(FxRate).where(
                    FxRate.base_currency == base,
                    FxRate.quote_currency == quote,
                    FxRate.price_as_of == moment,
                    FxRate.provider == result.provider,
                )
            )
            if exists is not None:
                continue
            self.session.add(
                FxRate(
                    id=str(uuid.uuid4()),
                    base_currency=base,
                    quote_currency=quote,
                    rate=self._quantize(bar.close),
                    price_as_of=moment,
                    fetched_at=result.fetched_at,
                    provider=result.provider,
                    provider_symbol=symbol,
                    created_at=now,
                )
            )
        self.session.flush()

    def _as_conversion(
        self,
        base: str,
        quote: str,
        observed: FxRate,
        method: str,
        path: list[str],
        as_of: date,
        *,
        rate_override: Decimal | None = None,
    ) -> Conversion:
        observed_at = _aware(observed.price_as_of)
        # Staleness is measured against the date asked for, not the rate's own date, which
        # would compare a value to itself and never report anything as stale.
        stale = self._is_stale(observed_at, as_of)
        warnings: list[str] = []
        if method == ConversionMethod.INVERSE:
            warnings.append(
                f"No {base}/{quote} rate was published, so the {quote}/{base} rate was inverted"
            )
        if stale:
            warnings.append(
                f"The newest {base}/{quote} rate on or before {as_of.isoformat()} is from "
                f"{observed_at.date().isoformat()}, more than {STALE_RATE_DAYS} days earlier"
            )
        return Conversion(
            base_currency=base,
            quote_currency=quote,
            rate=rate_override if rate_override is not None else observed.rate,
            method=method,
            conversion_path=path,
            price_as_of=observed_at,
            provider=observed.provider,
            is_stale=stale,
            warnings=warnings,
        )

    def _is_stale(self, observed_at: datetime | None, as_of: date) -> bool:
        if observed_at is None:
            return False
        return (as_of - observed_at.date()).days > STALE_RATE_DAYS

    @staticmethod
    def _quantize(value: Decimal) -> Decimal:
        return clean_provider_value(value)


def _start_of_day(value: date) -> datetime:
    return datetime.combine(value, time(0, 0), UTC)


def _end_of_day(value: date) -> datetime:
    return datetime.combine(value, time(23, 59, 59, 999999), UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
