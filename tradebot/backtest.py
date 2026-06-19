from dataclasses import dataclass, field
import hashlib
import json

from tradebot.fees import BinanceStockFeeModel
from tradebot.market_quality import MarketQuality, market_quality_allows_trade
from tradebot.models import Candle, Trade
from tradebot.strategy import StrategyConfig, generate_signal


@dataclass(frozen=True)
class BacktestConfig:
    starting_quote: float = 3500.0
    core_allocation_pct: float = 0.70
    slippage_bps: float = 30.0
    synthetic_spread_bps: float = 20.0
    max_spread_pct: float = 0.005


@dataclass(frozen=True)
class BacktestResult:
    starting_quote: float
    ending_equity: float
    cash: float
    t_position_qty: float
    t_position_value: float
    max_t_position_quote: float
    fees_paid: float
    trades: list[Trade]
    equity_curve: list[float]
    trade_pnls: list[float]
    result_id: str = ""
    engine_version: str = "tradebot-backtest-v2"
    config_snapshot: dict = field(default_factory=dict)
    execution_assumptions: dict = field(default_factory=dict)
    order_intents: list = field(default_factory=list)
    risk_events: list = field(default_factory=list)
    core_only_return_pct: float = 0.0
    strategy_vs_core_only_alpha: float = 0.0


