import math
from dataclasses import dataclass


@dataclass(frozen=True)
class BacktestMetrics:
    total_return_pct: float
    max_drawdown_pct: float
    win_rate: float
    profit_factor: float
    sharpe: float
    sortino: float


def calculate_metrics(
    starting_equity: float,
    equity_curve: list[float],
    trade_pnls: list[float],
) -> BacktestMetrics:
    if not equity_curve:
        equity_curve = [starting_equity]

    ending = equity_curve[-1]
    total_return_pct = ending / starting_equity - 1 if starting_equity else 0.0
    max_drawdown_pct = _max_drawdown(equity_curve)
    wins = [pnl for pnl in trade_pnls if pnl > 0]
    losses = [pnl for pnl in trade_pnls if pnl < 0]
    win_rate = len(wins) / len(trade_pnls) if trade_pnls else 0.0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss else (float("inf") if gross_profit else 0.0)
    returns = _period_returns(equity_curve)
    sharpe = _sharpe(returns)
    sortino = _sortino(returns)

    return BacktestMetrics(
        total_return_pct=total_return_pct,
        max_drawdown_pct=max_drawdown_pct,
        win_rate=win_rate,
        profit_factor=profit_factor,
        sharpe=sharpe,
        sortino=sortino,
    )


def _max_drawdown(equity_curve: list[float]) -> float:
    peak = equity_curve[0]
    worst = 0.0
    for equity in equity_curve:
        peak = max(peak, equity)
        if peak:
            worst = min(worst, equity / peak - 1)
    return worst


def _period_returns(equity_curve: list[float]) -> list[float]:
    returns = []
    for previous, current in zip(equity_curve, equity_curve[1:]):
        if previous:
            returns.append(current / previous - 1)
    return returns


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _sharpe(returns: list[float]) -> float:
    std = _std(returns)
    if not returns or std == 0:
        return 0.0
    return _mean(returns) / std * math.sqrt(252)


def _sortino(returns: list[float]) -> float:
    downside = [value for value in returns if value < 0]
    std = _std(downside)
    if not returns or std == 0:
        return 0.0
    return _mean(returns) / std * math.sqrt(252)

