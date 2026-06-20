"""
Walk-Forward 验证引擎

传统回测在全历史上调好参数再回头解释，本质是"对着后视镜开车"。
Walk-Forward 把调参过程本身也变成有时间顺序的流水线：

  IS窗口（样本内）  → 网格搜索最优参数
  OOS窗口（样本外） → 用锁定参数跑一次，不允许回头修改
  拼接所有OOS结果   → 这才是策略真实能赚到的东西

关键诊断指标：
  OOS盈利率  > 50%      策略在样本外有正收益
  OOS跑赢持有 > 50%     主动管理有价值
  参数稳定性 ≥ 60%      最优参数没有随时间漂移（不是每个阶段都要重新调）
  IS/OOS效率比 ≈ 1.0    参数转移能力强；若 << 1 说明IS过拟合

用法：
    python walk_forward.py
    python walk_forward.py --is 4 --oos 2 --step 2
    python walk_forward.py --is 6 --oos 1 --metric return
"""
import argparse
import itertools
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest_engine import StrategyConfig, auto_classify, compute_grid, load_raw, compute_indicators, run_backtest


# ── 优化标的（流动性好、各赛道有代表性，用于IS参数搜索） ──────────────────
# stock_type 不再硬编码，每个窗口由 auto_classify(df_is) 动态决定
OPT_SYMBOLS = ["AVGO", "MU", "AXTI", "MSFT", "TSLA"]

# ── 参数搜索空间（刻意保持小规模：只搜最关键的2个维度） ───────────────────
# 参数越多 → 12组合变成48组合 → IS越容易过拟合优化过程本身
PARAM_GRID = {
    "atr_sl_mult":     [1.0, 1.5, 2.0, 2.5],
    "d_trend_min_mas": [3, 4, 5],
    "entry_signal":    ["d_ma"],   # 固定日线入场模式
}


@dataclass
class WFConfig:
    is_months:   int = 6      # 样本内窗口（月）
    oos_months:  int = 2      # 样本外窗口（月）
    step_months: int = 1      # 每次滚动步进（月）
    data_start:  str = "2024-07-01"
    data_end:    str = "2025-04-01"   # 两个窗口: W1 OOS=25-01~02, W2 OOS=25-02~03
    opt_metric:  str = "calmar"   # "calmar" | "return"
    min_trades:  int = 2          # 日线信号次数少，降低门槛


# ── 工具函数 ──────────────────────────────────────────────────────────────

def _iter_grid(grid: dict):
    keys = list(grid.keys())
    for vals in itertools.product(*grid.values()):
        yield dict(zip(keys, vals))


def _max_drawdown_pct(eq: pd.Series) -> float:
    """最大回撤（返回负数，如 -25.3 表示 25.3%）"""
    if eq.empty:
        return 0.0
    peak = eq.cummax()
    dd   = (eq - peak) / peak * 100
    return float(dd.min())


def _metric(trades, eq: pd.Series, initial: float, mode: str) -> float:
    if not trades or eq.empty:
        return -999.0
    ret = (trades[-1].equity / initial - 1)
    mdd = abs(_max_drawdown_pct(eq) / 100)
    if mode == "return":
        return ret
    # calmar：收益/最大回撤（比纯收益更能惩罚高回撤方案）
    return ret / mdd if mdd > 0.005 else ret


