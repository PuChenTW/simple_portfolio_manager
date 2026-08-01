from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Protocol

import pandas as pd
import yfinance as yf


class MarketDataError(Exception):
    pass


@dataclass(frozen=True)
class InstrumentSnapshot:
    ticker: str
    name: str
    asset_type: str
    market: str
    exchange: str | None
    currency: str
    # Raw provider structural hint (Yahoo `quoteType`). `asset_type` collapses this to
    # stock/crypto for the legacy contract; the taxonomy needs the unreduced value.
    quote_type: str | None = None


@dataclass(frozen=True)
class QuoteSnapshot:
    price: Decimal
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    previous_close: Decimal | None
    volume: Decimal | None
    change: Decimal | None
    change_percent: Decimal | None
    market_cap: Decimal | None
    year_high: Decimal | None
    year_low: Decimal | None
    sma20: Decimal | None
    sma50: Decimal | None
    rsi14: Decimal | None
    macd: Decimal | None
    macd_signal: Decimal | None
    macd_histogram: Decimal | None
    provider_as_of: datetime
    indicators_as_of: datetime


@dataclass(frozen=True)
class MarketSnapshot:
    instrument: InstrumentSnapshot
    quote: QuoteSnapshot


@dataclass(frozen=True)
class HistoryBar:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


class HistoryInterval(StrEnum):
    DAILY = "1d"
    WEEKLY = "1wk"
    MONTHLY = "1mo"


class HistoryAdjustment(StrEnum):
    AUTO = "yfinance_auto_adjust"
    UNADJUSTED = "unadjusted"


@dataclass(frozen=True)
class HistoryResult:
    ticker: str
    provider: str
    interval: HistoryInterval
    adjustment: HistoryAdjustment
    requested_start_date: date | None
    requested_end_date: date | None
    fetched_at: datetime
    warnings: list[str]
    bars: list[HistoryBar]


class MarketProvider(Protocol):
    def fetch(self, ticker: str) -> MarketSnapshot: ...

    def history(
        self,
        ticker: str,
        days: int | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        interval: HistoryInterval = HistoryInterval.DAILY,
        adjustment: HistoryAdjustment = HistoryAdjustment.AUTO,
    ) -> HistoryResult: ...


def _decimal(value: object) -> Decimal | None:
    if value is None or pd.isna(value):
        return None
    return Decimal(str(value))


# yfinance returns daily bars as float32, so a close of 117.58 arrives as 117.58000183105469 and
# an FX rate of 32.395 as 32.39500045776367. Those digits are the float32 round-trip, not data:
# the type carries only about seven significant decimal digits. Rounding there recovers what the
# provider meant, while storing the raw value would make every derived figure look more precise
# than its source.
PROVIDER_SIGNIFICANT_DIGITS = 7


def clean_provider_value(value: Decimal) -> Decimal:
    """Strip float32 residue from a provider price or rate.

    Significant figures rather than decimal places: the noise sits a fixed distance from the
    leading digit, so a fixed number of places would truncate a sub-cent crypto price while
    leaving a five-figure index level dirty.
    """
    if value == 0:
        return value
    exponent = value.adjusted()  # power of ten of the leading digit
    quantum = Decimal(1).scaleb(exponent - (PROVIDER_SIGNIFICANT_DIGITS - 1))
    rounded = value.quantize(quantum, rounding=ROUND_HALF_EVEN)
    # Drop trailing zeros without ever moving to exponent form (1E+2 instead of 100).
    trimmed = rounded.normalize()
    return trimmed if trimmed.as_tuple().exponent <= 0 else trimmed.quantize(Decimal("1"))


def _utc_timestamp(value: object) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC").to_pydatetime()


def _observation_timestamp(value: object) -> datetime:
    """Preserve Yahoo's exchange-local bar date while normalizing its time representation."""
    return datetime.combine(pd.Timestamp(value).date(), datetime.min.time(), UTC)


def _provider_timestamp(value: object, fallback: object) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    if value:
        try:
            return _utc_timestamp(value)
        except (TypeError, ValueError):
            pass
    return _utc_timestamp(fallback)


def _last_decimal(series: pd.Series) -> Decimal | None:
    clean = series.dropna()
    return _decimal(clean.iloc[-1]) if not clean.empty else None


