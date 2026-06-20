from collections.abc import Sequence
from typing import Optional

from tradebot.data import session_start_ms
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


def session_vwap(candles: Sequence[Candle], reset_utc_hour: int = 8) -> Optional[float]:
    if not candles:
        return None
    latest_time = candles[-1].open_time
    session_start = session_start_ms(latest_time, reset_utc_hour=reset_utc_hour)
    return vwap([c for c in candles if session_start <= c.open_time <= latest_time])


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


def kdj(
    candles: Sequence[Candle],
    period: int = 9,
    signal_smooth: int = 3,
) -> Optional[tuple[float, float, float]]:
    """Return (K, D, J) for the latest bar using standard EMA-based KDJ.

    For each bar i:
      RSV_i = (close_i - min_low_period) / (max_high_period - min_low_period) * 100
      K_i   = (1 - alpha) * K_{i-1} + alpha * RSV_i   where alpha = 1 / signal_smooth
      D_i   = (1 - alpha) * D_{i-1} + alpha * K_i
      J_i   = 3 * K_i - 2 * D_i

    K and D are initialised at 50 (neutral).  Returns None when fewer than
    `period` candles are available.
    """
    if len(candles) < period:
        return None

    alpha = 1.0 / signal_smooth
    k, d = 50.0, 50.0

    for i in range(len(candles)):
        win = candles[max(0, i - period + 1) : i + 1]
        lo = min(c.low for c in win)
        hi = max(c.high for c in win)
        rsv = 50.0 if hi == lo else (candles[i].close - lo) / (hi - lo) * 100
        k = (1 - alpha) * k + alpha * rsv
        d = (1 - alpha) * d + alpha * k

    j = 3 * k - 2 * d
    return k, d, j


def ema(values: Sequence[float], period: int) -> Optional[float]:
    """Exponential moving average of the last element, seeded from the first SMA."""
    if len(values) < period:
        return None
    seed = sum(values[:period]) / period
    alpha = 2.0 / (period + 1)
    result = seed
    for v in values[period:]:
        result = alpha * v + (1 - alpha) * result
    return result


def macd(
    candles: Sequence[Candle],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Optional[tuple[float, float, float]]:
    """Return (macd_line, signal_line, histogram) for the latest bar.

    macd_line = EMA(fast) - EMA(slow)
    signal_line = EMA(macd_line, signal)
    histogram   = macd_line - signal_line

    Positive histogram → short-term momentum accelerating upward.
    Returns None when there are not enough candles.
    """
    if len(candles) < slow + signal:
        return None

    closes = [c.close for c in candles]
    alpha_fast = 2.0 / (fast + 1)
    alpha_slow = 2.0 / (slow + 1)
    alpha_sig = 2.0 / (signal + 1)

    # Seed EMAs from the first SMA
    ema_fast = sum(closes[:fast]) / fast
    ema_slow = sum(closes[:slow]) / slow

    macd_values: list[float] = []
    for i, close in enumerate(closes):
        if i < slow:
            # Keep seeding slow EMA; fast EMA is already valid after `fast` bars
            if i >= fast:
                ema_fast = alpha_fast * close + (1 - alpha_fast) * ema_fast
            ema_slow = alpha_slow * close + (1 - alpha_slow) * ema_slow
        else:
            ema_fast = alpha_fast * close + (1 - alpha_fast) * ema_fast
            ema_slow = alpha_slow * close + (1 - alpha_slow) * ema_slow
            macd_values.append(ema_fast - ema_slow)

    if len(macd_values) < signal:
        return None

    # Signal line: EMA of macd_values
    sig_line = sum(macd_values[:signal]) / signal
    for mv in macd_values[signal:]:
        sig_line = alpha_sig * mv + (1 - alpha_sig) * sig_line

    macd_line = macd_values[-1]
    histogram = macd_line - sig_line
    return macd_line, sig_line, histogram


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
