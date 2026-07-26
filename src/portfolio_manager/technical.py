from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from math import sqrt

import pandas as pd

from .market import HistoryBar

PERIODS = (20, 60, 120, 252)


@dataclass(frozen=True)
class TechnicalAnalysis:
    trend: dict[str, Decimal | None]
    momentum: dict[str, Decimal | None]
    volatility: dict[str, Decimal | None]
    volume: dict[str, Decimal | None]
    warnings: list[str]


def _decimal(value: float | int | None) -> Decimal | None:
    if value is None or pd.isna(value):
        return None
    return Decimal(str(float(value)))


def _percent(numerator: float, denominator: float) -> Decimal | None:
    if denominator == 0:
        return None
    return _decimal((numerator / denominator - 1) * 100)


def bars_frame(bars: list[HistoryBar], as_of: date | None = None) -> pd.DataFrame:
    rows = [
        {
            "timestamp": pd.Timestamp(bar.timestamp),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": float(bar.volume),
        }
        for bar in bars
        if as_of is None or bar.timestamp.date() <= as_of
    ]
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(rows).sort_values("timestamp").drop_duplicates("timestamp", keep="last")
    return frame.set_index("timestamp")


def _last(series: pd.Series) -> Decimal | None:
    clean = series.dropna()
    return _decimal(clean.iloc[-1]) if not clean.empty else None


def _wilder_average(series: pd.Series, window: int) -> pd.Series:
    result = pd.Series(index=series.index, dtype=float)
    clean = series.dropna().astype(float)
    if len(clean) < window:
        return result
    first_index = clean.index[window - 1]
    average = clean.iloc[:window].mean()
    result.loc[first_index] = average
    for index, value in clean.iloc[window:].items():
        average = (average * (window - 1) + value) / window
        result.loc[index] = average
    return result


def _rsi(close: pd.Series, window: int = 14) -> Decimal | None:
    if len(close) <= window:
        return None
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    average_gain = _wilder_average(gain, window).iloc[-1]
    average_loss = _wilder_average(loss, window).iloc[-1]
    if average_gain == 0 and average_loss == 0:
        return Decimal("50")
    if average_loss == 0:
        return Decimal("100")
    if average_gain == 0:
        return Decimal("0")
    return _decimal(100 - 100 / (1 + average_gain / average_loss))


def _period_return(close: pd.Series, period: int) -> Decimal | None:
    if len(close) <= period:
        return None
    return _percent(close.iloc[-1], close.iloc[-period - 1])


def _sma_change(sma: pd.Series, periods: int = 20) -> Decimal | None:
    clean = sma.dropna()
    if len(clean) <= periods:
        return None
    return _percent(clean.iloc[-1], clean.iloc[-periods - 1])


def _realized_volatility(close: pd.Series, period: int) -> Decimal | None:
    returns = close.pct_change(fill_method=None).dropna()
    if len(returns) < period:
        return None
    return _decimal(returns.iloc[-period:].std(ddof=1) * sqrt(252) * 100)


def _volume_percentile(volume: pd.Series) -> Decimal | None:
    clean = volume.dropna().iloc[-252:]
    if clean.empty:
        return None
    return _decimal((clean <= clean.iloc[-1]).mean() * 100)


def _trend(close: pd.Series) -> dict[str, Decimal | None]:
    sma20 = close.rolling(20, min_periods=20).mean()
    sma50 = close.rolling(50, min_periods=50).mean()
    sma200 = close.rolling(200, min_periods=200).mean()
    latest = close.iloc[-1]
    return {
        "sma20": _last(sma20),
        "sma50": _last(sma50),
        "sma200": _last(sma200),
        "sma50_change_20d_percent": _sma_change(sma50),
        "sma200_change_20d_percent": _sma_change(sma200),
        "price_vs_sma20_percent": (
            _percent(latest, sma20.iloc[-1]) if not pd.isna(sma20.iloc[-1]) else None
        ),
        "price_vs_sma50_percent": (
            _percent(latest, sma50.iloc[-1]) if not pd.isna(sma50.iloc[-1]) else None
        ),
        "price_vs_sma200_percent": (
            _percent(latest, sma200.iloc[-1]) if not pd.isna(sma200.iloc[-1]) else None
        ),
    }


