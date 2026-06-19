from dataclasses import dataclass
from typing import Optional

from tradebot.indicators import atr_pct, daily_trend_state, session_vwap
from tradebot.models import Candle, Signal


@dataclass(frozen=True)
class StrategyConfig:
    min_order_quote: float = 350.0
    momentum_order_quote: float = 175.0
    max_t_bucket_pct: float = 0.35
    pullback_pct: float = 0.008
    neutral_pullback_pct: float = 0.015
    pullback_lookback: int = 24
    reclaim_buffer_pct: float = 0.001
    momentum_vwap_premium_pct: float = 0.005
    momentum_lookback: int = 15
    momentum_volume_multiplier: float = 1.5
    take_profit_pct: float = 0.018
    layer2_take_profit_pct: float = 0.014
    layer3_take_profit_pct: float = 0.010
    stop_loss_pct: float = 0.035
    atr_stop_loss_multiplier: float = 1.5
    min_daily_atr_pct: float = 0.018
    buy_grid_spacing_pct: float = 0.012
    flash_crash_pct: float = 0.05
    max_layers: int = 3
    session_reset_utc_hour: int = 13


def generate_signal(
    daily: list[Candle],
    intraday: list[Candle],
    position_quote: float,
    config: StrategyConfig,
    avg_entry_price: Optional[float] = None,
    layers: int = 0,
) -> Signal:
    if len(daily) < 20 or len(intraday) < 2:
        return Signal("HOLD", "not enough candles", 0.0)

    trend = daily_trend_state(daily)
    current = intraday[-1]
    previous = intraday[-2]
    day_vwap = session_vwap(intraday, reset_utc_hour=config.session_reset_utc_hour)
    daily_atr = atr_pct(daily) or 0.0

    if day_vwap is None:
        return Signal("HOLD", "missing vwap", 0.0)

    one_minute_move = abs(current.close / previous.close - 1) if previous.close else 1.0
    if one_minute_move >= config.flash_crash_pct:
        return Signal("HOLD", f"extreme 1m move: {one_minute_move:.2%}", 0.0)

    if position_quote > 0 and avg_entry_price:
        pnl_pct = current.close / avg_entry_price - 1
        target = dynamic_take_profit_pct(layers, config)
        if pnl_pct >= target:
            return Signal("SELL", f"dynamic profit target hit: {pnl_pct:.2%} >= {target:.2%}", 0.8)
        effective_stop = (
            daily_atr * config.atr_stop_loss_multiplier
            if daily_atr > 0
            else config.stop_loss_pct
        )
        if pnl_pct <= -effective_stop:
            return Signal("SELL", f"t bucket stop loss hit: {pnl_pct:.2%} (stop={effective_stop:.2%})", 0.7)

    if trend == "broken":
        return Signal("HOLD", "daily trend broken; pause new T buys", 0.1)

    recent = intraday[-config.pullback_lookback :]
    previous_was_pullback = any(c.close <= day_vwap * (1 - config.pullback_pct) for c in recent[:-1])
    previous_was_neutral_pullback = any(
        c.close <= day_vwap * (1 - config.neutral_pullback_pct) for c in recent[:-1]
    )
    reclaimed_vwap = current.close >= day_vwap * (1 + config.reclaim_buffer_pct)
    volatile_enough = daily_atr >= config.min_daily_atr_pct
    strong_momentum = has_strong_momentum(intraday, day_vwap, config)

    if trend == "uptrend" and strong_momentum and position_quote <= 0:
        return Signal("BUY", "momentum follow-through above VWAP", 0.55, config.momentum_order_quote)

    if trend == "uptrend" and previous_was_pullback and reclaimed_vwap:
        confidence = 0.65 + min(daily_atr, 0.08)
        reason = "uptrend pullback reclaimed VWAP"
        if not volatile_enough:
            reason += "; ATR is low, size conservatively"
            confidence -= 0.1
        return Signal("BUY", reason, min(confidence, 0.9), config.min_order_quote)

    if trend == "neutral" and volatile_enough and previous_was_neutral_pullback and reclaimed_vwap:
        return Signal("BUY", "neutral range pullback reclaimed VWAP", 0.55, config.min_order_quote)

    if trend in {"uptrend", "neutral"} and current.close < day_vwap:
        return Signal("HOLD", "below VWAP; wait for reclaim", 0.2)

    return Signal("HOLD", f"no setup: trend={trend}, vwap={day_vwap:.2f}", 0.3)


def dynamic_take_profit_pct(layers: int, config: StrategyConfig) -> float:
    if layers >= 3:
        return config.layer3_take_profit_pct
    if layers == 2:
        return config.layer2_take_profit_pct
    return config.take_profit_pct


def has_strong_momentum(intraday: list[Candle], day_vwap: float, config: StrategyConfig) -> bool:
    if len(intraday) < config.momentum_lookback:
        return False
    window = intraday[-config.momentum_lookback :]
    price_ok = all(c.close >= day_vwap * (1 + config.momentum_vwap_premium_pct) for c in window)
    if not price_ok:
        return False
    baseline = intraday[:-config.momentum_lookback]
    if not baseline:
        return False
    avg_volume = sum(c.volume for c in baseline) / len(baseline)
    latest_volume = window[-1].volume
    return latest_volume >= avg_volume * config.momentum_volume_multiplier
