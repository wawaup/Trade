"""
财报避雷对回测绩效的影响分析

运行两轮回测：
  1. 基准策略（不加财报过滤，与 factor_combo_backtest.py 完全一致）
  2. 财报避雷策略（EARNINGS_BLACKOUT_DAYS=2，调仓日有近期财报的股票跳过）

关键说明：
  - 财报数据通过 yfinance get_earnings_dates() 拉取
  - 首次运行约 5~10 分钟（拉取 150+ 只股票），结果缓存至 data/earnings_dates.json
  - 再次运行直接读缓存（秒级）
  - LULD 熔断重试 / Kill Switch 限价单属于执行层，不影响信号层回测结果

用法：
  python factor_earnings_impact.py
  python factor_earnings_impact.py --since 2022-01-01 --blackout-days 2
  python factor_earnings_impact.py --refresh-cache   # 强制重新拉取财报日期
"""
import argparse
import base64
import json
import sys
from datetime import datetime, date
from pathlib import Path

from typing import Optional

import numpy as np
import pandas as pd
import yfinance as yf
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

sys.path.insert(0, str(Path(__file__).parent))
from factor_scanner import (
    load_panel, load_benchmark, load_universe, build_liquidity_mask,
    compute_factors, compute_spy_regime,
    _DARK_BG, _DARK_AX, _GRID_CLR, _TEXT_CLR, _POS_CLR, _NEG_CLR,
    _dark_fig,
)
from factor_combo_backtest import (
    CORE_FACTORS, REGIME_WEIGHTS,
    zscore_factors, compute_combo, perf_stats,
)

DATA_DIR   = Path(__file__).parent.parent / "data"
REPORT_DIR = Path(__file__).parent.parent / "report"
REPORT_DIR.mkdir(exist_ok=True)
EARNINGS_CACHE = DATA_DIR / "earnings_dates.json"

CACHE_STALE_DAYS = 7  # 超过 7 天重新拉取


# ── 财报日期拉取与缓存 ─────────────────────────────────────────────────────────

def _cache_is_fresh() -> bool:
    if not EARNINGS_CACHE.exists():
        return False
    try:
        cache = json.loads(EARNINGS_CACHE.read_text(encoding="utf-8"))
        fetched = pd.Timestamp(cache.get("fetched_at", "2000-01-01"))
        return (pd.Timestamp.today() - fetched).days < CACHE_STALE_DAYS
    except Exception:
        return False


def fetch_earnings_dates(symbols: list[str], force_refresh: bool = False) -> dict[str, list[str]]:
    """
    返回 {symbol: ["2024-01-25", "2024-04-25", ...]} 历史财报日期字典。
    自动缓存至 data/earnings_dates.json，CACHE_STALE_DAYS 天内复用。
    """
    if not force_refresh and _cache_is_fresh():
        cache = json.loads(EARNINGS_CACHE.read_text(encoding="utf-8"))
        print(f"  使用财报日期缓存（{cache['fetched_at']}，共 {len(cache['data'])} 只股票）")
        return cache["data"]

    print(f"  拉取 {len(symbols)} 只股票历史财报日期（首次约 5~10 分钟）...")
    result: dict[str, list[str]] = {}
    for i, sym in enumerate(symbols):
        try:
            df = yf.Ticker(sym).get_earnings_dates(limit=40)
            if df is not None and not df.empty:
                dates_str = sorted(
                    df.index.normalize().strftime("%Y-%m-%d").unique().tolist()
                )
                result[sym] = dates_str
        except Exception:
            pass
        if (i + 1) % 20 == 0:
            print(f"    进度 {i+1}/{len(symbols)}...")

    cache_data = {
        "fetched_at": pd.Timestamp.today().strftime("%Y-%m-%d"),
        "data": result,
    }
    EARNINGS_CACHE.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")
    print(f"  财报日期已缓存至 {EARNINGS_CACHE.name}（{len(result)} 只有数据）")
    return result


