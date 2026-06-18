from dataclasses import dataclass
from typing import Optional

from tradebot.indicators import atr_pct, daily_trend_state, vwap
from tradebot.models import Candle, Signal


@dataclass(frozen=True)
class StrategyConfig:
    min_order_quote: float = 350.0
    max_t_bucket_pct: float = 0.35
    pullback_pct: float = 0.008
    pullback_lookback: int = 24
    reclaim_buffer_pct: float = 0.001
    take_profit_pct: float = 0.018
    stop_loss_pct: float = 0.035
    min_daily_atr_pct: float = 0.018
    buy_grid_spacing_pct: float = 0.012


def generate_signal(
    daily: list[Candle],
    intraday: list[Candle],
    position_quote: float,
    config: StrategyConfig,
    avg_entry_price: Optional[float] = None,
) -> Signal:
    if len(daily) < 20 or len(intraday) < 2:
        return Signal("HOLD", "not enough candles", 0.0)

    trend = daily_trend_state(daily)
    current = intraday[-1]
    previous = intraday[-2]
    day_vwap = vwap(intraday)
    daily_atr = atr_pct(daily) or 0.0

    if day_vwap is None:
        return Signal("HOLD", "missing vwap", 0.0)

    if position_quote > 0 and avg_entry_price:
        pnl_pct = current.close / avg_entry_price - 1
        if pnl_pct >= config.take_profit_pct:
            return Signal("SELL", f"profit target hit: {pnl_pct:.2%}", 0.8)
        if pnl_pct <= -config.stop_loss_pct:
            return Signal("SELL", f"t bucket stop loss hit: {pnl_pct:.2%}", 0.7)

    if trend == "broken":
        return Signal("HOLD", "daily trend broken; pause new T buys", 0.1)

    recent = intraday[-config.pullback_lookback :]
    previous_was_pullback = any(c.close <= day_vwap * (1 - config.pullback_pct) for c in recent[:-1])
    reclaimed_vwap = current.close >= day_vwap * (1 + config.reclaim_buffer_pct)
    volatile_enough = daily_atr >= config.min_daily_atr_pct

    if trend == "uptrend" and previous_was_pullback and reclaimed_vwap:
        confidence = 0.65 + min(daily_atr, 0.08)
        reason = "uptrend pullback reclaimed VWAP"
        if not volatile_enough:
            reason += "; ATR is low, size conservatively"
            confidence -= 0.1
        return Signal("BUY", reason, min(confidence, 0.9), config.min_order_quote)

    if trend in {"uptrend", "neutral"} and current.close < day_vwap:
        return Signal("HOLD", "below VWAP; wait for reclaim", 0.2)

    return Signal("HOLD", f"no setup: trend={trend}, vwap={day_vwap:.2f}", 0.3)
