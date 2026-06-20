"""
多标的 / 多时间段回测对比脚本。

核心问题：主动管理（核心仓+T仓）是否优于同仓位被动持有？

三列对比：
  持有%  = 用同等核心仓比例买入持有（蓝筹80%/波动70%仓，其余现金）
  策略%  = 核心仓主动管理 + T仓
  α%     = 策略% - 持有%（正=主动管理赚到了，负=不如直接持有）
  T贡献% = T仓所有交易盈亏之和 / 初始资金（正=T仓有价值，负=T仓拖累）

用法：
    python compare_configs.py
"""
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from backtest_engine import (
    StrategyConfig, load_raw, compute_indicators, run_backtest
)

SYMBOLS = [
    # 美股
    ("NVDA",       "volatile"),
    ("TSLA",       "volatile"),
    ("AAPL",       "bluechip"),
    ("MSFT",       "bluechip"),
    ("MU",         "volatile"),
    # 加密
    ("BTC-USD",    "volatile"),
    ("ETH-USD",    "volatile"),
    # 韩股（VWAP时区近似）
    ("000660.KS",  "volatile"),   # SK海力士
    ("005930.KS",  "volatile"),   # 三星电子（HBM/存储，走势更像波动股）
    # 港股（VWAP时区近似）
    ("1810.HK",    "volatile"),   # 小米
]

PERIODS = [
    ("全期",   "2024-07-01", None),
    ("2024H2", "2024-07-01", "2024-12-31"),
    ("2025+",  "2025-01-01", None),
]


# ── 买入持有基准 ───────────────────────────────────────────────────────────

def buy_and_hold_return(raw: pd.DataFrame, core_pct: float = 0.70) -> float:
    """
    同等仓位买入持有：用 core_pct 比例资金建仓，其余现金（收益率=0）。
    这样与策略的核心仓形成公平对比（策略也只用 core_pct 买入核心仓）。
    """
    first = raw["close"].iloc[0]
    last  = raw["close"].iloc[-1]
    stock_ret = (last / first - 1)
    return stock_ret * core_pct * 100


# ── 策略回测 + 拆解 ────────────────────────────────────────────────────────

CORE_BUY_ACTIONS  = {"CORE_BUY"}
CORE_SELL_ACTIONS = {"CORE_EOD", "CORE_STOP"}


def run_and_decompose(symbol, stock_type, since, until=None, d_trend_min_mas=6):
    """
    返回字典：
      hold%      区间全仓持有收益
      strategy%  策略总收益
      alpha%     strategy - hold（超额α）
      t_pnl%     T仓累计盈亏 / 初始资金（T仓单独贡献）
      core_turns 核心仓操作次数（含分段买卖）
      t_entries  T仓入场次数
      t_winrate  T仓胜率%
    """
    try:
        raw = load_raw(symbol, "1h")
        raw = raw.loc[since:] if since else raw
        if until:
            raw = raw.loc[:until]
        if len(raw) < 80:
            return None

        df  = compute_indicators(raw)
        cfg = StrategyConfig(stock_type=stock_type, d_trend_min_mas=d_trend_min_mas)
        hold_ret  = buy_and_hold_return(raw, core_pct=cfg.core_pct)
        trades, equity_series = run_backtest(df, cfg)

        initial   = cfg.initial_capital
        final     = trades[-1].equity if trades else initial
        strat_ret = (final / initial - 1) * 100
        alpha     = strat_ret - hold_ret

        t_exits = [t for t in trades if t.action in ("T_TP", "T_STOP", "T_EOD")]
        t_buys  = [t for t in trades if t.action == "T_BUY"]
        t_pnls  = [t.pnl_pct for t in t_exits if t.pnl_pct is not None]
        t_pos_value  = initial * cfg.t_pct
        t_contribution = sum(p * t_pos_value for p in t_pnls) / initial * 100
        t_win = (sum(1 for p in t_pnls if p > 0) / len(t_pnls) * 100) if t_pnls else 0

        core_ops = [t for t in trades if t.action in CORE_BUY_ACTIONS | CORE_SELL_ACTIONS]

        # 归一化净值曲线（100基准）和标的价格曲线
        eq_norm    = (equity_series / initial * 100).round(2)
        hold_norm  = (raw["close"] / raw["close"].iloc[0] * 100).round(2)
        # 对齐时间轴
        common_idx = eq_norm.index.intersection(hold_norm.index)
        eq_norm    = eq_norm.reindex(common_idx).ffill()
        hold_norm  = hold_norm.reindex(common_idx).ffill()

        times = [str(x)[:16] for x in common_idx]

        return {
            "hold%":      round(hold_ret, 1),
            "strategy%":  round(strat_ret, 1),
            "alpha%":     round(alpha, 1),
            "t_pnl%":     round(t_contribution, 1),
            "core_turns": len(core_ops),
            "t_entries":  len(t_buys),
            "t_winrate":  round(t_win, 0),
            # 曲线数据（给HTML图表用）
            "times":      times,
            "eq_curve":   eq_norm.tolist(),
            "hold_curve": hold_norm.tolist(),
            # 交易标记（给图表标点用）
            "trade_marks": [
                {"t": str(tr.time)[:16], "action": tr.action,
                 "price": tr.price, "pnl": tr.pnl_pct}
                for tr in trades
            ],
        }
    except Exception as e:
        return {"err": str(e)[:80]}


