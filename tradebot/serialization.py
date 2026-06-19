from dataclasses import asdict, is_dataclass

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


def serialize_equity_curve(values: list[float]) -> list[dict]:
    return [{"index": idx, "value": value} for idx, value in enumerate(values)]


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
        "equityCurve": serialize_equity_curve(result.equity_curve),
        "trades": [serialize_trade(trade) for trade in result.trades],
        "tradePnls": list(result.trade_pnls),
        "orderIntents": [_plain_dict(row) for row in result.order_intents],
        "riskEvents": [_plain_dict(row) for row in result.risk_events],
    }


def _plain_dict(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return dict(value)
    return value