def build_earnings_blackout_mask(
    symbols: list[str],
    close_index: pd.DatetimeIndex,
    earnings_dict: dict[str, list[str]],
    days_ahead: int = 2,
) -> pd.DataFrame:
    """
    返回 bool DataFrame（shape: dates × symbols）。
    True = 该日该股处于财报避雷窗口，不应买入/应强制出场。

    避雷逻辑（与 alpaca_trader.py 完全一致）：
      在调仓日 d，如果股票有财报日期 e 满足 d <= e <= d + BDay(days_ahead)，
      则 d 日该股处于避雷窗口。
      等价于：e - BDay(days_ahead) <= d <= e
    """
    mask = pd.DataFrame(False, index=close_index, columns=symbols)
    for sym in symbols:
        earn_dates = earnings_dict.get(sym, [])
        for earn_str in earn_dates:
            try:
                earn_ts = pd.Timestamp(earn_str).normalize()
                window_start = earn_ts - pd.offsets.BDay(days_ahead)
                window_end   = earn_ts
                idx = close_index[
                    (close_index >= window_start) & (close_index <= window_end)
                ]
                if len(idx):
                    mask.loc[idx, sym] = True
            except Exception:
                pass
    return mask


# ── 带财报过滤的回测引擎 ───────────────────────────────────────────────────────

def run_backtest(
    combo: pd.DataFrame,
    vol_shock: pd.DataFrame,
    close: pd.DataFrame,
    liquid_mask: pd.DataFrame,
    spy_close: pd.Series,
    qqq_close: pd.Series,
    min_score: float = 1.0,
    vol_min: float = 1.2,
    top_n: int = 5,
    rebalance: int = 5,
    earnings_blackout: Optional[pd.DataFrame] = None,
) -> tuple:
    """
    单次回测。earnings_blackout=None 时退化为标准基准回测。
    返回 (equity, spy_equity, qqq_equity, port_ret, trade_log, blackout_events)。
    """
    dates   = close.index
    fwd_ret = close.pct_change()
    spy_ret = spy_close.pct_change().reindex(dates).fillna(0)
    qqq_ret = qqq_close.pct_change().reindex(dates).fillna(0)

    port_ret        = pd.Series(0.0, index=dates)
    holdings: dict  = {}
    trade_log       = []
    blackout_events = []  # [(date, blacked_out_syms)]

    for i, date in enumerate(dates):
        if i == 0:
            continue
        if i % rebalance == 0:
            prev   = dates[i - 1]
            scores = combo.loc[prev]     if prev in combo.index     else pd.Series(dtype=float)
            vs     = vol_shock.loc[prev] if prev in vol_shock.index else pd.Series(dtype=float)
            lm     = (liquid_mask.loc[prev].fillna(False)
                      if prev in liquid_mask.index
                      else pd.Series(True, index=scores.index))

            valid = scores[
                lm.reindex(scores.index, fill_value=False)
                & (scores > min_score)
                & (vs.reindex(scores.index, fill_value=0) > vol_min)
            ].dropna()

            # ── 财报避雷过滤 ──────────────────────────────────────────────────
            blacked: list[str] = []
            if earnings_blackout is not None and prev in earnings_blackout.index:
                bl_row = earnings_blackout.loc[prev].reindex(valid.index, fill_value=False)
                blacked = bl_row[bl_row].index.tolist()
                valid = valid[~bl_row]
            if blacked:
                blackout_events.append({"date": date, "syms": blacked})

            top      = valid.nlargest(top_n)
            holdings = {s: 1 / len(top) for s in top.index} if len(top) > 0 else {}
            trade_log.append({
                "date": date, "n_valid": len(valid),
                "stocks": list(top.index), "held": len(holdings),
                "blacked": blacked,
            })

        if holdings:
            day = sum(
                w * fwd_ret.at[date, sym]
                for sym, w in holdings.items()
                if sym in fwd_ret.columns and not pd.isna(fwd_ret.at[date, sym])
            )
            port_ret.at[date] = day

    equity     = (1 + port_ret).cumprod()
    spy_equity = (1 + spy_ret).cumprod()
    qqq_equity = (1 + qqq_ret).cumprod()
    return equity, spy_equity, qqq_equity, port_ret, trade_log, blackout_events


