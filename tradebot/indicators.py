from collections.abc import Sequence
from typing import Optional

from tradebot.models import Candle


def sma(values: Sequence[float], period: int) -> Optional[float]:
    if period <= 0 or len(values) < period:
        return None
    return sum(values[-period:]) / period


def vwap(candles: Sequence[Candle]) -> Optional[float]:
    quote = sum(c.quote_volume for c in candles)
    volume = sum(c.volume for c in candles)
    if volume <= 0:
        return None
    return quote / volume


def atr_pct(candles: Sequence[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None

    true_ranges = []
    window = candles[-period:]
    previous = candles[-period - 1]
    for current in window:
        true_range = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        true_ranges.append(true_range)
        previous = current

    latest_close = candles[-1].close
    if latest_close <= 0:
        return None
    return (sum(true_ranges) / period) / latest_close


def daily_trend_state(daily: Sequence[Candle]) -> str:
    closes = [c.close for c in daily]
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    if ma5 is None or ma10 is None or ma20 is None:
        return "unknown"

    close = closes[-1]
    if close > ma5 > ma10 > ma20:
        return "uptrend"
    if close < ma10 or ma5 < ma10:
        return "broken"
    return "neutral"