# ── 打印 ──────────────────────────────────────────────────────────────────

def fmt(v, plus=True):
    if not isinstance(v, (int, float)):
        return f"{'—':>7}"
    return f"{v:>+7.1f}" if plus else f"{v:>7}"


def print_table(period_label, since, until, rows):
    until_label = until or "2026-06"
    w = 78
    print(f"\n  ┌── {period_label}  {since} ~ {until_label} {'─'*(w-len(period_label)-20)}")
    print(f"  │  {'标的':<13} {'同仓持有%':>9} {'策略%':>7} {'α%':>7} {'T贡献%':>7}  "
          f"{'核心换手':>6} {'T入场':>5} {'T胜率':>5}")
    print(f"  │  {'─'*w}")

    beat_hold = 0
    t_positive = 0
    valid = 0

    for symbol, stype, res in rows:
        label = f"{symbol}({'蓝' if stype=='bluechip' else '波'})"
        if res is None or "err" in res:
            err = res["err"] if res else "数据不足"
            print(f"  │  {label:<13}  {err}")
            continue

        h  = res["hold%"]
        s  = res["strategy%"]
        a  = res["alpha%"]
        tp = res["t_pnl%"]
        ct = res["core_turns"]
        te = res["t_entries"]
        tw = res["t_winrate"]

        # 颜色标记（ASCII）
        a_mark  = "▲" if a > 0 else ("▼" if a < 0 else "=")
        tp_mark = "+" if tp > 0 else ("-" if tp < 0 else "=")

        print(f"  │  {label:<13} {fmt(h)} {fmt(s)} {fmt(a)}{a_mark} {fmt(tp)}{tp_mark}  "
              f"{ct:>6} {te:>5} {tw:>4.0f}%")

        valid += 1
        if a > 0:
            beat_hold += 1
        if tp > 0:
            t_positive += 1

    print(f"  │  {'─'*w}")
    if valid:
        print(f"  │  跑赢持有: {beat_hold}/{valid}  T仓正贡献: {t_positive}/{valid}")
    print(f"  └{'─'*(w+2)}")