# ── 对比图 ────────────────────────────────────────────────────────────────────

def plot_comparison(
    equity_base, equity_filter,
    spy_eq, qqq_eq,
    port_ret_base, port_ret_filter,
    out_path: Path,
):
    fig, axes = _dark_fig(2, (14, 9))
    ax_eq, ax_dd = axes

    ax_eq.plot(equity_base.index,   equity_base.values,   color="#ffd60a",  lw=2.0, label="基准策略（无财报过滤）")
    ax_eq.plot(equity_filter.index, equity_filter.values, color="#39d353",  lw=2.0, label="财报避雷策略")
    ax_eq.plot(spy_eq.index,        spy_eq.values,        color="#58a6ff",  lw=1.1, label="SPY",  ls="--")
    ax_eq.plot(qqq_eq.index,        qqq_eq.values,        color="#d2a8ff",  lw=1.1, label="QQQ",  ls="--")
    ax_eq.axhline(1, color=_GRID_CLR, lw=0.6, ls=":")
    ax_eq.set_ylabel("净值（起始=1）", color=_TEXT_CLR)
    ax_eq.set_title("基准 vs 财报避雷策略净值对比", color=_TEXT_CLR, fontsize=11)
    ax_eq.legend(facecolor=_DARK_AX, labelcolor=_TEXT_CLR, edgecolor=_GRID_CLR, fontsize=9)
    ax_eq.grid(alpha=0.12, color=_GRID_CLR)
    ax_eq.tick_params(colors=_TEXT_CLR)

    dd_base   = equity_base   / equity_base.cummax()   - 1
    dd_filter = equity_filter / equity_filter.cummax() - 1
    ax_dd.fill_between(dd_base.index,   dd_base.values,   0, color="#f85149", alpha=0.35, label="基准回撤")
    ax_dd.fill_between(dd_filter.index, dd_filter.values, 0, color="#39d353", alpha=0.30, label="避雷回撤")
    ax_dd.axhline(0, color=_GRID_CLR, lw=0.5)
    ax_dd.set_ylabel("回撤", color=_TEXT_CLR)
    ax_dd.set_title("回撤对比（绿=财报避雷，红=基准）", color=_TEXT_CLR, fontsize=10)
    ax_dd.legend(facecolor=_DARK_AX, labelcolor=_TEXT_CLR, edgecolor=_GRID_CLR, fontsize=9)
    ax_dd.grid(alpha=0.12, color=_GRID_CLR)
    ax_dd.tick_params(colors=_TEXT_CLR)

    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right", fontsize=7, color=_TEXT_CLR)

    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=_DARK_BG)
    plt.close()
    print(f"对比图 → {out_path.name}")


# ── HTML 报告 ─────────────────────────────────────────────────────────────────

def _b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode()


def _delta_color(v_filter: str, v_base: str, higher_is_better: bool = True) -> str:
    """解析两个百分比字符串，返回 CSS 颜色（绿/红/灰）。"""
    try:
        f = float(v_filter.replace("%", "").replace("+", ""))
        b = float(v_base.replace("%", "").replace("+", ""))
        improved = f > b if higher_is_better else f < b
        return "#39d353" if improved else ("#f85149" if f != b else "#8b949e")
    except Exception:
        return "#8b949e"