def run_backtest(
    daily: list[Candle],
    intraday: list[Candle],
    backtest_config: BacktestConfig,
    strategy_config: StrategyConfig,
) -> BacktestResult:
    fees = BinanceStockFeeModel()
    cash = backtest_config.starting_quote * (1 - backtest_config.core_allocation_pct)
    t_qty = 0.0
    t_cost = 0.0
    last_buy_price = None
    max_t_bucket = cash
    max_t_position_quote = 0.0
    fees_paid = 0.0
    trades: list[Trade] = []
    equity_curve: list[float] = [cash]
    trade_pnls: list[float] = []
    order_intents: list[dict] = []
    risk_events: list[dict] = []
    layers = 0

    for idx in range(2, len(intraday)):
        window = intraday[:idx]
        signal_bar = window[-1]
        fill_bar = intraday[idx]
        t_value = t_qty * signal_bar.close
        avg_entry = t_cost / t_qty if t_qty > 0 else None
        signal = generate_signal(
            daily,
            window,
            position_quote=t_value,
            avg_entry_price=avg_entry,
            layers=layers,
            config=strategy_config,
        )

        synthetic_quality = MarketQuality(
            best_bid=fill_bar.open * (1 - backtest_config.synthetic_spread_bps / 20_000),
            best_ask=fill_bar.open * (1 + backtest_config.synthetic_spread_bps / 20_000),
        )
        quality_ok, quality_reason = market_quality_allows_trade(
            synthetic_quality,
            backtest_config.max_spread_pct,
        )

        buy_is_spaced = (
            last_buy_price is None
            or fill_bar.open <= last_buy_price * (1 - strategy_config.buy_grid_spacing_pct)
        )
        max_layers_reached = layers >= strategy_config.max_layers

        if signal.action == "BUY":
            source_signal = "OPEN_T" if layers <= 0 else "ADD_T"
            order_intents.append(
                _order_intent_record(
                    signal_bar.open_time,
                    "buy",
                    source_signal,
                    signal.suggested_quote,
                    0.0,
                    signal.reason,
                )
            )
            if max_layers_reached:
                risk_events.append(
                    _risk_event(signal_bar.open_time, "max_layers_rejected", "max layers reached", signal.reason)
                )
            elif not quality_ok:
                risk_events.append(_risk_event(signal_bar.open_time, "spread_rejected", quality_reason, signal.reason))
            elif cash < signal.suggested_quote:
                risk_events.append(
                    _risk_event(signal_bar.open_time, "cash_rejected", "insufficient cash", signal.reason)
                )
            elif not buy_is_spaced:
                risk_events.append(
                    _risk_event(signal_bar.open_time, "grid_spacing_rejected", "buy grid spacing not reached", signal.reason)
                )

        if (
            signal.action == "BUY"
            and not max_layers_reached
            and quality_ok
            and cash >= signal.suggested_quote
            and buy_is_spaced
        ):
            quote = min(signal.suggested_quote, cash, max_t_bucket - t_value)
            if quote >= signal.suggested_quote:
                fill_price = fill_bar.open * (1 + backtest_config.slippage_bps / 10_000)
                fee = fees.estimate(quote)
                qty = (quote - fee) / fill_price
                cash -= quote
                t_qty += qty
                t_cost += quote
                fees_paid += fee
                last_buy_price = fill_price
                layers += 1
                max_t_position_quote = max(max_t_position_quote, t_qty * fill_bar.open)
                trades.append(
                    Trade("BUY", fill_bar.open_time, fill_price, qty, quote, fee, signal.reason)
                )
            else:
                risk_events.append(
                    _risk_event(signal_bar.open_time, "t_bucket_cap_rejected", "T bucket cap exceeded", signal.reason)
                )

        elif signal.action == "SELL":
            order_intents.append(
                _order_intent_record(
                    signal_bar.open_time,
                    "sell",
                    "CLOSE_T",
                    0.0,
                    t_qty,
                    signal.reason,
                    reduce_only=True,
                )
            )
            if not quality_ok:
                risk_events.append(_risk_event(signal_bar.open_time, "spread_rejected", quality_reason, signal.reason))
            elif t_qty <= 0:
                risk_events.append(_risk_event(signal_bar.open_time, "position_rejected", "no T position", signal.reason))

        if signal.action == "SELL" and quality_ok and t_qty > 0:
            fill_price = fill_bar.open * (1 - backtest_config.slippage_bps / 10_000)
            gross = t_qty * fill_price
            fee = fees.estimate(gross)
            pnl = gross - fee - t_cost
            cash += gross - fee
            fees_paid += fee
            trade_pnls.append(pnl)
            trades.append(
                Trade("SELL", fill_bar.open_time, fill_price, t_qty, gross, fee, signal.reason)
            )
            t_qty = 0.0
            t_cost = 0.0
            last_buy_price = None
            layers = 0

        equity_curve.append(cash + t_qty * fill_bar.close)

    last_price = intraday[-1].close if intraday else 0.0
    t_position_value = t_qty * last_price
    ending_equity = cash + t_position_value
    core_only_return_pct = _core_only_return_pct(daily)
    t_return_pct = (
        (ending_equity / max_t_bucket - 1)
        if max_t_bucket > 0
        else 0.0
    )
    strategy_vs_core_only_alpha = t_return_pct - core_only_return_pct
    config_snapshot = {
        "startingQuote": backtest_config.starting_quote,
        "coreAllocationPct": backtest_config.core_allocation_pct,
        "slippageBps": backtest_config.slippage_bps,
        "syntheticSpreadBps": backtest_config.synthetic_spread_bps,
        "maxSpreadPct": backtest_config.max_spread_pct,
        "strategy": strategy_config.__dict__,
    }
    execution_assumptions = {
        "fillTiming": "next_bar_open",
        "feeModel": "BinanceStockFeeModel",
        "paperOnly": True,
        "engineVersion": "tradebot-backtest-v2",
    }
    result_hash = hashlib.sha1(
        json.dumps(config_snapshot, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]

    return BacktestResult(
        starting_quote=backtest_config.starting_quote,
        ending_equity=ending_equity,
        cash=cash,
        t_position_qty=t_qty,
        t_position_value=t_position_value,
        max_t_position_quote=max_t_position_quote,
        fees_paid=fees_paid,
        trades=trades,
        equity_curve=equity_curve,
        trade_pnls=trade_pnls,
        result_id=f"bt_{result_hash}",
        engine_version="tradebot-backtest-v2",
        config_snapshot=config_snapshot,
        execution_assumptions=execution_assumptions,
        order_intents=order_intents,
        risk_events=risk_events,
        core_only_return_pct=core_only_return_pct,
        strategy_vs_core_only_alpha=strategy_vs_core_only_alpha,
    )


def _order_intent_record(
    timestamp: int,
    side: str,
    source_signal: str,
    quote_amount: float,
    quantity: float,
    reason: str,
    reduce_only: bool = False,
) -> dict:
    return {
        "timestamp": timestamp,
        "symbol": "T_BUCKET",
        "side": side,
        "quoteAmount": float(quote_amount or 0.0),
        "quantity": float(quantity or 0.0),
        "orderType": "market",
        "sourceSignal": source_signal,
        "strategyId": "t-vwap",
        "reduceOnly": reduce_only,
        "paperOnly": True,
        "reason": reason,
    }


def _risk_event(timestamp: int, event_type: str, reason: str, signal_reason: str) -> dict:
    return {
        "timestamp": timestamp,
        "type": event_type,
        "reason": reason,
        "signalReason": signal_reason,
    }


def _core_only_return_pct(daily: list[Candle]) -> float:
    if len(daily) < 2 or daily[0].close <= 0:
        return 0.0
    return daily[-1].close / daily[0].close - 1