def _momentum(close: pd.Series) -> dict[str, Decimal | None]:
    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    return {
        **{f"return_{period}d_percent": _period_return(close, period) for period in PERIODS},
        "rsi14": _rsi(close),
        "macd": _last(macd),
        "macd_signal": _last(signal),
        "macd_histogram": _last(macd - signal),
    }


def _volatility(
    frame: pd.DataFrame, close: pd.Series
) -> tuple[dict[str, Decimal | None], list[str]]:
    warnings: list[str] = []
    if {"high", "low"}.issubset(frame.columns):
        high = frame["high"].astype(float)
        low = frame["low"].astype(float)
        previous_close = frame["close"].astype(float).shift()
        true_range = pd.concat(
            [high - low, (high - previous_close).abs(), (low - previous_close).abs()], axis=1
        ).max(axis=1)
        atr14 = _wilder_average(true_range, 14)
    else:
        atr14 = pd.Series(dtype=float)
        warnings.append("High/low data is unavailable; ATR indicators are null")
    atr = _last(atr14)
    latest = close.iloc[-1]
    return {
        "atr14": atr,
        "atr14_percent": (
            _decimal(float(atr) / latest * 100) if atr is not None and latest != 0 else None
        ),
        "realized_volatility_20d_percent": _realized_volatility(close, 20),
        "realized_volatility_60d_percent": _realized_volatility(close, 60),
        "drawdown_from_252d_high_percent": (
            _percent(latest, close.iloc[-252:].max()) if len(close) >= 252 else None
        ),
    }, warnings


def _volume(frame: pd.DataFrame) -> tuple[dict[str, Decimal | None], list[str]]:
    if (
        "volume" in frame
        and not frame["volume"].dropna().empty
        and (frame["volume"].dropna() != 0).any()
    ):
        volume = frame["volume"].astype(float).dropna()
        average20 = volume.iloc[-20:].mean() if len(volume) >= 20 else None
        return {
            "latest_vs_20d_average": (
                _decimal(volume.iloc[-1] / average20) if average20 not in {None, 0} else None
            ),
            "latest_252d_percentile": _volume_percentile(volume),
        }, []
    return {
        "latest_vs_20d_average": None,
        "latest_252d_percentile": None,
    }, ["Volume data is unavailable; volume indicators are null"]


def _minimum_warnings(observation_count: int) -> list[str]:
    minimums = {
        "SMA20 and volume ratio": 20,
        "SMA50 and 60-day metrics": 61,
        "120-day return": 121,
        "SMA200 slope and 252-day metrics": 253,
    }
    return [
        f"Insufficient observations for {label}: have {observation_count}, need {minimum}"
        for label, minimum in minimums.items()
        if observation_count < minimum
    ]


def _empty_analysis(warnings: list[str]) -> TechnicalAnalysis:
    trend_names = (
        "sma20",
        "sma50",
        "sma200",
        "sma50_change_20d_percent",
        "sma200_change_20d_percent",
        "price_vs_sma20_percent",
        "price_vs_sma50_percent",
        "price_vs_sma200_percent",
    )
    momentum_names = (
        *(f"return_{period}d_percent" for period in PERIODS),
        "rsi14",
        "macd",
        "macd_signal",
        "macd_histogram",
    )
    volatility_names = (
        "atr14",
        "atr14_percent",
        "realized_volatility_20d_percent",
        "realized_volatility_60d_percent",
        "drawdown_from_252d_high_percent",
    )
    return TechnicalAnalysis(
        {name: None for name in trend_names},
        {name: None for name in momentum_names},
        {name: None for name in volatility_names},
        {"latest_vs_20d_average": None, "latest_252d_percentile": None},
        warnings,
    )


