"""
量化因子分析可视化。

用法:
    python 01_factor_analysis.py                           # NVDA 1d 最近2年
    python 01_factor_analysis.py --symbol TSLA
    python 01_factor_analysis.py --symbol SPY --since 2023-01-01 --until 2024-12-31
    python 01_factor_analysis.py --symbol NVDA --no-show   # 只保存不弹窗
"""
import argparse
import sys
from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

# ── 中文字体配置 ──────────────────────────────────────────
def _setup_chinese_font():
    candidates = ["Kaiti SC", "PingFang SC", "Heiti SC", "Arial Unicode MS",
                  "Noto Sans CJK SC", "WenQuanYi Micro Hei"]
    available = {f.name for f in fm.fontManager.ttflist}
    for font in candidates:
        if font in available:
            matplotlib.rcParams["font.family"] = font
            matplotlib.rcParams["axes.unicode_minus"] = False
            return font
    matplotlib.rcParams["axes.unicode_minus"] = False
    return None

_FONT = _setup_chinese_font()
import matplotlib.dates as mdates
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from factor_store import load_factors

DATA_DIR = Path(__file__).parent.parent / "data"


def simulate_equity(df: pd.DataFrame, capital: float = 10_000.0) -> pd.Series:
    """极简信号回测：buy_signal 买入，sell_signal 卖出，不计滑点/手续费。"""
    equity = []
    position = 0.0
    cash = capital

    for i in range(len(df)):
        row = df.iloc[i]
        close = row["close"]
        if position == 0 and row["buy_signal"] == 1:
            position = cash / close
            cash = 0.0
        elif position > 0 and row["sell_signal"] == 1:
            cash = position * close
            position = 0.0
        equity.append(cash + position * close)

    return pd.Series(equity, index=df.index)