def generate_comparison_report(
    stats_base: dict,
    stats_filter: dict,
    stats_spy: dict,
    stats_qqq: dict,
    blackout_events: list,
    trade_log_base: list,
    trade_log_filter: list,
    equity_path: Path,
    out_path: Path,
    blackout_days: int,
    since: str,
):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    equity_b64 = _b64(equity_path)

    # 绩效对比表行
    metrics = ["总收益", "年化收益", "年化波动", "Sharpe", "最大回撤", "Calmar"]
    higher_better = {"总收益": True, "年化收益": True, "年化波动": False,
                     "Sharpe": True, "最大回撤": False, "Calmar": True}

    comparison_rows = ""
    for m in metrics:
        vb  = stats_base.get(m, "—")
        vf  = stats_filter.get(m, "—")
        col = _delta_color(vf, vb, higher_better.get(m, True))
        delta_sign = "▲" if col == "#39d353" else ("▼" if col == "#f85149" else "—")
        comparison_rows += (
            f"<tr><td>{m}</td>"
            f'<td>{vb}</td>'
            f'<td style="color:{col};font-weight:700">{vf} {delta_sign}</td>'
            f'<td>{stats_spy.get(m,"—")}</td>'
            f'<td>{stats_qqq.get(m,"—")}</td>'
            "</tr>"
        )

    # 财报避雷事件（最近20次）
    event_rows = ""
    if blackout_events:
        for ev in blackout_events[-20:]:
            d   = ev["date"].date() if hasattr(ev["date"], "date") else ev["date"]
            syms = ", ".join(ev["syms"])
            event_rows += f"<tr><td>{d}</td><td>{syms}</td></tr>"
    else:
        event_rows = "<tr><td colspan='2' style='color:#8b949e'>无财报避雷事件</td></tr>"

    # 统计数字
    total_rebalances = len(trade_log_base)
    total_blackouts  = len(blackout_events)
    total_syms_avoided = sum(len(e["syms"]) for e in blackout_events)
    blackout_rate = f"{total_blackouts / total_rebalances:.1%}" if total_rebalances else "—"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>财报避雷影响分析报告</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            margin: 0; background: #0d1117; color: #e6edf3; }}
    .main {{ max-width: 1100px; margin: 0 auto; padding: 32px 36px 80px; }}
    h1 {{ color: #fff; border-bottom: 3px solid #39d353; padding-bottom: 10px;
          font-size: 1.5em; margin-top: 0; }}
    h2 {{ color: #ffd60a; font-size: 1.05em; margin-top: 44px;
          border-left: 4px solid #ffd60a; padding-left: 12px; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 12px; }}
    .meta-item {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                  padding: 12px 18px; text-align: center; min-width: 120px; }}
    .meta-item .val {{ font-size: 1.3em; font-weight: 700; color: #ffd60a; }}
    .meta-item .lbl {{ font-size: 0.76em; color: #8b949e; margin-top: 3px; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0;
             background: #161b22; border-radius: 10px; overflow: hidden;
             font-size: 0.88em; border: 1px solid #30363d; }}
    th {{ background: #1f2937; color: #ffd60a; padding: 9px 14px; text-align: center; }}
    td {{ padding: 8px 14px; text-align: center; border-bottom: 1px solid #21262d; }}
    tr:last-child td {{ border-bottom: none; }}
    img {{ max-width: 100%; border-radius: 10px;
           box-shadow: 0 2px 12px rgba(0,0,0,.4); margin: 8px 0; }}
    .note {{ background: #161b22; border-left: 4px solid #39d353;
             padding: 12px 16px; border-radius: 0 8px 8px 0;
             margin: 12px 0; font-size: 0.88em; line-height: 1.7;
             border: 1px solid #30363d; border-left: 4px solid #39d353; }}
    .note b {{ color: #39d353; }}
    .verdict-good {{ color: #39d353; font-weight: 700; }}
    .verdict-bad  {{ color: #f85149; font-weight: 700; }}
    .verdict-neu  {{ color: #8b949e; font-weight: 700; }}
  </style>
</head>
<body>
<div class="main">
  <h1>🛡️ 财报避雷对回测绩效的影响分析</h1>

  <div class="meta">
    <div class="meta-item"><div class="val">{blackout_days}日</div><div class="lbl">避雷窗口</div></div>
    <div class="meta-item"><div class="val">{since}</div><div class="lbl">回测起点</div></div>
    <div class="meta-item"><div class="val">{total_rebalances}</div><div class="lbl">总调仓次数</div></div>
    <div class="meta-item"><div class="val">{total_blackouts}</div><div class="lbl">触发避雷次数</div></div>
    <div class="meta-item"><div class="val">{total_syms_avoided}</div><div class="lbl">被回避股票次数</div></div>
    <div class="meta-item"><div class="val">{blackout_rate}</div><div class="lbl">避雷触发率</div></div>
    <div class="meta-item"><div class="val">{now}</div><div class="lbl">生成时间</div></div>
  </div>

  <div class="note">
    <b>分析说明</b><br>
    财报避雷仅影响<b>仓位构建层</b>（在调仓日跳过近期有财报的股票），不影响信号计算逻辑。<br>
    LULD 熔断重试 / Kill Switch 盘后限价单属于<b>执行层</b>改进，不体现在纯净值回测中。<br>
    财报数据来源：yfinance get_earnings_dates()，对比 2022 至今历史财报日期。
  </div>

  <h2>1. 净值对比图</h2>
  <img src="data:image/png;base64,{equity_b64}" alt="净值对比">

  <h2>2. 绩效对比（黄=基准 / 绿=财报避雷）</h2>
  <table>
    <thead>
      <tr>
        <th>指标</th>
        <th style="color:#ffd60a">基准策略</th>
        <th style="color:#39d353">+ 财报避雷</th>
        <th>SPY</th>
        <th>QQQ</th>
      </tr>
    </thead>
    <tbody>{comparison_rows}</tbody>
  </table>

  <h2>3. 财报避雷触发记录（最近 20 次）</h2>
  <p style="font-size:0.82em;color:#8b949e;margin:4px 0 8px">
    调仓日发现持仓或目标股票有近期财报，该股被剔出/强制出场的记录
  </p>
  <table>
    <thead><tr><th>调仓日</th><th>被回避的股票</th></tr></thead>
    <tbody>{event_rows}</tbody>
  </table>
</div>
</body>
</html>"""
    out_path.write_text(html, encoding="utf-8")
    print(f"HTML 报告 → {out_path.name}")


# ── 主流程 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="财报避雷对回测绩效的影响分析")
    parser.add_argument("--since",         default="2022-01-01")
    parser.add_argument("--tf",            default="1d")
    parser.add_argument("--top-n",         type=int,   default=5)
    parser.add_argument("--min-score",     type=float, default=1.0)
    parser.add_argument("--vol-min",       type=float, default=1.2)
    parser.add_argument("--rebalance",     type=int,   default=5)
    parser.add_argument("--blackout-days", type=int,   default=2,
                        help="财报避雷窗口（交易日），默认 2")
    parser.add_argument("--refresh-cache", action="store_true",
                        help="强制重新拉取财报日期（忽略缓存）")
    args = parser.parse_args()

    # ── 数据加载 ──────────────────────────────────────────────────────────────
    symbols, _ = load_universe()
    print(f"加载 {len(symbols)} 只股票...")
    close, high, low, vol = load_panel(symbols, args.tf, since=args.since)
    qqq_close = load_benchmark("QQQ", args.tf, since=args.since).reindex(close.index)
    spy_close = load_benchmark("SPY", args.tf, since=args.since).reindex(close.index)
    print(f"  有效标的: {close.shape[1]} 只  {close.index[0].date()} ~ {close.index[-1].date()}")

    liquid    = build_liquidity_mask(close, vol)
    vol_ma20  = vol.rolling(20).mean().replace(0, np.nan)
    vol_shock = (vol / vol_ma20).reindex(close.index)

    # ── 因子计算 ──────────────────────────────────────────────────────────────
    print("计算因子...")
    all_factors  = compute_factors(close, high, low, vol, qqq_close)
    core_panels  = {f: all_factors[f] for f in CORE_FACTORS if f in all_factors}
    z_panels     = zscore_factors(core_panels, liquid)
    regime, _    = compute_spy_regime(qqq_close, ma_window=50, buffer=0.0)
    combo        = compute_combo(z_panels, regime)

    # ── 财报日期 ──────────────────────────────────────────────────────────────
    stock_syms = [s for s in close.columns if s not in ("QQQ", "SPY")]
    print(f"\n拉取财报日期（blackout={args.blackout_days}日）...")
    earnings_dict = fetch_earnings_dates(stock_syms, force_refresh=args.refresh_cache)
    earnings_mask = build_earnings_blackout_mask(
        stock_syms, close.index, earnings_dict, days_ahead=args.blackout_days
    )
    n_blackout_total = int(earnings_mask.sum().sum())
    print(f"  财报避雷掩码：{n_blackout_total} 个（股票×日期）组合被标记")

    # ── 基准回测 ──────────────────────────────────────────────────────────────
    print("\n[1/2] 基准回测（无财报过滤）...")
    eq_base, spy_eq, qqq_eq, ret_base, log_base, _ = run_backtest(
        combo, vol_shock, close, liquid, spy_close, qqq_close,
        min_score=args.min_score, vol_min=args.vol_min,
        top_n=args.top_n, rebalance=args.rebalance,
        earnings_blackout=None,
    )
    print(f"  调仓次数: {len(log_base)}")

    # ── 财报避雷回测 ──────────────────────────────────────────────────────────
    print(f"[2/2] 财报避雷回测（blackout={args.blackout_days}日）...")
    eq_filter, _, _, ret_filter, log_filter, blackout_events = run_backtest(
        combo, vol_shock, close, liquid, spy_close, qqq_close,
        min_score=args.min_score, vol_min=args.vol_min,
        top_n=args.top_n, rebalance=args.rebalance,
        earnings_blackout=earnings_mask,
    )
    print(f"  调仓次数: {len(log_filter)}，避雷触发: {len(blackout_events)} 次")

    # ── 绩效对比 ──────────────────────────────────────────────────────────────
    spy_ret_s = spy_close.pct_change().reindex(close.index).fillna(0)
    qqq_ret_s = qqq_close.pct_change().reindex(close.index).fillna(0)

    s_base   = perf_stats(eq_base,   ret_base,   "基准策略")
    s_filter = perf_stats(eq_filter, ret_filter, "财报避雷")
    s_spy    = perf_stats(spy_eq,    spy_ret_s,  "SPY")
    s_qqq    = perf_stats(qqq_eq,    qqq_ret_s,  "QQQ")

    print("\n" + "=" * 70)
    print(f"{'标的':<12}{'总收益':>10}{'年化收益':>10}{'Sharpe':>8}{'最大回撤':>10}{'Calmar':>8}")
    print("-" * 70)
    for row in [s_base, s_filter, s_spy, s_qqq]:
        print(
            f"{row['标的']:<12}{row['总收益']:>10}{row['年化收益']:>10}"
            f"{row['Sharpe']:>8}{row['最大回撤']:>10}{row['Calmar']:>8}"
        )
    print("=" * 70)

    # ── 生成报告 ──────────────────────────────────────────────────────────────
    equity_path = REPORT_DIR / "earnings_impact_equity.png"
    html_path   = REPORT_DIR / "earnings_impact.html"

    print("\n生成图表和报告...")
    plot_comparison(eq_base, eq_filter, spy_eq, qqq_eq, ret_base, ret_filter, equity_path)
    generate_comparison_report(
        s_base, s_filter, s_spy, s_qqq,
        blackout_events, log_base, log_filter,
        equity_path, html_path,
        blackout_days=args.blackout_days,
        since=args.since,
    )
    print(f"\n全部输出已保存至 {REPORT_DIR}/")
    print(f"  用浏览器打开: open {html_path}")


if __name__ == "__main__":
    main()
