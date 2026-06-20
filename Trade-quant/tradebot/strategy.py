from dataclasses import dataclass
from typing import Optional

from tradebot.indicators import atr_pct, daily_trend_state, kdj, macd, session_vwap, sma
from tradebot.models import Candle, CoreSignal, Signal


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
    confidence_size_floor: float = 0.5
    kdj_period: int = 9
    kdj_signal_smooth: int = 3
    kdj_overbought_threshold: float = 80.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    require_macd_histogram_positive: bool = True
    overnight_gap_pct: float = 0.04
    core_catastrophic_pct: float = 0.08


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

    # Only check overnight gap when intraday data is genuinely newer than the last daily bar.
    if daily and intraday and daily[-1].close > 0 and intraday[0].open_time >= daily[-1].close_time:
        gap = abs(intraday[0].open / daily[-1].close - 1)
        if gap >= config.overnight_gap_pct:
            return Signal("HOLD", f"overnight gap {gap:.2%} exceeds {config.overnight_gap_pct:.2%}", 0.1)

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

    kdj_result = kdj(intraday, config.kdj_period, config.kdj_signal_smooth)
    if kdj_result is not None:
        _, _, j_value = kdj_result
        if j_value > config.kdj_overbought_threshold:
            return Signal("HOLD", f"KDJ overbought: J={j_value:.1f}", 0.2)

    if config.require_macd_histogram_positive:
        macd_result = macd(daily, config.macd_fast, config.macd_slow, config.macd_signal)
        if macd_result is not None:
            _, _, histogram = macd_result
            if histogram < 0:
                return Signal("HOLD", f"MACD histogram negative: {histogram:.4f}", 0.2)

    recent = intraday[-config.pullback_lookback :]
    previous_was_pullback = any(c.close <= day_vwap * (1 - config.pullback_pct) for c in recent[:-1])
    previous_was_neutral_pullback = any(
        c.close <= day_vwap * (1 - config.neutral_pullback_pct) for c in recent[:-1]
    )
    reclaimed_vwap = current.close >= day_vwap * (1 + config.reclaim_buffer_pct)
    volatile_enough = daily_atr >= config.min_daily_atr_pct
    strong_momentum = has_strong_momentum(intraday, day_vwap, config)

    if trend == "uptrend" and strong_momentum and position_quote <= 0:
        momentum_confidence = 0.55
        return Signal(
            "BUY",
            "momentum follow-through above VWAP",
            momentum_confidence,
            confidence_sized_quote(config.momentum_order_quote, momentum_confidence, config.confidence_size_floor),
        )

    if trend == "uptrend" and previous_was_pullback and reclaimed_vwap:
        confidence = 0.65 + min(daily_atr, 0.08)
        reason = "uptrend pullback reclaimed VWAP"
        if not volatile_enough:
            reason += "; ATR is low, size conservatively"
            confidence -= 0.1
        confidence = min(confidence, 0.9)
        return Signal(
            "BUY",
            reason,
            confidence,
            confidence_sized_quote(config.min_order_quote, confidence, config.confidence_size_floor),
        )

    if trend == "neutral" and volatile_enough and previous_was_neutral_pullback and reclaimed_vwap:
        neutral_confidence = 0.55
        return Signal(
            "BUY",
            "neutral range pullback reclaimed VWAP",
            neutral_confidence,
            confidence_sized_quote(config.min_order_quote, neutral_confidence, config.confidence_size_floor),
        )

    if trend in {"uptrend", "neutral"} and current.close < day_vwap:
        return Signal("HOLD", "below VWAP; wait for reclaim", 0.2)

    return Signal("HOLD", f"no setup: trend={trend}, vwap={day_vwap:.2f}", 0.3)


def dynamic_take_profit_pct(layers: int, config: StrategyConfig) -> float:
    if layers >= 3:
        return config.layer3_take_profit_pct
    if layers == 2:
        return config.layer2_take_profit_pct
    return config.take_profit_pct


def confidence_sized_quote(base_quote: float, confidence: float, floor: float) -> float:
    """Scale base_quote by confidence.

    confidence=0.9 (max) → full size; floor fraction at confidence=0.
    Example with floor=0.5: confidence 0.55 → ~61%, 0.65 → ~72%, 0.9 → 100%.
    """
    scale = max(floor, min(confidence / 0.9, 1.0))
    return base_quote * scale


def core_position_signal(daily: list[Candle], config: StrategyConfig) -> CoreSignal:
    """4-tier gradual core-position exit trigger (Plan B).

    Returns the TARGET allocation pct (0.0–1.0) for the core position.
    Caller compares this to the current allocation to decide how much to buy/sell.

    Exit tiers (most severe first):
      Catastrophic drop ≥ core_catastrophic_pct → EXIT_ALL (target=0.0) immediately.
      3+ consecutive closes below MA20              → target=0.0 (full exit)
      MA5 dead-cross + below MA20 + MACD dead-cross → target=0.25
      MA5 dead-cross + below MA20                   → target=0.50
      MA5 dead-cross only                           → target=0.75
      No triggers                                   → HOLD  (target=1.0)
    """
    if len(daily) < 20:
        return CoreSignal("HOLD", "not enough daily candles", 1.0)

    closes = [c.close for c in daily]
    ma5 = sma(closes, 5)
    ma10 = sma(closes, 10)
    ma20 = sma(closes, 20)
    if ma5 is None or ma10 is None or ma20 is None:
        return CoreSignal("HOLD", "missing MAs", 1.0)

    current_close = closes[-1]
    prev_close = closes[-2]

    # Catastrophic single-day drop (瀑布式大跌)
    if prev_close > 0:
        daily_drop = current_close / prev_close - 1
        if daily_drop <= -config.core_catastrophic_pct:
            return CoreSignal("EXIT_ALL", f"catastrophic daily drop: {daily_drop:.2%}", 0.0)

    # Consecutive closes below MA20
    consec_below = 0
    for c in reversed(closes):
        if c < ma20:
            consec_below += 1
        else:
            break

    macd_result = macd(daily, config.macd_fast, config.macd_slow, config.macd_signal)
    macd_negative = macd_result is not None and macd_result[2] < 0
    dead_cross = ma5 < ma10
    below_ma20 = current_close < ma20

    if consec_below >= 3:
        return CoreSignal("REDUCE", f"{consec_below} consecutive closes below MA20 → full exit", 0.0)
    if dead_cross and below_ma20 and macd_negative:
        return CoreSignal("REDUCE", "MA5 dead-cross + below MA20 + MACD dead-cross → 75% exit", 0.25)
    if dead_cross and below_ma20:
        return CoreSignal("REDUCE", "MA5 dead-cross + below MA20 → 50% exit", 0.50)
    if dead_cross:
        return CoreSignal("REDUCE", "MA5 dead-cross → 25% exit", 0.75)

    return CoreSignal("HOLD", f"trend={daily_trend_state(daily)}, no core exit trigger", 1.0)


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