def plot_analysis(df: pd.DataFrame, symbol: str, since: str, until: str, show: bool = True):
    df = df.loc[since:until].copy()
    if len(df) == 0:
        print(f"无数据在 {since}~{until} 区间")
        return

    buy_bars  = df[df["buy_signal"]  == 1]
    sell_bars = df[df["sell_signal"] == 1]
    equity    = simulate_equity(df)

    total_ret = (equity.iloc[-1] / 10_000 - 1) * 100
    max_dd    = ((equity / equity.cummax()) - 1).min() * 100

    fig, axes = plt.subplots(5, 1, figsize=(16, 22), sharex=True,
                              gridspec_kw={"height_ratios": [3, 1.5, 1.5, 1.5, 1]})
    fig.suptitle(f"{symbol} 量化因子分析  {since} ~ {until}", fontsize=13, fontweight="bold")

    # ── 图1：价格 + 均线 + 买卖点 ────────────────────────
    ax = axes[0]
    ax.plot(df.index, df["close"],        color="#1a1a2e", lw=0.9, label="Close")
    ax.plot(df.index, df["MA5"],          color="#4361ee", lw=0.8, alpha=0.8, label="MA5")
    ax.plot(df.index, df["MA10"],         color="#f77f00", lw=0.8, alpha=0.8, label="MA10")
    ax.plot(df.index, df["MA20"],         color="#9b2226", lw=0.8, alpha=0.8, label="MA20")
    ax.plot(df.index, df["VWAP_session"], color="#2a9d8f", lw=0.7, alpha=0.6,
            linestyle="--", label="VWAP")
    if len(buy_bars):
        ax.scatter(buy_bars.index, buy_bars["close"], marker="^",
                   color="#2dc653", s=80, zorder=6, label=f"买入({len(buy_bars)})")
    if len(sell_bars):
        ax.scatter(sell_bars.index, sell_bars["close"], marker="v",
                   color="#e63946", s=80, zorder=6, label=f"卖出({len(sell_bars)})")
    ax.legend(loc="upper left", fontsize=7.5, ncol=4, framealpha=0.7)
    ax.set_ylabel("Price (USD)")
    ax.set_title("价格 + 均线 + 买卖点", fontsize=10)
    ax.grid(alpha=0.25)

    # ── 图2：KDJ ────────────────────────────────────────
    ax = axes[1]
    ax.plot(df.index, df["KDJ_K"], color="#4361ee", lw=0.8, label="K")
    ax.plot(df.index, df["KDJ_D"], color="#f77f00", lw=0.8, label="D")
    ax.plot(df.index, df["KDJ_J"], color="#9b2226", lw=0.8, label="J")
    ax.axhline(80, color="#e63946", lw=0.6, ls="--", alpha=0.7, label="超买80")
    ax.axhline(20, color="#2dc653", lw=0.6, ls="--", alpha=0.7, label="超卖20")
    ax.axhline(50, color="#aaa",    lw=0.4, ls=":")
    ax.set_ylim(-20, 120)
    ax.legend(loc="upper left", fontsize=7.5, ncol=3, framealpha=0.7)
    ax.set_ylabel("KDJ")
    ax.set_title("KDJ 指标 (9,3,3)", fontsize=10)
    ax.grid(alpha=0.25)

    # ── 图3：MACD ───────────────────────────────────────
    ax = axes[2]
    colors = ["#2dc653" if v >= 0 else "#e63946" for v in df["MACD_hist"]]
    ax.bar(df.index, df["MACD_hist"], color=colors, alpha=0.65, label="Hist", width=1.5)
    ax.plot(df.index, df["MACD_line"],   color="#4361ee", lw=0.8, label="MACD")
    ax.plot(df.index, df["MACD_signal"], color="#f77f00", lw=0.8, label="Signal")
    ax.axhline(0, color="#aaa", lw=0.5)
    ax.legend(loc="upper left", fontsize=7.5, ncol=3, framealpha=0.7)
    ax.set_ylabel("MACD")
    ax.set_title("MACD (12,26,9)", fontsize=10)
    ax.grid(alpha=0.25)

    # ── 图4：资金曲线 ───────────────────────────────────
    ax = axes[3]
    ax.plot(df.index, equity, color="#4361ee", lw=1.1, label="资金曲线")
    ax.fill_between(df.index, equity, 10_000, where=(equity >= 10_000),
                    alpha=0.12, color="#2dc653")
    ax.fill_between(df.index, equity, 10_000, where=(equity < 10_000),
                    alpha=0.12, color="#e63946")
    ax.axhline(10_000, color="#aaa", lw=0.6, ls="--", label="初始资金 $10,000")
    ax.legend(loc="upper left", fontsize=7.5, framealpha=0.7)
    ax.set_ylabel("Equity ($)")
    ax.set_title(f"资金曲线  总收益: {total_ret:+.1f}%  最大回撤: {max_dd:.1f}%", fontsize=10)
    ax.grid(alpha=0.25)

    # ── 图5：成交量倍数 ─────────────────────────────────
    ax = axes[4]
    vol_colors = ["#e63946" if v > 1.5 else "#90caf9" for v in df["vol_ratio"].fillna(0)]
    ax.bar(df.index, df["vol_ratio"].fillna(0), color=vol_colors, alpha=0.75, width=1.5)
    ax.axhline(1.5, color="#f77f00", lw=0.8, ls="--", label="1.5× 阈值")
    ax.legend(loc="upper right", fontsize=7.5, framealpha=0.7)
    ax.set_ylabel("Vol Ratio")
    ax.set_title("相对成交量 (红=放量)", fontsize=10)
    ax.grid(alpha=0.25)

    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right", fontsize=7)

    plt.tight_layout(rect=[0, 0, 1, 0.97])

    out = DATA_DIR / f"{symbol.lower()}_factor_chart.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"图表保存至: {out}")
    if show:
        plt.show()
    plt.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol",   default="NVDA")
    parser.add_argument("--tf",       default="1d")
    parser.add_argument("--since",    default="2024-01-01")
    parser.add_argument("--until",    default="2026-06-20")
    parser.add_argument("--no-show",  action="store_true", help="只保存图片不弹窗")
    args = parser.parse_args()

    df = load_factors(args.symbol, args.tf)
    print(f"载入 {args.symbol}: {len(df)} 行  {df.index[0].date()} ~ {df.index[-1].date()}")
    plot_analysis(df, args.symbol, args.since, args.until, show=not args.no_show)


if __name__ == "__main__":
    main()