def calculate_indicators(close: pd.Series) -> dict[str, Decimal | None]:
    clean = close.dropna().astype(float)
    sma20 = clean.rolling(20, min_periods=20).mean()
    sma50 = clean.rolling(50, min_periods=50).mean()

    delta = clean.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    relative_strength = gain / loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + relative_strength))

    ema12 = clean.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = clean.ewm(span=26, adjust=False, min_periods=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()

    return {
        "sma20": _last_decimal(sma20),
        "sma50": _last_decimal(sma50),
        "rsi14": _last_decimal(rsi),
        "macd": _last_decimal(macd),
        "macd_signal": _last_decimal(signal),
        "macd_histogram": _last_decimal(macd - signal),
    }


def _metadata(symbol: str, info: dict[str, object]) -> InstrumentSnapshot:
    quote_type = str(info.get("quoteType", "")).upper()
    asset_type = "crypto" if quote_type == "CRYPTOCURRENCY" else "stock"
    if asset_type == "crypto":
        market = "CRYPTO"
    elif symbol.endswith(".TWO"):
        market = "TWO"
    elif symbol.endswith(".TW"):
        market = "TW"
    else:
        market = "US"

    fallback_currency = "TWD" if market in {"TW", "TWO"} else "USD"
    currency = str(info.get("currency") or fallback_currency).upper()
    name = str(info.get("shortName") or info.get("longName") or symbol)
    exchange = info.get("fullExchangeName") or info.get("exchange")
    return InstrumentSnapshot(
        ticker=symbol,
        name=name,
        asset_type=asset_type,
        market=market,
        exchange=str(exchange) if exchange else None,
        currency=currency,
        quote_type=quote_type or None,
    )


class YahooMarketProvider:
    def fetch(self, ticker: str) -> MarketSnapshot:
        symbol = ticker.strip().upper()
        try:
            security = yf.Ticker(symbol)
            history = security.history(
                period="1y", interval="1d", auto_adjust=True, actions=False
            )
            if history.empty:
                raise MarketDataError(f"No market data found for {symbol}")
            try:
                info = security.info or {}
            except Exception:
                info = {}
        except MarketDataError:
            raise
        except Exception as exc:
            raise MarketDataError(f"Yahoo request failed for {symbol}") from exc

        close = history["Close"].dropna()
        if close.empty:
            raise MarketDataError(f"No closing prices found for {symbol}")
        latest = history.iloc[-1]
        price = _decimal(info.get("currentPrice") or info.get("regularMarketPrice"))
        price = price or _decimal(close.iloc[-1])
        if price is None:
            raise MarketDataError(f"No current price found for {symbol}")
        previous_close = _decimal(info.get("previousClose"))
        if previous_close is None and len(close) > 1:
            previous_close = _decimal(close.iloc[-2])
        change = price - previous_close if previous_close is not None else None
        change_percent = (
            change / previous_close * 100
            if change is not None and previous_close not in {None, Decimal("0")}
            else None
        )
        indicators = calculate_indicators(close)
        provider_as_of = _provider_timestamp(info.get("regularMarketTime"), history.index[-1])

        quote = QuoteSnapshot(
            price=price,
            open=_decimal(latest.get("Open")),
            high=_decimal(latest.get("High")),
            low=_decimal(latest.get("Low")),
            previous_close=previous_close,
            volume=_decimal(latest.get("Volume")),
            change=change,
            change_percent=change_percent,
            market_cap=_decimal(info.get("marketCap")),
            year_high=_decimal(info.get("fiftyTwoWeekHigh")) or _decimal(history["High"].max()),
            year_low=_decimal(info.get("fiftyTwoWeekLow")) or _decimal(history["Low"].min()),
            provider_as_of=provider_as_of,
            indicators_as_of=provider_as_of,
            **indicators,
        )
        return MarketSnapshot(instrument=_metadata(symbol, info), quote=quote)

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
        requested_days = days if days is not None else 365
        requested_start = start_date
        requested_end = end_date
        if start_date is None:
            reference = end_date or datetime.now(UTC).date()
            start_date = reference - timedelta(days=requested_days + 10)
        yahoo_end = end_date + timedelta(days=1) if end_date is not None else None
        try:
            frame = yf.Ticker(symbol).history(
                start=start_date,
                end=yahoo_end,
                interval=interval.value,
                auto_adjust=adjustment == HistoryAdjustment.AUTO,
                actions=False,
            )
        except Exception as exc:
            raise MarketDataError(f"Yahoo request failed for {symbol}") from exc
        if frame.empty:
            raise MarketDataError(f"No market data found for {symbol}")

        bars: list[HistoryBar] = []
        if end_date is not None:
            frame = frame[pd.Index(frame.index).date <= end_date]
        if requested_start is None and requested_end is None:
            frame = frame.tail(requested_days)
        warnings: list[str] = []
        skipped_ohlc = 0
        missing_volume = 0
        for index, row in frame.sort_index().iterrows():
            values = [_decimal(row.get(column)) for column in ("Open", "High", "Low", "Close")]
            if any(value is None for value in values):
                skipped_ohlc += 1
                continue
            volume = _decimal(row.get("Volume"))
            if volume is None:
                missing_volume += 1
            bars.append(
                HistoryBar(
                    timestamp=_observation_timestamp(index),
                    open=values[0],  # type: ignore[arg-type]
                    high=values[1],  # type: ignore[arg-type]
                    low=values[2],  # type: ignore[arg-type]
                    close=values[3],  # type: ignore[arg-type]
                    volume=volume or Decimal("0"),
                )
            )
        if not bars:
            raise MarketDataError(f"No usable market data found for {symbol}")
        if skipped_ohlc:
            warnings.append(f"Skipped {skipped_ohlc} observations with missing OHLC values")
        if missing_volume:
            warnings.append(
                f"Volume was unavailable for {missing_volume} observations and represented as zero"
            )
        return HistoryResult(
            ticker=symbol,
            provider="Yahoo Finance via yfinance",
            interval=interval,
            adjustment=adjustment,
            requested_start_date=requested_start,
            requested_end_date=requested_end,
            fetched_at=datetime.now(UTC),
            warnings=warnings,
            bars=bars,
        )
