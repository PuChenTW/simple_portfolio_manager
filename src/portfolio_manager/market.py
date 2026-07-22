from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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


class MarketProvider(Protocol):
    def fetch(self, ticker: str) -> MarketSnapshot: ...

    def history(self, ticker: str, days: int) -> list[HistoryBar]: ...


def _decimal(value: object) -> Decimal | None:
    if value is None or pd.isna(value):
        return None
    return Decimal(str(value))


def _utc_timestamp(value: object) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC").to_pydatetime()


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

    def history(self, ticker: str, days: int) -> list[HistoryBar]:
        symbol = ticker.strip().upper()
        start = datetime.now(UTC) - timedelta(days=days + 10)
        try:
            frame = yf.Ticker(symbol).history(
                start=start.date(), interval="1d", auto_adjust=True, actions=False
            )
        except Exception as exc:
            raise MarketDataError(f"Yahoo request failed for {symbol}") from exc
        if frame.empty:
            raise MarketDataError(f"No market data found for {symbol}")

        bars: list[HistoryBar] = []
        for index, row in frame.tail(days).iterrows():
            values = [_decimal(row.get(column)) for column in ("Open", "High", "Low", "Close")]
            if any(value is None for value in values):
                continue
            bars.append(
                HistoryBar(
                    timestamp=_utc_timestamp(index),
                    open=values[0],  # type: ignore[arg-type]
                    high=values[1],  # type: ignore[arg-type]
                    low=values[2],  # type: ignore[arg-type]
                    close=values[3],  # type: ignore[arg-type]
                    volume=_decimal(row.get("Volume")) or Decimal("0"),
                )
            )
        return bars
