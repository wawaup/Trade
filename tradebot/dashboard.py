from tradebot.metrics import calculate_metrics
from tradebot.research import build_synthetic_universe, run_multi_asset_research, run_parameter_sensitivity, run_stress_suite
from tradebot.terminal_log import format_signal_log


def build_dashboard_state(seed: int = 7) -> dict:
    profiles = build_synthetic_universe(seed)
    multi = run_multi_asset_research(profiles, starting_quote=15_000, global_t_max_exposure_pct=0.30, seed=seed)
    sensitivity = run_parameter_sensitivity([0.006, 0.007, 0.008, 0.009, 0.010], seed)
    stress = run_stress_suite(seed)
    assets = []
    live_positions = []
    logs = []

    for row in multi.asset_results:
        assets.append(
            {
                "symbol": row.symbol,
                "theme": row.theme,
                "returnPct": row.metrics.total_return_pct,
                "maxDrawdownPct": row.metrics.max_drawdown_pct,
                "winRate": row.metrics.win_rate,
                "profitFactor": row.metrics.profit_factor,
                "trades": len(row.result.trades),
                "feesPaid": row.result.fees_paid,
            }
        )
        latest_trade = row.result.trades[-1] if row.result.trades else None
        live_positions.append(
            {
                "symbol": row.symbol,
                "status": "观察中" if latest_trade is None else ("持有T仓" if latest_trade.side == "BUY" else "T仓空仓"),
                "lastPrice": latest_trade.price if latest_trade else 0,
                "layers": 1 if latest_trade and latest_trade.side == "BUY" else 0,
                "tExposure": row.result.t_position_value,
                "spreadPct": _spread_pct_for_symbol(row.symbol),
                "risk": _risk_label(row.metrics.max_drawdown_pct),
            }
        )
        logs.append(
            format_signal_log(
                latest_trade.open_time if latest_trade else 0,
                row.symbol,
                "INFO",
                "SIGNAL",
                f"{row.theme} 状态={live_positions[-1]['status']} 收益={row.metrics.total_return_pct:.2%}",
            )
        )

    aggregate_start = sum(row.result.starting_quote * 0.30 for row in multi.asset_results)
    aggregate_curve = [aggregate_start]
    aggregate_pnls = []
    for row in multi.asset_results:
        aggregate_curve.append(aggregate_curve[-1] + (row.result.ending_equity - row.result.starting_quote * 0.30))
        aggregate_pnls.extend(row.result.trade_pnls)
    aggregate_metrics = calculate_metrics(aggregate_start, aggregate_curve, aggregate_pnls)

    return {
        "backtest": {
            "title": "策略历史回测",
            "summary": {
                "totalReturnPct": aggregate_metrics.total_return_pct,
                "maxDrawdownPct": aggregate_metrics.max_drawdown_pct,
                "winRate": aggregate_metrics.win_rate,
                "profitFactor": aggregate_metrics.profit_factor,
                "globalTExposureCap": multi.max_global_t_exposure,
            },
            "assets": assets,
            "sensitivity": [
                {
                    "parameter": row.parameter_value,
                    "returnPct": row.metrics.total_return_pct,
                    "maxDrawdownPct": row.metrics.max_drawdown_pct,
                }
                for row in sensitivity
            ],
            "stress": [
                {
                    "name": row.name,
                    "returnPct": row.metrics.total_return_pct,
                    "maxDrawdownPct": row.metrics.max_drawdown_pct,
                    "trades": len(row.result.trades),
                }
                for row in stress
            ],
        },
        "live": {
            "title": "当前实盘交易",
            "mode": "模拟监控",
            "positions": live_positions,
            "logs": logs[-10:],
            "guardrails": [
                "全局T仓敞口 <= 总资产30%",
                "点差超过0.5%拒绝成交",
                "1分钟波动超过5%拒绝接飞刀",
                "核心仓与T仓本地虚拟钱包隔离",
            ],
        },
        "glossary": glossary_items(),
    }


def glossary_items() -> list[dict]:
    return [
        {"term": "VWAP / 成交量加权均价", "body": "按成交量加权后的平均价格。做T时用它判断当前价格是否偏离日内公平价。"},
        {"term": "ATR / 平均真实波幅", "body": "衡量最近波动幅度。ATR越高，做T空间越大，但风险也越高。"},
        {"term": "bps / 基点", "body": "1 bps 等于 0.01%。30 bps 就是 0.30%，常用于滑点、点差和手续费。"},
        {"term": "Max Drawdown / 最大回撤", "body": "资金曲线从高点到低点的最大跌幅，是衡量最痛亏损的重要指标。"},
        {"term": "Profit Factor / 盈亏比", "body": "总盈利除以总亏损。大于1说明盈利交易覆盖了亏损交易。"},
        {"term": "Sharpe / 夏普比率", "body": "衡量单位波动带来的收益。它不是越高越一定安全，但能帮助比较策略质量。"},
        {"term": "T仓 / Trading Bucket", "body": "专门用于日内或波段高抛低吸的仓位，和长期核心仓分开管理。"},
    ]


def _spread_pct_for_symbol(symbol: str) -> float:
    if symbol in {"NVDA", "TSLA"}:
        return 0.0012
    if symbol == "SPCX":
        return 0.0028
    return 0.004


def _risk_label(max_drawdown_pct: float) -> str:
    if max_drawdown_pct <= -0.20:
        return "危险"
    if max_drawdown_pct <= -0.08:
        return "警戒"
    return "正常"

