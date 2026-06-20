from dataclasses import asdict, is_dataclass
from typing import Optional

from tradebot.backtest import BacktestResult


def serialize_trade(trade) -> dict:
    return {
        "side": trade.side,
        "openTime": trade.open_time,
        "price": trade.price,
        "qty": trade.qty,
        "quote": trade.quote,
        "fee": trade.fee,
        "reason": trade.reason,
    }


def serialize_equity_curve(values: list[float], timestamps: Optional[list[int]] = None) -> list[dict]:
    if timestamps and len(timestamps) == len(values):
        return [{"time": ts // 1000, "value": round(value, 4)} for ts, value in zip(timestamps, values)]
    return [{"index": idx, "value": round(value, 4)} for idx, value in enumerate(values)]


def serialize_backtest_result(
    result: BacktestResult,
    *,
    result_id: str,
    created_at: str,
    symbols: list[str],
    summary: dict,
    assets: list[dict],
    source: str,
) -> dict:
    return {
        "resultId": result_id,
        "createdAt": created_at,
        "engineVersion": result.engine_version,
        "source": source,
        "symbols": list(symbols),
        "configSnapshot": result.config_snapshot,
        "executionAssumptions": result.execution_assumptions,
        "summary": summary,
        "assets": assets,
        "equityCurve": serialize_equity_curve(result.equity_curve, getattr(result, "equity_curve_ts", None)),
        "trades": [serialize_trade(trade) for trade in result.trades],
        "tradePnls": list(result.trade_pnls),
        "orderIntents": [_plain_dict(row) for row in result.order_intents],
        "riskEvents": [_plain_dict(row) for row in result.risk_events],
        "coreOnlyReturnPct": result.core_only_return_pct,
        "strategyVsCoreOnlyAlpha": result.strategy_vs_core_only_alpha,
    }


def serialize_asset_result_detail(symbol: str, theme: str, result: BacktestResult, metrics: Optional[dict] = None) -> dict:
    return {
        "symbol": symbol,
        "theme": theme,
        "metrics": dict(metrics or {}),
        "configSnapshot": result.config_snapshot,
        "executionAssumptions": result.execution_assumptions,
        "equityCurve": serialize_equity_curve(result.equity_curve, getattr(result, "equity_curve_ts", None)),
        "trades": [serialize_trade(trade) for trade in result.trades],
        "tradePnls": list(result.trade_pnls),
        "orderIntents": [_plain_dict(row) for row in result.order_intents],
        "riskEvents": [_plain_dict(row) for row in result.risk_events],
        "coreOnlyReturnPct": result.core_only_return_pct,
        "strategyVsCoreOnlyAlpha": result.strategy_vs_core_only_alpha,
    }


def _plain_dict(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return value
