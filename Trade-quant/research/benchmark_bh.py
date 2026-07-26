#!/usr/bin/env python3
"""
benchmark_bh.py —— 池内等权买入持有基准（RESEARCH_LOG §15，2026-07-27）

回答审核第 1 问（裁判性）：策略的 +39.7%/0.89 里，"选池"贡献多少、
"因子在池内选股"贡献多少？若策略跑不赢自己股票池的等权 B&H（同成本口径），
则四因子 + regime 的全部复杂度为零增量甚至负增量。

三条基准，与 factor_combo_backtest.perf_stats 同一套统计公式：
  1. BH-期初等权：起始日已有数据的票各买 1/N 后纯持有（一次买入成本 25bp）。
     中途 IPO 的票不进——这是"2022 年初真金实盘可执行"的最保守口径。
  2. BH-月度等权：每 21 个交易日再平衡回等权，中途 IPO 满 60 日历史后纳入，
     每次调仓按换手金额收双边 25bp——这是"持续维护等权敞口"的口径。
  3. QQQ：市场基准。

用法：python3 benchmark_bh.py [--since 2022-01-01] [--cost-bps 25]
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from factor_scanner import load_universe, load_panel, load_benchmark
from factor_combo_backtest import perf_stats

TRADING_DAYS_PER_MONTH = 21
MIN_HISTORY_FOR_ENTRY = 60   # IPO 票纳入月度等权前的最少数据天数


def bh_static(close: pd.DataFrame, cost: float) -> pd.Series:
    """期初等权买入后纯持有。停牌/退市段用 ffill 收盘价估值（与主回测同口径，偏乐观）。"""
    close_ff = close.ffill()
    first = close_ff.iloc[0]
    eligible = first.dropna().index
    weights = pd.Series(1.0 / len(eligible), index=eligible)
    # 一次性买入成本
    equity0 = 1.0 - cost
    rel = close_ff[eligible].div(first[eligible])
    equity = rel.mul(weights).sum(axis=1) * equity0
    return equity


def bh_monthly(close: pd.DataFrame, cost: float) -> pd.Series:
    """每 21 个交易日再平衡回等权；有 ≥60 天历史的票（含中途 IPO）逐步纳入。

    实现为持仓市值演化：re-balance 日把组合调回等权，
    换手成本 = Σ|目标市值-当前市值| × cost。
    """
    close_ff = close.ffill()
    dates = close_ff.index
    valid_history = close_ff.notna().cumsum() >= MIN_HISTORY_FOR_ENTRY

    equity = pd.Series(np.nan, index=dates)
    holdings: dict = {}          # sym -> 股数（组合净值起点 1.0）
    cash = 1.0

    for i, dt in enumerate(dates):
        px = close_ff.loc[dt]
        # 当日估值
        pos_val = sum(q * px[s] for s, q in holdings.items() if not pd.isna(px[s]))
        eq = cash + pos_val
        equity.iloc[i] = eq

        if i % TRADING_DAYS_PER_MONTH != 0:
            continue
        # 再平衡日：目标 = 当日合格票等权
        elig = [s for s in close_ff.columns
                if valid_history.at[dt, s] and not pd.isna(px[s]) and px[s] > 0]
        if not elig:
            continue
        tgt_val = eq / len(elig)
        turnover = 0.0
        new_holdings = {}
        for s in elig:
            cur_val = holdings.get(s, 0.0) * px[s] if s in holdings else 0.0
            turnover += abs(tgt_val - cur_val)
        for s in set(holdings) - set(elig):     # 剔除票全卖
            if not pd.isna(px[s]):
                turnover += holdings[s] * px[s]
        fee = turnover * cost
        eq_after = eq - fee
        tgt_val = eq_after / len(elig)
        for s in elig:
            new_holdings[s] = tgt_val / px[s]
        holdings = new_holdings
        cash = 0.0
        equity.iloc[i] = eq_after

    return equity.ffill()


def main():
    ap = argparse.ArgumentParser(description="池内等权 B&H 基准")
    ap.add_argument("--since", default="2022-01-01")
    ap.add_argument("--cost-bps", type=float, default=25.0)
    args = ap.parse_args()
    cost = args.cost_bps / 10_000.0

    symbols, benchmarks = load_universe()
    print(f"股票池 {len(symbols)} 只，since={args.since}，单边成本 {args.cost_bps:.0f}bp")
    close, high, low, vol = load_panel(symbols, since=args.since)
    close = close.loc[args.since:]
    print(f"面板：{close.shape[1]} 只 × {len(close)} 天  {close.index[0].date()} ~ {close.index[-1].date()}")
    n_start = close.iloc[0].notna().sum()
    print(f"期初有数据（BH-期初等权可买）：{n_start} 只；其余 {close.shape[1] - n_start} 只为中途上市/数据起点晚")

    eq_static = bh_static(close, cost)
    eq_monthly = bh_monthly(close, cost)
    qqq = load_benchmark("qqq", since=args.since).reindex(close.index).ffill()
    eq_qqq = qqq / qqq.iloc[0]

    rows = []
    for eq, label in ((eq_static, "BH-期初等权(纯持有)"),
                      (eq_monthly, "BH-月度等权(21日再平衡)"),
                      (eq_qqq, "QQQ")):
        ret = eq.pct_change().dropna()
        rows.append(perf_stats(eq, ret, label))
    df = pd.DataFrame(rows)
    print("\n" + df.to_string(index=False))
    print("\n对照（RESEARCH_LOG §14.4c，同期同成本口径）：")
    print("  策略-平衡档   +40.0%  Sharpe 0.89  MaxDD -38.1%")
    print("  策略-激进档   +44.0%  Sharpe 0.92  MaxDD -42.9%")

    out = Path(__file__).parent.parent / "report" / "benchmark_bh.csv"
    df.to_csv(out, index=False)
    print(f"\n已写 {out}")


if __name__ == "__main__":
    main()
