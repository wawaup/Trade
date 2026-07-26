#!/usr/bin/env python3
"""
walk_forward.py —— 真滚动窗口 walk-forward 参数验证（RESEARCH_LOG §15.2，2026-07-27）

回答审核第 2 问：`bear_flat_days=45`/`soft_drawdown` 等风控参数是否样本内过拟合。
此前 factor_combo_optimize.py 的 hold-out 是"假 OOS"（全样本调参后再切开看）；
本脚本做真 OOS：

  训练窗（默认 12 个月）网格选参 → 紧接的测试窗（默认 3 个月）用选出的参数交易
  → 窗口前滚 3 个月 → 重复 → 把所有测试窗净值段几何拼接成一条 OOS 曲线。

因子/Combo/regime 在全量面板一次计算（全部为因果滚动量，无前视），
各窗口只做日期切片；每个测试窗从净值 1.0 独立起跑再链乘
（简化：kill-switch 状态不跨窗延续，回撤记忆按窗重置——对风控参数偏乐观，
结果读法见 §15.2 结论）。

三条曲线对比：
  A. WF-选参   ：每窗用训练窗 Sharpe 最优参数（真 OOS，含参数选择过程）
  B. 冻结平衡档：全程固定 §14.4c 平衡档参数（bear_flat_days=45 + soft_drawdown
                 non_bull×5）跑同样的拼接窗口——A/B 差距 = 参数选择的贡献/伤害
  C. 月度等权  ：§15.1 的强制对照组，同窗拼接

用法：python3 walk_forward.py [--train-months 12] [--test-months 3]
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from factor_scanner import (load_universe, load_panel, load_benchmark,
                            build_liquidity_mask, compute_factors,
                            compute_spy_regime)
from factor_combo_backtest import (zscore_factors, compute_combo, CORE_FACTORS,
                                   run_backtest_live, perf_stats)
from benchmark_bh import bh_monthly

# 参数网格：只放本轮真正要裁决的风控参数（网格越大，选参过程自身的过拟合越重）
GRID = [
    {"bear_flat_days": bfd, "soft_drawdown": sd, "dd_ewma_span": span,
     "soft_drawdown_scope": scope}
    for bfd in (0, 5, 15, 45)
    for sd, span, scope in ((False, 5, "bear"), (True, 5, "bear"), (True, 40, "all"))
]
BALANCED = {"bear_flat_days": 45, "soft_drawdown": True,
            "dd_ewma_span": 5, "soft_drawdown_scope": "non_bull"}
FIXED_KW = dict(min_score=1.0, vol_min=1.2, top_n=5, rebalance=5,
                cost_bps=25.0, max_pos_pct=0.50, kill_dd=-0.30,
                exec_stage="moc_single", choppy_half=True, cooldown_min_days=20)


def slice_run(panels, start, end, cfg):
    """在 [start, end) 日期段上跑一次回测，返回 (净值序列, 日收益序列)。"""
    combo, vol_shock, close, open_px, liquid, spy, qqq, regime = panels
    sl = (close.index >= start) & (close.index < end)
    idx = close.index[sl]
    if len(idx) < 30:
        return None, None
    eq, _, _, ret, _, _ = run_backtest_live(
        combo.loc[idx], vol_shock.loc[idx], close.loc[idx], open_px.loc[idx],
        liquid.loc[idx], spy.loc[idx], qqq.loc[idx], regime.loc[idx],
        **FIXED_KW, **cfg)
    return eq.ffill(), ret


def wf_sharpe(ret):
    if ret is None or ret.std() == 0:
        return -99.0
    return float(ret.mean() * 252 / (ret.std() * np.sqrt(252)))


def main():
    ap = argparse.ArgumentParser(description="walk-forward 参数验证")
    ap.add_argument("--since", default="2022-01-01")
    ap.add_argument("--train-months", type=int, default=12)
    ap.add_argument("--test-months", type=int, default=3)
    args = ap.parse_args()

    symbols, _ = load_universe()
    print(f"加载 {len(symbols)} 只，全量计算因子（一次）...")
    close, high, low, vol, open_px = load_panel(symbols, since=args.since, include_open=True)
    qqq = load_benchmark("QQQ", since=args.since).reindex(close.index)
    spy = load_benchmark("SPY", since=args.since).reindex(close.index)
    liquid = build_liquidity_mask(close, vol)
    factors = compute_factors(close, high, low, vol, qqq)
    z = zscore_factors({f: factors[f] for f in CORE_FACTORS if f in factors}, liquid)
    regime, _ = compute_spy_regime(qqq, ma_window=50, buffer=0.0)
    combo = compute_combo(z, regime)
    vol_ma20 = vol.rolling(20).mean().replace(0, np.nan)
    vol_shock = (vol / vol_ma20).reindex(close.index)
    panels = (combo, vol_shock, close, open_px, liquid, spy, qqq, regime)

    # ── 窗口划分 ─────────────────────────────────────────────────────────────
    t0, t_end = close.index[0], close.index[-1]
    windows = []
    train_start = t0
    while True:
        train_end = train_start + pd.DateOffset(months=args.train_months)
        test_end = train_end + pd.DateOffset(months=args.test_months)
        if train_end >= t_end:
            break
        windows.append((train_start, train_end, min(test_end, t_end)))
        train_start = train_start + pd.DateOffset(months=args.test_months)
    print(f"共 {len(windows)} 个滚动窗（训练 {args.train_months}m / 测试 {args.test_months}m），"
          f"OOS 覆盖 {windows[0][1].date()} ~ {windows[-1][2].date()}")

    # ── 逐窗：训练选参 → 测试执行 ────────────────────────────────────────────
    picks, oos_rets_A, oos_rets_B = [], [], []
    for k, (tr0, tr1, te1) in enumerate(windows):
        scores = []
        for cfg in GRID:
            _, ret = slice_run(panels, tr0, tr1, cfg)
            scores.append(wf_sharpe(ret))
        best = GRID[int(np.argmax(scores))]
        _, ret_A = slice_run(panels, tr1, te1, best)
        _, ret_B = slice_run(panels, tr1, te1, BALANCED)
        if ret_A is None or ret_B is None:
            continue
        picks.append({"window": k + 1, "test": f"{tr1.date()}~{te1.date()}",
                      **best, "train_sharpe": round(max(scores), 2),
                      "oos_sharpe": round(wf_sharpe(ret_A), 2)})
        oos_rets_A.append(ret_A)
        oos_rets_B.append(ret_B)
        print(f"  窗{k+1:2d} 训练最优={best}  IS_Sharpe={max(scores):+.2f} "
              f"OOS_Sharpe={wf_sharpe(ret_A):+.2f}")

    ret_A = pd.concat(oos_rets_A)
    ret_B = pd.concat(oos_rets_B)
    eq_A = (1 + ret_A).cumprod()
    eq_B = (1 + ret_B).cumprod()

    # C：月度等权同窗拼接
    oos_start = windows[0][1]
    close_oos = close.loc[close.index >= oos_start]
    eq_C = bh_monthly(close_oos, 25.0 / 10_000)
    ret_C = eq_C.pct_change().dropna()

    qqq_oos = qqq.loc[qqq.index >= oos_start].ffill()
    eq_Q = qqq_oos / qqq_oos.iloc[0]
    ret_Q = eq_Q.pct_change().dropna()

    print("\n══ 拼接 OOS 绩效（覆盖 {} ~ {}）══".format(eq_A.index[0].date(), eq_A.index[-1].date()))
    rows = [perf_stats(eq_A, ret_A, "A: WF-逐窗选参（真 OOS）"),
            perf_stats(eq_B, ret_B, "B: 冻结平衡档（同窗拼接）"),
            perf_stats(eq_C, ret_C, "C: 月度等权对照"),
            perf_stats(eq_Q, ret_Q, "QQQ")]
    df = pd.DataFrame(rows)
    print(df.to_string(index=False))

    picks_df = pd.DataFrame(picks)
    print("\n逐窗选参明细：")
    print(picks_df.to_string(index=False))

    out_dir = Path(__file__).parent.parent / "report"
    df.to_csv(out_dir / "walk_forward_stats.csv", index=False)
    picks_df.to_csv(out_dir / "walk_forward_picks.csv", index=False)
    print(f"\n已写 report/walk_forward_stats.csv + walk_forward_picks.csv")


if __name__ == "__main__":
    main()