def calculate_technical(frame: pd.DataFrame) -> TechnicalAnalysis:
    warnings: list[str] = []
    missing = sorted({"open", "high", "low", "close", "volume"}.difference(frame.columns))
    if missing:
        warnings.append(f"Missing OHLCV columns: {', '.join(missing)}")
    if "close" not in frame or frame["close"].dropna().empty:
        warnings.append("Closing-price data is unavailable; technical indicators are null")
        return _empty_analysis(warnings)

    close = frame["close"].astype(float).dropna()
    volatility, volatility_warnings = _volatility(frame, close)
    volume, volume_warnings = _volume(frame)
    warnings.extend(volatility_warnings)
    warnings.extend(volume_warnings)
    warnings.extend(_minimum_warnings(len(close)))
    return TechnicalAnalysis(_trend(close), _momentum(close), volatility, volume, warnings)


def relative_returns(
    instrument: pd.DataFrame, benchmark: pd.DataFrame
) -> tuple[dict[str, Decimal | None], list[str]]:
    aligned = pd.concat(
        [instrument["close"].rename("instrument"), benchmark["close"].rename("benchmark")],
        axis=1,
        join="inner",
    ).dropna()
    values: dict[str, Decimal | None] = {}
    warnings: list[str] = []
    for period in PERIODS:
        instrument_return = _period_return(aligned["instrument"], period)
        benchmark_return = _period_return(aligned["benchmark"], period)
        values[f"return_{period}d_percent"] = (
            instrument_return - benchmark_return
            if instrument_return is not None and benchmark_return is not None
            else None
        )
        if values[f"return_{period}d_percent"] is None:
            warnings.append(
                f"Insufficient common observations for {period}-day relative return: "
                f"have {len(aligned)}, need {period + 1}"
            )
    return values, warnings


def event_metrics(
    frame: pd.DataFrame, requested_date: date
) -> tuple[dict[str, date | Decimal | None], list[str]]:
    eligible = frame[frame.index.date >= requested_date]
    if eligible.empty:
        return {
            "requested_event_date": requested_date,
            "effective_anchor_date": None,
            "gap_percent": None,
            "high": None,
            "low": None,
            "close": None,
            "volume_percentile": None,
            "anchored_vwap": None,
        }, ["No observation exists on or after the requested event date"]

    anchor_timestamp = eligible.index[0]
    anchor_position = frame.index.get_loc(anchor_timestamp)
    anchor = frame.iloc[anchor_position]
    previous_close = frame.iloc[anchor_position - 1]["close"] if anchor_position > 0 else None
    gap = (
        _percent(float(anchor["open"]), float(previous_close))
        if previous_close is not None and not pd.isna(previous_close)
        else None
    )
    anchored = frame.iloc[anchor_position:]
    volume = anchored["volume"].astype(float)
    typical = (
        anchored["high"].astype(float)
        + anchored["low"].astype(float)
        + anchored["close"].astype(float)
    ) / 3
    total_volume = volume.sum()
    vwap = _decimal((typical * volume).sum() / total_volume) if total_volume > 0 else None
    history_volume = frame.iloc[max(0, anchor_position - 251) : anchor_position + 1]["volume"]
    warnings = []
    if previous_close is None or pd.isna(previous_close):
        warnings.append("Event gap is null because no prior close is available")
    if vwap is None:
        warnings.append("Anchored VWAP is null because volume is unavailable")
    return {
        "requested_event_date": requested_date,
        "effective_anchor_date": anchor_timestamp.date(),
        "gap_percent": gap,
        "high": _decimal(anchor["high"]),
        "low": _decimal(anchor["low"]),
        "close": _decimal(anchor["close"]),
        "volume_percentile": _volume_percentile(history_volume),
        "anchored_vwap": vwap,
    }, warnings
