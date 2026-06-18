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
    layers = 0

    for idx in range(2, len(intraday) + 1):
        window = intraday[:idx]
        current = window[-1]
        t_value = t_qty * current.close
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
            best_bid=current.close * (1 - backtest_config.synthetic_spread_bps / 20_000),
            best_ask=current.close * (1 + backtest_config.synthetic_spread_bps / 20_000),
        )
        quality_ok, quality_reason = market_quality_allows_trade(
            synthetic_quality,
            backtest_config.max_spread_pct,
        )

        buy_is_spaced = (
            last_buy_price is None
            or current.close <= last_buy_price * (1 - strategy_config.buy_grid_spacing_pct)
        )

        if signal.action == "BUY" and quality_ok and cash >= signal.suggested_quote and buy_is_spaced:
            quote = min(signal.suggested_quote, cash, max_t_bucket - t_value)
            if quote >= signal.suggested_quote:
                fill_price = current.close * (1 + backtest_config.slippage_bps / 10_000)
                fee = fees.estimate(quote)
                qty = (quote - fee) / fill_price
                cash -= quote
                t_qty += qty
                t_cost += quote
                fees_paid += fee
                last_buy_price = fill_price
                layers += 1
                max_t_position_quote = max(max_t_position_quote, t_qty * current.close)
                trades.append(
                    Trade("BUY", current.open_time, fill_price, qty, quote, fee, signal.reason)
                )

        elif signal.action == "SELL" and quality_ok and t_qty > 0:
            fill_price = current.close * (1 - backtest_config.slippage_bps / 10_000)
            gross = t_qty * fill_price
            fee = fees.estimate(gross)
            pnl = gross - fee - t_cost
            cash += gross - fee
            fees_paid += fee
            trade_pnls.append(pnl)
            trades.append(
                Trade("SELL", current.open_time, fill_price, t_qty, gross, fee, signal.reason)
            )
            t_qty = 0.0
            t_cost = 0.0
            last_buy_price = None
            layers = 0

        equity_curve.append(cash + t_qty * current.close)

    last_price = intraday[-1].close if intraday else 0.0
    t_position_value = t_qty * last_price
    ending_equity = cash + t_position_value
    config_snapshot = {
        "startingQuote": backtest_config.starting_quote,
        "coreAllocationPct": backtest_config.core_allocation_pct,
        "slippageBps": backtest_config.slippage_bps,
        "syntheticSpreadBps": backtest_config.synthetic_spread_bps,
        "maxSpreadPct": backtest_config.max_spread_pct,
        "strategy": strategy_config.__dict__,
    }
    execution_assumptions = {
        "fillTiming": "modeled_current_close",
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
        order_intents=[],
        risk_events=[],
    )