def main():
    print("\n正在运行多标的 / 多时间段回测（含持有基准对比）...")

    all_results = {}
    for period_label, since, until in PERIODS:
        rows = []
        for symbol, stype in SYMBOLS:
            res = run_and_decompose(symbol, stype, since, until)
            rows.append((symbol, stype, res))
        all_results[period_label] = (since, until, rows)

    print(f"\n{'='*86}")
    print(f"  多标的对比  配置：日线过滤6档 + 2H双根核心入场 + T仓2H MA5出场")
    print(f"  α% = 策略收益 − 区间持有收益  |  T贡献% = T仓盈亏/初始资金")
    print(f"  ▲跑赢持有  ▼跑输持有  +T仓正贡献  -T仓负贡献")
    print(f"  注：韩股/港股VWAP时区用美股近似，数字仅供参考")
    print(f"{'='*86}")

    for period_label, (since, until, rows) in all_results.items():
        print_table(period_label, since, until, rows)

    # ── 日线过滤档位对比（全期，α% 汇总）─────────────────────────────────
    print(f"\n  ┌── 日线多头过滤档位对比（全期α% | 越高=主动管理越有价值）{'─'*15}")
    filter_labels = {
        3: "MA5>10>20",
        4: "MA5>10>20>30",
        5: "MA5>10>20>30>60",
        6: "MA5>10>20>30>60>250",
    }
    print(f"  │  {'标的':<13} {'档位3':>8} {'档位4':>8} {'档位5':>8} {'档位6':>8}")
    print(f"  │  {'─'*50}")
    for symbol, stype in SYMBOLS:
        row_vals = {}
        for lvl in [3, 4, 5, 6]:
            res = run_and_decompose(symbol, stype, "2024-07-01", None,
                                    d_trend_min_mas=lvl)
            row_vals[lvl] = res.get("alpha%") if res and "err" not in res else None
        label = f"{symbol}({'蓝' if stype=='bluechip' else '波'})"
        def fv(v): return f"{v:>+8.1f}" if v is not None else f"{'—':>8}"
        print(f"  │  {label:<13} {fv(row_vals[3])} {fv(row_vals[4])} {fv(row_vals[5])} {fv(row_vals[6])}")
    print(f"  └{'─'*55}")
    print(f"  说明：档位越低=过滤越松=入场越早；正α=主动管理跑赢同仓位持有")

    # ── 跨期汇总矩阵 ─────────────────────────────────────────────────────
    print(f"\n  ┌── 跨期超额α%矩阵（策略收益 − 全仓持有收益，正=有价值）{'─'*20}")
    print(f"  │  {'标的':<13} {'全期α':>8} {'2024H2 α':>9} {'2025+ α':>9}  综合判断")
    print(f"  │  {'─'*65}")

    for symbol, stype in SYMBOLS:
        vals = {}
        for pl, (_, _, rows) in all_results.items():
            for s, _, r in rows:
                if s == symbol and r and "err" not in r:
                    vals[pl] = r["alpha%"]

        label = f"{symbol}({'蓝' if stype=='bluechip' else '波'})"
        af  = vals.get("全期",   None)
        ah2 = vals.get("2024H2", None)
        a25 = vals.get("2025+",  None)

        def verdict(af, ah2, a25):
            valid = [v for v in [af, ah2, a25] if v is not None]
            if not valid:
                return "—"
            pos = sum(1 for v in valid if v > 0)
            if pos == len(valid):
                return "✓ 全期跑赢"
            elif pos == 0:
                return "✗ 全期跑输"
            elif ah2 is not None and a25 is not None:
                if ah2 < 0 and a25 > 0:
                    return "△ 熊市跑输/牛市跑赢"
                if ah2 > 0 and a25 < 0:
                    return "△ 熊市跑赢/牛市跑输"
            return f"△ {pos}/{len(valid)}期跑赢"

        af_s  = f"{af:>+8.1f}" if af  is not None else f"{'—':>8}"
        ah2_s = f"{ah2:>+9.1f}" if ah2 is not None else f"{'—':>9}"
        a25_s = f"{a25:>+9.1f}" if a25 is not None else f"{'—':>9}"

        print(f"  │  {label:<13} {af_s} {ah2_s} {a25_s}  {verdict(af, ah2, a25)}")

    print(f"  └{'─'*70}")
    print()
    print("  【关键问题】α% 为负说明做T的摩擦损耗 > 主动管理收益，策略在该标的上是负贡献。")
    print("  【T仓意义】T贡献% 为负说明T仓不如不做，应考虑禁用该标的的T交易。")


if __name__ == "__main__":
    main()