def _generate_windows(cfg: WFConfig):
    """生成 (is_start, is_end, oos_start, oos_end) 字符串元组列表"""
    windows = []
    cursor  = pd.Timestamp(cfg.data_start)
    end     = pd.Timestamp(cfg.data_end)

    while True:
        is_start  = cursor
        oos_start = cursor + pd.DateOffset(months=cfg.is_months)
        oos_end   = oos_start + pd.DateOffset(months=cfg.oos_months)

        if oos_end > end:
            break

        windows.append((
            is_start.strftime("%Y-%m-%d"),
            (oos_start - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            oos_start.strftime("%Y-%m-%d"),
            (oos_end - pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
        ))
        cursor += pd.DateOffset(months=cfg.step_months)

    return windows


# ── 主函数 ────────────────────────────────────────────────────────────────

def run_walk_forward(wf_cfg: WFConfig = None):
    if wf_cfg is None:
        wf_cfg = WFConfig()

    windows = _generate_windows(wf_cfg)
    if not windows:
        print("  数据区间太短，生成不了任何窗口。")
        return

    # ── 预加载数据（只做一次，避免重复读 parquet） ────────────────────────
    print("\n  预加载数据 & 计算指标...", flush=True)
    df_cache = {}
    for symbol in OPT_SYMBOLS:
        try:
            raw = load_raw(symbol, "1h")
            raw = raw.loc[wf_cfg.data_start:]
            df_cache[symbol] = compute_indicators(raw)
            print(f"    {symbol:12s} {len(raw)} 根K线", flush=True)
        except Exception as e:
            print(f"    {symbol:12s} 跳过：{e}", flush=True)

    if not df_cache:
        print("  没有可用数据，退出。")
        return

    n_combos = sum(1 for _ in _iter_grid(PARAM_GRID))
    print(f"\n  参数网格: {n_combos} 组合 × {len(df_cache)} 标的")
    print(f"  IS={wf_cfg.is_months}月  OOS={wf_cfg.oos_months}月  步进={wf_cfg.step_months}月  "
          f"共 {len(windows)} 个窗口")
    print(f"  stock_type 由每窗口 IS 数据动态分类（auto_classify）\n")

    # ── 滚动验证主循环 ─────────────────────────────────────────────────────
    window_results = []

    for wi, (is_s, is_e, oos_s, oos_e) in enumerate(windows):
        print(f"  [{wi+1:2d}/{len(windows)}] IS {is_s}~{is_e}  →  OOS {oos_s}~{oos_e}", flush=True)

        # 1. IS：网格搜索
        best_is_score = -np.inf
        best_params   = {}

        # 先为每个标的分类（基于 IS 数据，打印以便观察）
        is_stypes: dict[str, str] = {}
        for sym, df_full in df_cache.items():
            df_is = df_full.loc[is_s:is_e]
            is_stypes[sym] = auto_classify(df_is)
        type_str = "  ".join(f"{s}={t}" for s, t in is_stypes.items())
        print(f"         分类: {type_str}", flush=True)

        for params in _iter_grid(PARAM_GRID):
            scores = []
            for sym, df_full in df_cache.items():
                df_is = df_full.loc[is_s:is_e]
                if len(df_is) < 60:
                    continue
                stype    = is_stypes[sym]
                g_up, g_lo = compute_grid(df_is) if stype == "volatile_vol" else (0.0, 0.0)
                cfg_try  = StrategyConfig(stock_type=stype, core_mode="auto",
                                          grid_upper=g_up, grid_lower=g_lo, **params)
                try:
                    trades, eq = run_backtest(df_is, cfg_try)
                    n_sig = sum(1 for t in trades
                                if t.action in ("CORE_BUY", "T_BUY", "T_TP", "T_STOP"))
                    if n_sig < wf_cfg.min_trades:
                        continue
                    scores.append(_metric(trades, eq, cfg_try.initial_capital, wf_cfg.opt_metric))
                except Exception:
                    pass

            if scores:
                avg = float(np.mean(scores))
                if avg > best_is_score:
                    best_is_score = avg
                    best_params   = params

        if not best_params:
            print("         → IS数据不足，跳过此窗口")
            continue

        params_label = " ".join(f"{k}={v}" for k, v in best_params.items())
        print(f"         最优参数: [{params_label}]  IS-{wf_cfg.opt_metric}={best_is_score:+.3f}", flush=True)

        # 2. OOS：锁定参数，只跑一次（这里不允许回头修改）
        oos_rets  = {}
        oos_mdds  = {}
        hold_rets = {}
        oos_alpha = {}

        for sym, df_full in df_cache.items():
            df_oos = df_full.loc[oos_s:oos_e]
            if len(df_oos) < 20:
                continue
            stype      = is_stypes[sym]
            df_is_w    = df_cache[sym].loc[is_s:is_e]
            g_up, g_lo = compute_grid(df_is_w) if stype == "volatile_vol" else (0.0, 0.0)
            cfg_oos    = StrategyConfig(stock_type=stype, core_mode="auto",
                                        grid_upper=g_up, grid_lower=g_lo, **best_params)
            try:
                trades, eq = run_backtest(df_oos, cfg_oos)
                if not trades:
                    continue
                ini = cfg_oos.initial_capital
                ret = (trades[-1].equity / ini - 1) * 100
                mdd = _max_drawdown_pct(eq)
                hld = (df_oos["close"].iloc[-1] / df_oos["close"].iloc[0] - 1) * cfg_oos.core_pct * 100

                oos_rets[sym]  = ret
                oos_mdds[sym]  = mdd
                hold_rets[sym] = hld
                oos_alpha[sym] = ret - hld
            except Exception:
                pass

        if oos_rets:
            avg_oos = np.mean(list(oos_rets.values()))
            avg_hld = np.mean(list(hold_rets.values()))
            print(f"         OOS均收益={avg_oos:+.1f}%  持有均={avg_hld:+.1f}%  "
                  f"α={avg_oos-avg_hld:+.1f}%\n")

        window_results.append({
            "is_s": is_s, "is_e": is_e, "oos_s": oos_s, "oos_e": oos_e,
            "best_params": best_params,
            "is_score":    best_is_score,
            "oos_rets":    oos_rets,
            "oos_mdds":    oos_mdds,
            "hold_rets":   hold_rets,
            "oos_alpha":   oos_alpha,
        })

    if not window_results:
        print("  没有有效窗口结果，退出。")
        return

    _print_summary(window_results, wf_cfg)


def _print_summary(window_results, wf_cfg):
    w = 112
    n_win = len(window_results)

    print(f"\n  {'═'*w}")
    print(f"  Walk-Forward 汇总  "
          f"IS={wf_cfg.is_months}月 OOS={wf_cfg.oos_months}月 步进={wf_cfg.step_months}月  "
          f"{n_win} 个有效窗口  优化目标={wf_cfg.opt_metric}")
    print(f"  {'═'*w}")

    # ── 各窗口明细 ────────────────────────────────────────────────────────
    print(f"\n  ┌── 各窗口 OOS 明细 {'─'*89}")
    print(f"  │  {'OOS区间':<24} {'最优参数':<35} {'IS得分':>9} {'OOS均%':>8} {'持有均%':>8} {'α均%':>7}")
    print(f"  │  {'─'*96}")

    all_oos_rets = []
    all_hld_rets = []

    for r in window_results:
        params_str = " ".join(f"{k}={v}" for k, v in r["best_params"].items())
        avg_oos = np.mean(list(r["oos_rets"].values()))  if r["oos_rets"]  else 0.0
        avg_hld = np.mean(list(r["hold_rets"].values())) if r["hold_rets"] else 0.0
        avg_a   = avg_oos - avg_hld

        mark = "▲" if avg_oos > 0 else "▼"
        a_mk = "▲" if avg_a  > 0 else "▼"
        print(f"  │  {r['oos_s']}~{r['oos_e']}  "
              f"{params_str:<35} "
              f"{r['is_score']:>+9.3f} "
              f"{avg_oos:>+7.1f}%{mark} "
              f"{avg_hld:>+7.1f}%  "
              f"{avg_a:>+6.1f}%{a_mk}")

        all_oos_rets.extend(r["oos_rets"].values())
        all_hld_rets.extend(r["hold_rets"].values())

    print(f"  └{'─'*98}")

    # ── 每个标的的 OOS 跨窗口汇总 ────────────────────────────────────────
    syms = list({sym for r in window_results for sym in r["oos_rets"]})
    print(f"\n  ┌── 各标的 OOS 跨窗口表现 {'─'*81}")
    print(f"  │  {'标的':<12} {'各窗口OOS%':<55} {'均%':>7} {'均α%':>7} {'胜率':>6}")
    print(f"  │  {'─'*90}")

    for sym in sorted(syms):
        sym_oos = [r["oos_rets"][sym]   for r in window_results if sym in r["oos_rets"]]
        sym_hld = [r["hold_rets"][sym]  for r in window_results if sym in r["hold_rets"]]
        sym_a   = [o - h for o, h in zip(sym_oos, sym_hld)]

        vals_str = "  ".join(f"{v:+.1f}%" for v in sym_oos)
        avg_oos  = np.mean(sym_oos) if sym_oos else 0.0
        avg_a    = np.mean(sym_a)   if sym_a   else 0.0
        win_rate = sum(1 for v in sym_oos if v > 0) / len(sym_oos) * 100 if sym_oos else 0

        print(f"  │  {sym:<12} {vals_str:<55} {avg_oos:>+6.1f}% {avg_a:>+6.1f}% {win_rate:>5.0f}%")

    print(f"  └{'─'*92}")

    # ── 核心诊断 ──────────────────────────────────────────────────────────
    if not all_oos_rets:
        return

    total      = len(all_oos_rets)
    pos_oos    = sum(1 for v in all_oos_rets if v > 0)
    beat_hold  = sum(1 for o, h in zip(all_oos_rets, all_hld_rets) if o > h)
    avg_oos_g  = np.mean(all_oos_rets)
    avg_hld_g  = np.mean(all_hld_rets)
    oos_wr     = pos_oos / total * 100
    beat_rate  = beat_hold / total * 100
    alpha_avg  = avg_oos_g - avg_hld_g

    # IS-OOS 效率比：OOS均收益 / IS最优Calmar均值（不同量纲，仅看方向）
    avg_is = np.mean([r["is_score"] for r in window_results])

    # 参数稳定性
    param_stability = {}
    for key in PARAM_GRID:
        vals = [r["best_params"].get(key) for r in window_results if r["best_params"]]
        if vals:
            cnt  = Counter(vals)
            stab = max(cnt.values()) / len(vals) * 100
            param_stability[key] = (stab, cnt)

    print(f"\n  ┌── 核心诊断 {'─'*94}")

    def _diag(label, val, thresh_ok, thresh_warn, fmt=".0f", suffix="%", ok="✓", warn="△", bad="✗"):
        mark = ok if val >= thresh_ok else (warn if val >= thresh_warn else bad)
        tip  = ("健康" if val >= thresh_ok else ("边缘" if val >= thresh_warn else "过拟合警告"))
        print(f"  │  {label:<28} {val:{fmt}}{suffix}  {mark} {tip}")

    _diag("OOS 盈利率",          oos_wr,    50, 40)
    _diag("OOS 跑赢持有比例",    beat_rate, 50, 40)
    print(f"  │  {'OOS 均收益率':<28} {avg_oos_g:+.1f}%")
    print(f"  │  {'OOS 均α（vs持有）':<28} {alpha_avg:+.1f}%")
    print(f"  │  {'IS 均得分（参考上限）':<28} {avg_is:+.3f}  (IS越好≠OOS越好，两者差距=过拟合程度)")
    print(f"  │")
    print(f"  │  参数稳定性：")
    for key, (stab, cnt) in param_stability.items():
        val_str = "  ".join(f"{v}×{c}" for v, c in sorted(cnt.items()))
        mark = "✓稳定" if stab >= 60 else ("△一般" if stab >= 40 else "✗漂移严重")
        print(f"  │    {key:<25} [{val_str}]  最常见占{stab:.0f}%  {mark}")

    print(f"  │")
    print(f"  │  解读：")
    if oos_wr > 50 and beat_rate > 50:
        print(f"  │    OOS盈利率和跑赢持有率均>50% → 策略逻辑在样本外有正向有效性")
    elif oos_wr > 50:
        print(f"  │    OOS能盈利但跑不赢持有 → 主动管理没有产生净alpha，持有更划算")
    else:
        print(f"  │    OOS盈利率<50% → 策略在未见数据上无法稳定盈利，参数过拟合IS")
    stable_params = [k for k, (s, _) in param_stability.items() if s >= 60]
    drift_params  = [k for k, (s, _) in param_stability.items() if s < 40]
    if stable_params:
        print(f"  │    参数稳定（≥60%）: {stable_params} → 这些参数跨周期有效")
    if drift_params:
        print(f"  │    参数漂移（<40%）: {drift_params} → 市场在变，这些参数需要定期重调")

    print(f"  └{'─'*98}\n")


# ── 命令行入口 ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Walk-Forward 验证引擎")
    parser.add_argument("--is",     type=int, default=6,       dest="is_months",   help="IS窗口（月，默认6）")
    parser.add_argument("--oos",    type=int, default=2,       dest="oos_months",  help="OOS窗口（月，默认2）")
    parser.add_argument("--step",   type=int, default=2,       dest="step_months", help="步进（月，默认2）")
    parser.add_argument("--start",  type=str, default="2024-07-01", dest="data_start")
    parser.add_argument("--metric", type=str, default="calmar",
                        choices=["calmar", "return"], help="优化目标（默认calmar）")
    args = parser.parse_args()

    print("\n" + "═" * 60)
    print("  Walk-Forward 验证引擎")
    print("═" * 60)
    print(f"  IS={args.is_months}月  OOS={args.oos_months}月  步进={args.step_months}月")
    print(f"  优化标的: {OPT_SYMBOLS}")
    print(f"  参数网格: atr_sl_mult × d_trend_min_mas = "
          f"{len(PARAM_GRID['atr_sl_mult'])} × {len(PARAM_GRID['d_trend_min_mas'])} = "
          f"{len(PARAM_GRID['atr_sl_mult']) * len(PARAM_GRID['d_trend_min_mas'])} 组合")

    cfg = WFConfig(
        is_months=args.is_months,
        oos_months=args.oos_months,
        step_months=args.step_months,
        data_start=args.data_start,
        opt_metric=args.metric,
    )
    run_walk_forward(cfg)


if __name__ == "__main__":
    main()
