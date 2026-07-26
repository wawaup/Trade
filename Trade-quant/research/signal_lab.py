#!/usr/bin/env python3
"""
signal_lab.py —— 新信号源筛选实验台（RESEARCH_LOG §15.7，2026-07-27）

§15.6 判决后重建主动层：在 B5 骨架（排序 Top-20 + 单票 10% + 平衡风控 +
vol_target 0.25，无绝对过滤）上，逐一评测候选信号与等权混合。

诚实约束（防止重蹈 §15.2 的覆辙）：
  1. 候选全部是文献级经典溢价的**标准定义**（Mom_12_1 / Prox_52W / LowVol_60），
     不做任何因子内参数调优；
  2. 权重只用等权混合，不做权重搜索、不做 regime 分表；
  3. 门槛固定为"2022+ 与 2023+ 两个子样本都在风险调整口径 ≥ 月度等权+VT0.25"，
     门槛与被评测对象在同一数据切片上现算；
  4. 每跑一个配置计一次多重检验，总次数如实写入 RESEARCH_LOG——
     本实验台跑的一切结果仍属样本内探索，最终裁决权在 paper OOS。

与旧 B5 复现口径的差异：面板加载全量历史（旧口径 --since 截断面板），
252 日因子在窗口起点即有效；因此本表内旧因子数字与 §15.3 略有出入，
比较只在本表内部进行。

用法：python3 signal_lab.py
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from factor_scanner import (load_universe, load_panel, load_benchmark,
                            build_liquidity_mask, compute_factors,
                            compute_spy_regime)
from factor_combo_backtest import zscore_factors, run_backtest_live, perf_stats
from benchmark_bh import bh_monthly

# B5 骨架 + VT（§15.6 冻结的评测环境，禁止修改）
SKELETON = dict(min_score=-1e9, vol_min=-1e9, top_n=20, rebalance=5,
                cost_bps=25.0, max_pos_pct=0.10, min_stocks=0, kill_dd=-0.30,
                exec_stage="moc_single", bear_flat_days=45, choppy_half=True,
                soft_drawdown=True, dd_ewma_span=5, soft_drawdown_scope="bear",
                cooldown_min_days=20, vol_target=0.25)

# 候选：单因子 + 等权混合（LowVol 仅作混合稳定器，单测预期平庸）
CANDIDATES = {
    "RS_Beta(旧,对照)":       {"RS_Beta": 1.0},
    "Mom_12_1":               {"Mom_12_1": 1.0},
    "Prox_52W":               {"Prox_52W": 1.0},
    "LowVol_60":              {"LowVol_60": 1.0},
    "Mom+Prox":               {"Mom_12_1": 0.5, "Prox_52W": 0.5},
    "Mom+Prox+LowVol":        {"Mom_12_1": 1/3, "Prox_52W": 1/3, "LowVol_60": 1/3},
    "Mom+Prox+LowVol+RS":     {"Mom_12_1": 0.25, "Prox_52W": 0.25,
                               "LowVol_60": 0.25, "RS_Beta": 0.25},
}
WINDOWS = ("2022-01-01", "2023-01-01")


def static_combo(z_panels: dict, weights: dict) -> pd.DataFrame:
    combo = None
    for f, w in weights.items():
        part = z_panels[f] * w
        combo = part if combo is None else combo.add(part, fill_value=0.0)
    return combo


def run_slice(combo, vol_shock, close, open_px, liquid, spy, qqq, regime, since):
    idx = close.index[close.index >= since]
    eq, _, _, ret, _, kills = run_backtest_live(
        combo.loc[idx], vol_shock.loc[idx], close.loc[idx], open_px.loc[idx],
        liquid.loc[idx], spy.loc[idx], qqq.loc[idx], regime.loc[idx], **SKELETON)
    return eq.ffill(), ret, len(kills)


def fmt(eq, ret, label, kills=None):
    s = perf_stats(eq, ret, label)
    out = f"{s['年化收益']:>7} / {s['Sharpe']:>5} / {s['最大回撤']:>7}"
    return out + (f" / 熔断×{kills}" if kills is not None else "")


def main():
    symbols, _ = load_universe()
    print(f"加载 {len(symbols)} 只（全量历史，252 日因子在窗口起点即有效）...")
    close, high, low, vol, open_px = load_panel(symbols, include_open=True)
    qqq = load_benchmark("QQQ").reindex(close.index)
    spy = load_benchmark("SPY").reindex(close.index)
    liquid = build_liquidity_mask(close, vol)
    factors = compute_factors(close, high, low, vol, qqq)
    need = sorted({f for w in CANDIDATES.values() for f in w})
    z = zscore_factors({f: factors[f] for f in need}, liquid)
    regime, _ = compute_spy_regime(qqq, ma_window=50, buffer=0.0)
    vol_ma20 = vol.rolling(20).mean().replace(0, np.nan)
    vol_shock = (vol / vol_ma20).reindex(close.index)

    # 门槛：月度等权 + VT0.25（同切片现算）
    gates = {}
    for since in WINDOWS:
        c = close.loc[close.index >= since]
        eq_ew = bh_monthly(c, 25.0 / 10_000)
        r = eq_ew.pct_change().fillna(0.0)
        ewm_var = r.pow(2).ewm(span=20).mean()
        mult = (0.25 / np.sqrt(ewm_var * 252)).clip(upper=1.0).shift(1).fillna(1.0)
        eq_g = (1 + r * mult).cumprod()
        gates[since] = (eq_g, eq_g.pct_change().dropna())
        print(f"门槛 EW+VT0.25 @ {since}+ : {fmt(eq_g, gates[since][1], 'gate')}")

    rows = []
    n_tests = 0
    for name, weights in CANDIDATES.items():
        combo = static_combo(z, weights)
        row = {"信号": name}
        for since in WINDOWS:
            eq, ret, kills = run_slice(combo, vol_shock, close, open_px,
                                       liquid, spy, qqq, regime, since)
            n_tests += 1
            row[f"{since[:4]}+"] = fmt(eq, ret, name, kills)
            g_eq, g_ret = gates[since]
            row[f"{since[:4]}胜门槛"] = "✅" if (
                (ret.mean() * 252 / (ret.std() * np.sqrt(252))) >=
                (g_ret.mean() * 252 / (g_ret.std() * np.sqrt(252)))) else "—"
        rows.append(row)
        print(f"  {name:24s} 2022+[{row['2022+']}]  2023+[{row['2023+']}]"
              f"  {row['2022胜门槛']}{row['2023胜门槛']}")

    df = pd.DataFrame(rows)
    out = Path(__file__).parent.parent / "report" / "signal_lab.csv"
    df.to_csv(out, index=False)
    print(f"\n多重检验计数：{n_tests} 次回测（写入 RESEARCH_LOG）")
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
