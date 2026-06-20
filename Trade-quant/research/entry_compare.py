"""
4种入场信号对比
  h2_ma    : 2H MA触及+双根确认（当前基准策略）
  kdj_macd : KDJ超卖金叉 + MACD柱收敛/金叉
  swing_low: 摆动低点（下影长+量能，连续收涨）
  daily_ma : 日线MA20/MA60支撑反弹

运行：python3 entry_compare.py
"""
import pandas as pd
from backtest_engine import (
    load_raw, compute_indicators, run_backtest,
    StrategyConfig, auto_classify, compute_grid,
)

# ── 参数 ────────────────────────────────────────────────────────────────────
IS_START  = "2024-07-01"
IS_END    = "2024-12-31"
OOS_START = "2025-01-01"
OOS_END   = "2025-02-28"

SYMBOLS = [
    ("AVGO", "博通"),
    ("MU",   "美光"),
    ("AXTI", "AXTI"),
    ("MSFT", "微软"),
    ("TSLA", "特斯拉"),
]

MODES = ["h2_ma", "d_ma"]

# ── 辅助 ────────────────────────────────────────────────────────────────────
def fmt_pnl(v):
    if v is None:
        return "  n/a "
    sign = "+" if v >= 0 else ""
    return f"{sign}{v*100:5.1f}%"

def run_mode(df_full, is_start, is_end, oos_start, oos_end, mode, stype, grid_upper, grid_lower):
    df_oos = df_full.loc[oos_start:oos_end]
    if len(df_oos) < 5:
        return [], None

    cfg = StrategyConfig(
        stock_type   = stype,
        core_mode    = "auto",
        grid_upper   = grid_upper,
        grid_lower   = grid_lower,
        entry_signal = mode,
    )
    trades, eq = run_backtest(df_oos, cfg)
    return trades, eq

# ── 主流程 ───────────────────────────────────────────────────────────────────
print(f"\n{'='*72}")
print(f"  入场信号对比  IS={IS_START}~{IS_END}  OOS={OOS_START}~{OOS_END}")
print(f"{'='*72}\n")

for sym, name in SYMBOLS:
    print(f"{'─'*72}")
    print(f"  {sym} {name}")
    print(f"{'─'*72}")

    try:
        raw    = load_raw(sym, "1h")
        df_all = compute_indicators(raw)
    except Exception as e:
        print(f"  数据加载失败: {e}\n")
        continue

    df_is  = df_all.loc[IS_START:IS_END]
    stype  = auto_classify(df_is)
    g_up, g_lo = compute_grid(df_is) if stype == "volatile_vol" else (0.0, 0.0)
    print(f"  IS分类: {stype}  grid=({g_lo:.2f}, {g_up:.2f})\n")

    for mode in MODES:
        trades, eq = run_mode(df_all, IS_START, IS_END, OOS_START, OOS_END,
                              mode, stype, g_up, g_lo)

        core_buys  = [t for t in trades if t.action == "CORE_BUY"]
        core_stops = [t for t in trades if t.action in ("CORE_STOP", "CORE_CUT", "CORE_EOD")]
        t_trades   = [t for t in trades if t.action in ("T_BUY", "T_TP", "T_STOP", "T_EOD")]
        final_ret  = (trades[-1].equity / 10000 - 1) * 100 if trades else 0.0

        print(f"  [{mode:10s}]  建仓:{len(core_buys)}次  T操作:{len([x for x in t_trades if x.action=='T_BUY'])}次  最终:{final_ret:+.1f}%")

        for t in core_buys:
            ts = pd.Timestamp(t.time).strftime("%m-%d %H:%M")
            print(f"    ▲ CORE_BUY  {ts}  ${t.price:.2f}  {t.reason}")
        for t in core_stops:
            ts = pd.Timestamp(t.time).strftime("%m-%d %H:%M")
            print(f"    ▼ {t.action:9s} {ts}  ${t.price:.2f}  {fmt_pnl(t.pnl_pct)}  {t.reason}")

        if t_trades:
            print(f"    --- T仓 ---")
            for t in t_trades:
                ts  = pd.Timestamp(t.time).strftime("%m-%d %H:%M")
                pnl = fmt_pnl(t.pnl_pct) if t.pnl_pct is not None else "      "
                print(f"    {'△' if t.action=='T_BUY' else '▽'} {t.action:7s} {ts}  ${t.price:.2f}  {pnl}  {t.reason}")

        if not core_buys:
            print(f"    （无入场）")
        print()

print(f"{'='*72}")
print("  对比完成")
print(f"{'='*72}\n")
