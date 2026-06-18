from dataclasses import dataclass

from tradebot.backtest import BacktestConfig, BacktestResult, run_backtest
from tradebot.data import DAY_MS, generate_synthetic_spcx
from tradebot.metrics import BacktestMetrics, calculate_metrics
from tradebot.models import Candle
from tradebot.strategy import StrategyConfig
from tradebot.terminal_log import format_signal_log


@dataclass(frozen=True)
class AssetProfile:
    symbol: str
    theme: str
    slippage_bps: float
    synthetic_spread_bps: float


@dataclass(frozen=True)
class AssetResearchRow:
    symbol: str
    theme: str
    result: BacktestResult
    metrics: BacktestMetrics


@dataclass(frozen=True)
class SensitivityRow:
    parameter_name: str
    parameter_value: float
    metrics: BacktestMetrics


@dataclass(frozen=True)
class StressRow:
    name: str
    metrics: BacktestMetrics
    result: BacktestResult


@dataclass(frozen=True)
class MultiAssetResearchResult:
    asset_results: list[AssetResearchRow]
    max_global_t_exposure: float


@dataclass(frozen=True)
class ResearchSummary:
    title: str
    rows: list[AssetResearchRow]
    sensitivity: list[SensitivityRow]
    stress: list[StressRow]
    terminal_logs: list[str]


def build_synthetic_universe(seed: int = 7) -> list[AssetProfile]:
    return [
        AssetProfile("SPCX", "商业航天/星链", 30, 20),
        AssetProfile("TSLA", "科技巨头/高流动性", 12, 12),
        AssetProfile("NVDA", "AI芯片/高流动性", 8, 10),
        AssetProfile("MU", "存储周期", 24, 28),
        AssetProfile("CPOX", "CPO光通信", 35, 40),
    ]


def synthetic_asset_data(profile: AssetProfile, seed: int) -> tuple[list[Candle], list[Candle]]:
    daily, intraday = generate_synthetic_spcx(seed + sum(ord(ch) for ch in profile.symbol))
    factor = 1 + (profile.slippage_bps - 20) / 1000
    adjusted_daily = [_scale_candle(c, factor) for c in daily]
    adjusted_intraday = [_scale_candle(c, factor) for c in intraday]
    return adjusted_daily, adjusted_intraday


def run_parameter_sensitivity(
    pullback_values: list[float],
    seed: int = 7,
) -> list[SensitivityRow]:
    daily, intraday = generate_synthetic_spcx(seed)
    rows = []
    for value in pullback_values:
        result = run_backtest(
            daily,
            intraday,
            BacktestConfig(),
            StrategyConfig(pullback_pct=value),
        )
        rows.append(
            SensitivityRow(
                "VWAP回踩阈值",
                value,
                calculate_metrics(result.starting_quote * 0.30, result.equity_curve, result.trade_pnls),
            )
        )
    return rows


def run_multi_asset_research(
    profiles: list[AssetProfile],
    starting_quote: float,
    global_t_max_exposure_pct: float,
    seed: int = 7,
) -> MultiAssetResearchResult:
    rows = []
    max_allowed = starting_quote * global_t_max_exposure_pct
    max_seen = 0.0
    per_asset_quote = starting_quote / len(profiles)

    for profile in profiles:
        daily, intraday = synthetic_asset_data(profile, seed)
        result = run_backtest(
            daily,
            intraday,
            BacktestConfig(
                starting_quote=per_asset_quote,
                core_allocation_pct=0.70,
                slippage_bps=profile.slippage_bps,
                synthetic_spread_bps=profile.synthetic_spread_bps,
            ),
            StrategyConfig(),
        )
        metrics = calculate_metrics(result.starting_quote * 0.30, result.equity_curve, result.trade_pnls)
        rows.append(AssetResearchRow(profile.symbol, profile.theme, result, metrics))
        max_seen += result.max_t_position_quote

    return MultiAssetResearchResult(rows, min(max_seen, max_allowed))


def run_stress_suite(seed: int = 7) -> list[StressRow]:
    daily, intraday = generate_synthetic_spcx(seed)
    down_daily, down_intraday = apply_downtrend_stress(daily, intraday)
    scenarios = [
        ("高滑点100bps", daily, intraday, BacktestConfig(slippage_bps=100, synthetic_spread_bps=40)),
        ("点差放大拒单", daily, intraday, BacktestConfig(slippage_bps=30, synthetic_spread_bps=80)),
        ("连续五天下跌", down_daily, down_intraday, BacktestConfig(slippage_bps=30, synthetic_spread_bps=30)),
    ]
    rows = []
    for name, scenario_daily, scenario_intraday, config in scenarios:
        result = run_backtest(scenario_daily, scenario_intraday, config, StrategyConfig())
        metrics = calculate_metrics(result.starting_quote * 0.30, result.equity_curve, result.trade_pnls)
        rows.append(StressRow(name, metrics, result))
    return rows


def run_full_research(seed: int = 7) -> ResearchSummary:
    profiles = build_synthetic_universe(seed)
    multi = run_multi_asset_research(profiles, starting_quote=15_000, global_t_max_exposure_pct=0.30, seed=seed)
    sensitivity = run_parameter_sensitivity([0.006, 0.007, 0.008, 0.009, 0.010], seed)
    stress = run_stress_suite(seed)
    logs = []
    for row in multi.asset_results:
        logs.append(
            format_signal_log(
                row.result.trades[0].open_time if row.result.trades else 0,
                row.symbol,
                "INFO",
                "SIGNAL",
                f"{row.theme} 回测完成，收益 {row.metrics.total_return_pct:.2%}",
            )
        )
    return ResearchSummary("实盘前工业级回测报告", multi.asset_results, sensitivity, stress, logs)


def split_in_sample_out_of_sample(
    candles: list[Candle],
    split_timestamp_ms: int,
) -> tuple[list[Candle], list[Candle]]:
    return (
        [c for c in candles if c.open_time < split_timestamp_ms],
        [c for c in candles if c.open_time >= split_timestamp_ms],
    )


def apply_downtrend_stress(daily: list[Candle], intraday: list[Candle]) -> tuple[list[Candle], list[Candle]]:
    stressed_daily = []
    for idx, candle in enumerate(daily):
        factor = 1 - min(max(idx - (len(daily) - 5), 0) * 0.03, 0.15)
        stressed_daily.append(_scale_candle(candle, factor))
    stressed_intraday = []
    for idx, candle in enumerate(intraday):
        factor = 1 - min(idx / max(len(intraday), 1) * 0.12, 0.12)
        stressed_intraday.append(_scale_candle(candle, factor))
    return stressed_daily, stressed_intraday


def _scale_candle(candle: Candle, factor: float) -> Candle:
    return Candle(
        candle.open_time,
        candle.open * factor,
        candle.high * factor,
        candle.low * factor,
        candle.close * factor,
        candle.volume,
        candle.close_time,
        candle.quote_volume * factor,
        candle.trades,
    )

