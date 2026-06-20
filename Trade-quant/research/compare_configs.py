"""
多标的 / 多时间段回测对比脚本。

核心问题：主动管理（核心仓+T仓）是否优于同仓位被动持有？

α% = 策略% - 持有%（正=主动管理赚到了，负=不如直接持有）
T贡献% = T仓盈亏之和 / 初始资金

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

# ── 标的列表：(代码, 波动类型, 显示名称, 赛道) ────────────────────────────
# 波动分类依据：2024-07 以来日内振幅>5%占比 ≥20% = 大波动，<20% = 小波动

SYMBOLS = [
    # AI 算力
    ("NVDA",      "small_vol", "NVDA",    "AI算力"),      # 16%
    ("AMD",       "small_vol", "AMD",     "AI算力"),      #  4%
    # AI 芯片 & 网络
    ("AVGO",      "small_vol", "AVGO",    "AI芯片&网络"), #  3%
    ("MRVL",      "small_vol", "MRVL",    "AI芯片&网络"), #  7%
    # HBM 存储
    ("MU",        "large_vol", "MU",      "HBM存储"),     # 30%
    ("SNDK",      "large_vol", "SNDK",    "HBM存储"),     # 22%
    ("000660.KS", "large_vol", "SK海力士","HBM存储"),     # 20%
    ("005930.KS", "small_vol", "三星",    "HBM存储"),     # 10%
    # 半导体上游
    ("TSM",       "small_vol", "TSM",     "半导体上游"),  #  1%
    ("ASML",      "small_vol", "ASML",    "半导体上游"),  #  1%
    # AI 云平台
    ("MSFT",      "small_vol", "MSFT",    "AI云平台"),    #  1%
    ("GOOGL",     "small_vol", "GOOGL",   "AI云平台"),    #  1%
    # 网络安全
    ("CRWD",      "small_vol", "CRWD",    "网络安全"),    #  2%
    ("PANW",      "small_vol", "PANW",    "网络安全"),    #  2%
    # 量子（高波动主题）
    ("IONQ",      "large_vol", "IONQ",    "量子"),        # 35%
    ("QBTS",      "large_vol", "QBTS",    "量子"),        # 44%
    # 太空防务（高波动主题）
    ("RKLB",      "large_vol", "RKLB",    "太空防务"),    # 23%
    ("LMT",       "small_vol", "LMT",     "太空防务"),    #  1%
    # 加密货币
    ("ETH-USD",   "large_vol", "ETH",     "加密货币"),    # 43%
    ("BTC-USD",   "large_vol", "BTC",     "加密货币"),    # 19%→加密归大
    # 消费科技
    ("TSLA",      "large_vol", "TSLA",    "消费科技"),    # 29%
    ("1810.HK",   "small_vol", "小米",    "消费科技"),    # 19%
    ("AAPL",      "small_vol", "AAPL",    "消费科技"),    #  3%
]

# ── Serenity 专属选股池 ────────────────────────────────────────────────────
# Serenity 风格：不买最显眼的主线，往上游/平台/基础设施渗透
SYMBOLS_SERENITY = [
    # 光通信 / CPO / 光子学
    ("AAOI",  "large_vol", "AAOI",   "光通信CPO"),   # 41%
    ("AXTI",  "large_vol", "AXTI",   "光通信CPO"),   # 39%
    ("LITE",  "small_vol", "LITE",   "光通信CPO"),   # 12%
    ("COHR",  "small_vol", "COHR",   "光通信CPO"),   # 11%
    ("FN",    "small_vol", "FN",     "光通信CPO"),   #  8%
    ("NOK",   "small_vol", "NOK",    "光通信CPO"),   #  3%
    # 半导体制造 / 封测 / ASIC
    ("TSEM",  "small_vol", "TSEM",   "封测ASIC"),    #  6%
    ("GFS",   "small_vol", "GFS",    "封测ASIC"),    #  4%
    ("ASX",   "small_vol", "ASE",    "封测ASIC"),    #  2%  ASE Technology
    ("AMKR",  "small_vol", "AMKR",   "封测ASIC"),    #  5%
    ("AEHR",  "large_vol", "AEHR",   "封测ASIC"),    # 29%
    ("ACMR",  "small_vol", "ACMR",   "封测ASIC"),    #  9%
    # AI 电力 / 数据中心基础设施
    ("VRT",   "small_vol", "VRT",    "AI电力DC"),    #  7%
    ("VICR",  "small_vol", "VICR",   "AI电力DC"),    # 13%
    ("ETN",   "small_vol", "ETN",    "AI电力DC"),    #  1%
    ("SIEGY", "small_vol", "Siemens","AI电力DC"),    #  0%
    # 存储 / HBM / NAND
    ("MU",    "large_vol", "MU",     "存储HBM"),     # 30%
    ("SNDK",  "large_vol", "SNDK",   "存储HBM"),     # 22%
    ("EWY",   "small_vol", "EWY",    "韩股ETF"),     #  2%
]

PERIODS = [
    ("全期",   "2024-07-01", None),
    ("2024H2", "2024-07-01", "2024-12-31"),
    ("2025+",  "2025-01-01", None),
]


# ── 买入持有基准 ──────────────────────────────────────────────────────────

def buy_and_hold_return(raw: pd.DataFrame, core_pct: float = 0.70) -> float:
    first = raw["close"].iloc[0]
    last  = raw["close"].iloc[-1]
    return (last / first - 1) * core_pct * 100


# ── 策略回测 + 拆解 ───────────────────────────────────────────────────────

CORE_BUY_ACTIONS  = {"CORE_BUY"}
CORE_SELL_ACTIONS = {"CORE_EOD", "CORE_STOP"}


def run_and_decompose(symbol, stock_type, since, until=None, d_trend_min_mas=4):
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
        t_contribution = sum(p * initial * cfg.t_pct for p in t_pnls) / initial * 100
        t_win = (sum(1 for p in t_pnls if p > 0) / len(t_pnls) * 100) if t_pnls else 0

        core_ops = [t for t in trades if t.action in CORE_BUY_ACTIONS | CORE_SELL_ACTIONS]

        eq_norm   = (equity_series / initial * 100).round(2)
        hold_norm = (raw["close"] / raw["close"].iloc[0] * 100).round(2)
        common_idx = eq_norm.index.intersection(hold_norm.index)
        eq_norm    = eq_norm.reindex(common_idx).ffill()
        hold_norm  = hold_norm.reindex(common_idx).ffill()
        times = [str(x)[:16] for x in common_idx]

        def max_drawdown(series):
            peak = series.cummax()
            dd   = (series - peak) / peak * 100
            return round(dd.min(), 1)

        strat_mdd = max_drawdown(equity_series.reindex(common_idx).ffill())
        hold_price = raw["close"].reindex(common_idx).ffill() * cfg.core_pct
        hold_mdd  = max_drawdown(hold_price)

        return {
            "hold%":      round(hold_ret, 1),
            "strategy%":  round(strat_ret, 1),
            "alpha%":     round(alpha, 1),
            "t_pnl%":     round(t_contribution, 1),
            "hold_mdd%":  hold_mdd,
            "strat_mdd%": strat_mdd,
            "core_turns": len(core_ops),
            "t_entries":  len(t_buys),
            "t_winrate":  round(t_win, 0),
            "times":      times,
            "eq_curve":   eq_norm.tolist(),
            "hold_curve": hold_norm.tolist(),
            "trade_marks": [
                {"t": str(tr.time)[:16], "action": tr.action,
                 "price": tr.price, "pnl": tr.pnl_pct}
                for tr in trades
            ],
        }
    except Exception as e:
        return {"err": str(e)[:80]}


# ── 格式化 ────────────────────────────────────────────────────────────────

def fmt(v, plus=True):
    if not isinstance(v, (int, float)):
        return f"{'—':>7}"
    return f"{v:>+7.1f}" if plus else f"{v:>7}"


def print_table(period_label, since, until, rows, title=""):
    until_label = until or "2026-06"
    hdr = f"{period_label}  {since} ~ {until_label}"
    if title:
        hdr = f"{title} │ {hdr}"
    w = 114
    print(f"\n  ┌── {hdr} {'─'*max(0, w-len(hdr)-6)}")
    print(f"  │  {'标的':<10} {'赛道':<11} {'同仓持有%':>9} {'策略%':>7} {'α%':>7} "
          f"{'T贡献%':>7}  {'持有回撤%':>9} {'策略回撤%':>9}  {'换手':>4} {'T入场':>5} {'T胜率':>5}  盈利  波动")
    print(f"  │  {'─'*w}")

    beat_hold  = 0
    t_positive = 0
    profitable = 0
    valid      = 0
    last_sector = None

    for symbol, stype, name, sector, res in rows:
        if sector != last_sector and last_sector is not None:
            print(f"  │  {'·'*w}")
        last_sector = sector

        vol_label = "大" if stype == "large_vol" else "小"
        if res is None or "err" in res:
            err = res["err"] if res else "数据不足"
            print(f"  │  {name:<10} {sector:<11}  {err}")
            continue

        h  = res["hold%"];  s = res["strategy%"]
        a  = res["alpha%"]; tp = res["t_pnl%"]
        hm = res.get("hold_mdd%", 0); sm = res.get("strat_mdd%", 0)
        ct = res["core_turns"]; te = res["t_entries"]; tw = res["t_winrate"]

        a_mark    = "▲" if a  > 0 else ("▼" if a  < 0 else "=")
        tp_mark   = "+" if tp > 0 else ("-" if tp < 0 else "=")
        pnl_mark  = "✅" if s  > 0 else "❌"

        print(f"  │  {name:<10} {sector:<11} {fmt(h)} {fmt(s)} {fmt(a)}{a_mark} "
              f"{fmt(tp)}{tp_mark}  {fmt(hm)} {fmt(sm)}  {ct:>4} {te:>5} {tw:>4.0f}%  {pnl_mark}  {vol_label}")

        valid += 1
        if a > 0:  beat_hold  += 1
        if tp > 0: t_positive += 1
        if s > 0:  profitable += 1

    print(f"  │  {'─'*w}")
    if valid:
        print(f"  │  策略盈利: {profitable}/{valid}  跑赢持有: {beat_hold}/{valid}  T仓正贡献: {t_positive}/{valid}")
    print(f"  └{'─'*(w+2)}")


def main():
    print("\n正在运行多标的 / 多时间段回测...")

    # ── 收集全部结果 ─────────────────────────────────────────────────────
    all_main     = {}
    all_serenity = {}

    for period_label, since, until in PERIODS:
        rows_main = []
        for symbol, stype, name, sector in SYMBOLS:
            res = run_and_decompose(symbol, stype, since, until)
            rows_main.append((symbol, stype, name, sector, res))
        all_main[period_label] = (since, until, rows_main)

        rows_ser = []
        for symbol, stype, name, sector in SYMBOLS_SERENITY:
            res = run_and_decompose(symbol, stype, since, until)
            rows_ser.append((symbol, stype, name, sector, res))
        all_serenity[period_label] = (since, until, rows_ser)

    # ── 表头说明 ────────────────────────────────────────────────────────
    print(f"\n{'='*100}")
    print(f"  科技主线 + Serenity 主题  |  日线MA5>10>20>30 + 2H双根入场 + T仓2H MA5出场")
    print(f"  α% = 策略 − 同仓位持有  |  T贡献% = T盈亏/初始资金  |  波动: 大≥20%振幅>5%, 小<20%")
    print(f"  大波动：无核心止损（顺势持有）  |  小波动：建仓初期3%止损 + 浮盈归零保护")
    print(f"{'='*100}")

    # ── 主线科技表 ───────────────────────────────────────────────────────
    for period_label, (since, until, rows) in all_main.items():
        print_table(period_label, since, until, rows, title="科技主线")

    # ── 跨期汇总矩阵（主线） ──────────────────────────────────────────────
    print(f"\n  ┌── 跨期α%矩阵 · 科技主线 {'─'*60}")
    print(f"  │  {'标的':<10} {'赛道':<11} {'全期α':>8} {'2024H2α':>9} {'2025+α':>9}  综合  波动")
    print(f"  │  {'─'*75}")
    last_sector = None
    for symbol, stype, name, sector in SYMBOLS:
        if sector != last_sector and last_sector is not None:
            print(f"  │  {'·'*75}")
        last_sector = sector
        vals = {}
        for pl, (_, _, rows) in all_main.items():
            for s, _, n, sec, r in rows:
                if s == symbol and r and "err" not in r:
                    vals[pl] = r["alpha%"]
        af  = vals.get("全期");  ah2 = vals.get("2024H2"); a25 = vals.get("2025+")
        def verdict(af, ah2, a25):
            valid = [v for v in [af, ah2, a25] if v is not None]
            if not valid: return "—"
            pos = sum(1 for v in valid if v > 0)
            if pos == len(valid):   return "✓全期赢"
            if pos == 0:            return "✗全期输"
            if ah2 is not None and a25 is not None:
                if ah2 < 0 and a25 > 0: return "△熊输/牛赢"
                if ah2 > 0 and a25 < 0: return "△熊赢/牛输"
            return f"△{pos}/{len(valid)}期赢"
        vol = "大" if stype == "large_vol" else "小"
        af_s  = f"{af:>+8.1f}"  if af  is not None else f"{'—':>8}"
        ah2_s = f"{ah2:>+9.1f}" if ah2 is not None else f"{'—':>9}"
        a25_s = f"{a25:>+9.1f}" if a25 is not None else f"{'—':>9}"
        print(f"  │  {name:<10} {sector:<11} {af_s} {ah2_s} {a25_s}  {verdict(af,ah2,a25):<12} {vol}")
    print(f"  └{'─'*78}")

    # ── Serenity 主题表 ─────────────────────────────────────────────────
    print(f"\n{'='*100}")
    print(f"  Serenity 主题选股  |  不买最显眼主线，往上游/基础设施渗透")
    print(f"{'='*100}")
    for period_label, (since, until, rows) in all_serenity.items():
        print_table(period_label, since, until, rows, title="Serenity")

    # ── Serenity 跨期矩阵 ────────────────────────────────────────────────
    print(f"\n  ┌── 跨期α%矩阵 · Serenity {'─'*62}")
    print(f"  │  {'标的':<10} {'赛道':<11} {'全期α':>8} {'2024H2α':>9} {'2025+α':>9}  综合  波动")
    print(f"  │  {'─'*75}")
    last_sector = None
    for symbol, stype, name, sector in SYMBOLS_SERENITY:
        if sector != last_sector and last_sector is not None:
            print(f"  │  {'·'*75}")
        last_sector = sector
        vals = {}
        for pl, (_, _, rows) in all_serenity.items():
            for s, _, n, sec, r in rows:
                if s == symbol and r and "err" not in r:
                    vals[pl] = r["alpha%"]
        af  = vals.get("全期");  ah2 = vals.get("2024H2"); a25 = vals.get("2025+")
        def verdict(af, ah2, a25):
            valid = [v for v in [af, ah2, a25] if v is not None]
            if not valid: return "—"
            pos = sum(1 for v in valid if v > 0)
            if pos == len(valid):   return "✓全期赢"
            if pos == 0:            return "✗全期输"
            if ah2 is not None and a25 is not None:
                if ah2 < 0 and a25 > 0: return "△熊输/牛赢"
                if ah2 > 0 and a25 < 0: return "△熊赢/牛输"
            return f"△{pos}/{len(valid)}期赢"
        vol = "大" if stype == "large_vol" else "小"
        af_s  = f"{af:>+8.1f}"  if af  is not None else f"{'—':>8}"
        ah2_s = f"{ah2:>+9.1f}" if ah2 is not None else f"{'—':>9}"
        a25_s = f"{a25:>+9.1f}" if a25 is not None else f"{'—':>9}"
        print(f"  │  {name:<10} {sector:<11} {af_s} {ah2_s} {a25_s}  {verdict(af,ah2,a25):<12} {vol}")
    print(f"  └{'─'*78}")
    print()
    print("  【α>0=策略有价值】 【T贡献>0=做T值得】 【大=顺势持有不止损，小=建仓期保护+浮盈归零】")


if __name__ == "__main__":
    main()
