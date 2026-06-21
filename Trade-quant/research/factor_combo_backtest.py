"""
多因子合成回测 — 状态机动态加权策略

因子：RS_Beta + MFI_14 + LR_Slope + HV_ratio（4 个核心因子）
加权：SPY MA200 三态开关（牛市 / 熊市 / 震荡）
过滤：Combo_Score > 1.0 AND Vol_Shock > 1.2
仓位：前 N 名等权，每 5 个交易日调仓

输出（data/ 目录）：
  combo_corr.png       因子截面相关性热力图
  combo_equity.png     净值曲线 vs SPY / QQQ
  combo_report.html    完整 HTML 报告

用法：
  python factor_combo_backtest.py
  python factor_combo_backtest.py --since 2022-01-01 --top-n 5
"""
import argparse
import base64
import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

sys.path.insert(0, str(Path(__file__).parent))
from factor_scanner import (
    load_panel, load_benchmark, load_universe, build_liquidity_mask,
    compute_factors, compute_spy_regime, REGIMES,
    _DARK_BG, _DARK_AX, _GRID_CLR, _TEXT_CLR, _POS_CLR, _NEG_CLR, _CUM_CLR,
    _dark_fig,
)

DATA_DIR = Path(__file__).parent.parent / "data"
MIN_STOCKS = 10  # 调仓日至少有这么多只候选股才入场

# ── 状态机权重矩阵 ─────────────────────────────────────────────────────────────
CORE_FACTORS = ["RS_Beta", "MFI_14", "LR_Slope", "HV_ratio"]

REGIME_WEIGHTS = {
    1: {   # 牛市：SPY > MA200 × 1.01
        "RS_Beta":  0.40,
        "MFI_14":   0.20,
        "LR_Slope": 0.30,
        "HV_ratio": 0.10,
    },
    -1: {  # 熊市：SPY < MA200 × 0.99
        "RS_Beta":  0.00,
        "MFI_14":  -0.30,
        "LR_Slope": 0.00,
        "HV_ratio": 0.70,
    },
    0: {   # 震荡：±1% 缓冲区
        "RS_Beta":  0.10,
        "MFI_14":  -0.40,
        "LR_Slope": 0.10,
        "HV_ratio": 0.40,
    },
}


# ── 截面 Z-Score 标准化 ────────────────────────────────────────────────────────

def zscore_factors(factor_panels, liquid_mask):
    """对每个因子做逐日截面 Z-Score（减均值除标准差），统一量纲。"""
    z = {}
    for fname, panel in factor_panels.items():
        masked   = panel.where(liquid_mask.reindex_like(panel).fillna(False))
        row_mean = masked.mean(axis=1)
        row_std  = masked.std(axis=1).replace(0, np.nan)
        z[fname] = masked.sub(row_mean, axis=0).div(row_std, axis=0)
    return z


# ── 因子截面相关性 ─────────────────────────────────────────────────────────────

def compute_factor_corr(factor_panels, liquid_mask, min_stocks=20):
    """
    逐日计算各因子对之间的截面 Spearman 相关系数，取时间序列均值。
    Spearman = Pearson(rank_A, rank_B)，可以全向量化。
    """
    names = list(factor_panels.keys())
    n = len(names)

    # 逐日截面 rank
    rank_panels = {}
    for fname, panel in factor_panels.items():
        masked = panel.where(liquid_mask.reindex_like(panel).fillna(False))
        rank_panels[fname] = masked.rank(axis=1)  # 每行对 stocks 排名

    corr_matrix = np.full((n, n), np.nan)
    for i, fa in enumerate(names):
        corr_matrix[i, i] = 1.0
        for j, fb in enumerate(names):
            if j <= i:
                continue
            ra = rank_panels[fa]
            rb = rank_panels[fb]
            daily = []
            for date in ra.index:
                a = ra.loc[date].dropna()
                b = rb.loc[date].dropna()
                common = a.index.intersection(b.index)
                if len(common) < min_stocks:
                    continue
                c = np.corrcoef(a[common].values, b[common].values)[0, 1]
                if not np.isnan(c):
                    daily.append(c)
            val = float(np.mean(daily)) if daily else np.nan
            corr_matrix[i, j] = corr_matrix[j, i] = val

    return pd.DataFrame(corr_matrix, index=names, columns=names)


# ── Combo Score ───────────────────────────────────────────────────────────────

def compute_combo(z_panels, spy_regime):
    """
    按每日市场状态选取对应权重矩阵，加权求和各因子 Z-Score。
    缺失值对应因子贡献为 0（不影响其他因子的权重）。
    """
    ref    = z_panels[CORE_FACTORS[0]]
    combo  = pd.DataFrame(0.0, index=ref.index, columns=ref.columns)

    for regime_val, weights in REGIME_WEIGHTS.items():
        dates = spy_regime[spy_regime == regime_val].index.intersection(combo.index)
        if len(dates) == 0:
            continue
        for fname, w in weights.items():
            if w == 0 or fname not in z_panels:
                continue
            z = z_panels[fname].reindex(dates).fillna(0)
            combo.loc[dates] += z * w

    return combo


# ── 回测引擎 ──────────────────────────────────────────────────────────────────

def run_backtest(combo, vol_shock, close, liquid_mask,
                 spy_close, qqq_close,
                 min_score=1.0, vol_min=1.2, top_n=5, rebalance=5):
    """
    每 rebalance 个交易日调仓：
      - 信号基于前一日 Combo_Score 和 Vol_Shock
      - Combo_Score > min_score  AND  Vol_Shock > vol_min
      - 取分数最高的 top_n 只，等权持仓
    """
    dates    = close.index
    fwd_ret  = close.pct_change()
    spy_ret  = spy_close.pct_change().reindex(dates).fillna(0)
    qqq_ret  = qqq_close.pct_change().reindex(dates).fillna(0)

    port_ret   = pd.Series(0.0, index=dates)
    holdings   = {}   # {sym: weight}
    trade_log  = []   # [{date, stocks, n_valid}]

    for i, date in enumerate(dates):
        if i == 0:
            continue

        # ── 调仓 ──────────────────────────────────────────────────────────────
        if i % rebalance == 0:
            prev = dates[i - 1]
            scores = combo.loc[prev]    if prev in combo.index    else pd.Series(dtype=float)
            vs     = vol_shock.loc[prev] if prev in vol_shock.index else pd.Series(dtype=float)
            lm     = (liquid_mask.loc[prev].fillna(False)
                      if prev in liquid_mask.index
                      else pd.Series(True, index=scores.index))

            valid = scores[
                lm.reindex(scores.index, fill_value=False) &
                (scores > min_score) &
                (vs.reindex(scores.index, fill_value=0) > vol_min)
            ].dropna()

            top = valid.nlargest(top_n)
            holdings = {s: 1 / len(top) for s in top.index} if len(top) > 0 else {}
            trade_log.append({
                "date": date, "n_valid": len(valid),
                "stocks": list(top.index), "held": len(holdings),
            })

        # ── 当日收益 ──────────────────────────────────────────────────────────
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

    return equity, spy_equity, qqq_equity, port_ret, trade_log


# ── 绩效统计 ──────────────────────────────────────────────────────────────────

def perf_stats(equity, ret_series, label="策略"):
    n_days   = len(ret_series)
    n_years  = n_days / 252
    total_r  = equity.iloc[-1] - 1
    cagr     = (equity.iloc[-1]) ** (1 / n_years) - 1
    vol      = ret_series.std() * 252 ** 0.5
    sharpe   = (ret_series.mean() * 252) / (ret_series.std() * 252 ** 0.5) if ret_series.std() > 0 else 0
    dd       = (equity / equity.cummax() - 1)
    max_dd   = dd.min()
    calmar   = cagr / abs(max_dd) if max_dd != 0 else np.nan
    return {
        "标的":     label,
        "总收益":   f"{total_r:+.1%}",
        "年化收益": f"{cagr:+.1%}",
        "年化波动": f"{vol:.1%}",
        "Sharpe":   f"{sharpe:.2f}",
        "最大回撤": f"{max_dd:.1%}",
        "Calmar":   f"{calmar:.2f}" if not np.isnan(calmar) else "N/A",
    }


# ── 绘图 ──────────────────────────────────────────────────────────────────────

def plot_corr(corr_df, out_path):
    from matplotlib.colors import LinearSegmentedColormap
    n = len(corr_df)
    fig, (ax,) = _dark_fig(1, (7, 5.5))

    cmap = LinearSegmentedColormap.from_list("corr", ["#f85149", _DARK_AX, "#39d353"])
    im   = ax.imshow(corr_df.values, cmap=cmap, vmin=-1, vmax=1, aspect="auto")

    ax.set_xticks(range(n)); ax.set_xticklabels(corr_df.columns, color=_TEXT_CLR, fontsize=10)
    ax.set_yticks(range(n)); ax.set_yticklabels(corr_df.index,   color=_TEXT_CLR, fontsize=10)

    for i in range(n):
        for j in range(n):
            v = corr_df.iloc[i, j]
            ax.text(j, i, f"{v:.2f}", ha="center", va="center",
                    fontsize=11, color=_TEXT_CLR, fontweight="bold")

    cb = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cb.set_label("平均截面 Spearman 相关系数", color=_TEXT_CLR)
    cb.ax.yaxis.set_tick_params(color=_TEXT_CLR)
    plt.setp(cb.ax.yaxis.get_ticklabels(), color=_TEXT_CLR)

    ax.set_title("因子截面相关性（值 < 0.3 = 互补，> 0.6 = 同质）",
                 color=_TEXT_CLR, fontsize=11, pad=10)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=_DARK_BG)
    plt.close()


def plot_equity(equity, spy_eq, qqq_eq, spy_regime, spy_ma, port_ret, out_path):
    fig, axes = _dark_fig(2, (14, 9))
    ax_eq, ax_dd = axes

    regime_colors = {
        "熊市(2022)":     "#8b0000",
        "反弹(22Q4)":     "#7a4f00",
        "AI牛市(23-24)":  "#004d00",
        "关税震荡(25H1)": "#7a4f00",
        "复苏(25H2+)":    "#003d66",
    }
    x_min, x_max = equity.index.min(), equity.index.max()
    for ax in axes:
        for rname, (rs, re) in REGIMES.items():
            rs_ts = max(pd.Timestamp(rs), x_min)
            re_ts = min(pd.Timestamp(re), x_max)
            if rs_ts >= re_ts:
                continue
            ax.axvspan(rs_ts, re_ts, alpha=0.2,
                       color=regime_colors.get(rname, "#333"), label=None)

    # ── 净值曲线 ──
    ax_eq.plot(equity.index,  equity.values,  color="#ffd60a", lw=2.0, label="策略 Combo")
    ax_eq.plot(spy_eq.index,  spy_eq.values,  color="#58a6ff", lw=1.3, label="SPY")
    ax_eq.plot(qqq_eq.index,  qqq_eq.values,  color="#39d353", lw=1.3, label="QQQ")
    ax_eq.axhline(1, color=_GRID_CLR, lw=0.6, ls="--")
    ax_eq.set_ylabel("净值（起始=1）", color=_TEXT_CLR)
    ax_eq.set_title("多因子状态机策略净值曲线", color=_TEXT_CLR, fontsize=11)
    ax_eq.legend(facecolor=_DARK_AX, labelcolor=_TEXT_CLR,
                 edgecolor=_GRID_CLR, fontsize=9)
    ax_eq.grid(alpha=0.12, color=_GRID_CLR)
    ax_eq.tick_params(colors=_TEXT_CLR)

    # ── 回撤曲线 ──
    dd = equity / equity.cummax() - 1
    spy_dd = spy_eq / spy_eq.cummax() - 1
    ax_dd.fill_between(dd.index, dd.values, 0, color="#f85149", alpha=0.4, label="策略回撤")
    ax_dd.plot(spy_dd.index, spy_dd.values, color="#58a6ff", lw=0.9, ls="--", label="SPY 回撤")
    ax_dd.axhline(0, color=_GRID_CLR, lw=0.5)
    ax_dd.set_ylabel("回撤", color=_TEXT_CLR)
    ax_dd.set_title("最大回撤对比", color=_TEXT_CLR, fontsize=10)
    ax_dd.legend(facecolor=_DARK_AX, labelcolor=_TEXT_CLR,
                 edgecolor=_GRID_CLR, fontsize=9)
    ax_dd.grid(alpha=0.12, color=_GRID_CLR)
    ax_dd.tick_params(colors=_TEXT_CLR)

    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right",
                 fontsize=7, color=_TEXT_CLR)

    plt.suptitle("背景色：暗红=熊市 暗橙=震荡 暗绿=牛市 暗蓝=复苏",
                 fontsize=9, color="#8b949e", y=1.002)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=_DARK_BG)
    plt.close()


# ── HTML 报告 ─────────────────────────────────────────────────────────────────

def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def generate_report(stats_rows, corr_df, trade_log, corr_path, equity_path, out_path,
                    regime_days, top_n, min_score, vol_min, rebalance):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    corr_b64   = _b64(corr_path)
    equity_b64 = _b64(equity_path)

    # 绩效表
    cols = list(stats_rows[0].keys())
    thead = "".join(f"<th>{c}</th>" for c in cols)
    tbody = ""
    for row in stats_rows:
        is_strat = row["标的"] == "策略 Combo"
        style = ' style="background:#1f2d1f;font-weight:700"' if is_strat else ""
        tbody += f"<tr{style}>" + "".join(f"<td>{row[c]}</td>" for c in cols) + "</tr>"

    # 相关性表（颜色编码）
    def corr_color(v):
        if np.isnan(v): return "#333"
        if abs(v) > 0.6: return "#8b0000" if v > 0 else "#003d66"
        if abs(v) > 0.3: return "#7a4f00"
        return "#004d00"

    corr_rows = ""
    for fname in corr_df.index:
        corr_rows += f'<tr><td style="font-weight:600;font-family:monospace">{fname}</td>'
        for col in corr_df.columns:
            v   = corr_df.loc[fname, col]
            bg  = corr_color(v)
            txt = f"{v:.2f}" if not np.isnan(v) else "N/A"
            corr_rows += f'<td style="background:{bg};color:#e6edf3">{txt}</td>'
        corr_rows += "</tr>"
    corr_cols = "".join(f"<th>{c}</th>" for c in corr_df.columns)

    # 最近调仓日志（最后10次）
    trade_rows = ""
    for t in trade_log[-12:]:
        stocks_str = ", ".join(t["stocks"][:5]) + ("..." if len(t["stocks"]) > 5 else "")
        trade_rows += (
            f'<tr><td>{t["date"].date() if hasattr(t["date"],"date") else t["date"]}</td>'
            f'<td>{t["held"]}/{t["n_valid"]}</td>'
            f'<td style="font-size:0.85em;font-family:monospace">{stocks_str}</td></tr>'
        )

    regime_summary = " | ".join(
        f'{label}：{d}天' for label, d in regime_days.items()
    )

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>多因子合成回测报告</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            margin: 0; background: #0d1117; color: #e6edf3; }}
    .main {{ max-width: 1100px; margin: 0 auto; padding: 32px 36px 80px; }}
    h1 {{ color: #fff; border-bottom: 3px solid #ffd60a; padding-bottom: 10px;
          font-size: 1.5em; margin-top: 0; }}
    h2 {{ color: #ffd60a; font-size: 1.05em; margin-top: 44px;
          border-left: 4px solid #ffd60a; padding-left: 12px; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 20px; margin-bottom: 12px; }}
    .meta-item {{ background: #161b22; border: 1px solid #30363d; border-radius: 8px;
                  padding: 12px 18px; text-align: center; min-width: 110px; }}
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
    .note {{ background: #161b22; border-left: 4px solid #ffd60a;
             padding: 12px 16px; border-radius: 0 8px 8px 0;
             margin: 12px 0; font-size: 0.88em; line-height: 1.7;
             border: 1px solid #30363d; border-left: 4px solid #ffd60a; }}
    .note b {{ color: #ffd60a; }}
    .sub {{ font-size: 0.79em; color: #8b949e; margin: 3px 0 10px; }}
    .tag {{ display: inline-block; padding: 2px 10px; border-radius: 12px;
            font-size: 0.82em; font-weight: 700; margin: 2px; }}
    .tag-bull {{ background: #004d00; color: #39d353; }}
    .tag-bear {{ background: #3d0000; color: #f85149; }}
    .tag-neu  {{ background: #4d3800; color: #ffd60a; }}
  </style>
</head>
<body>
<div class="main">
  <h1>🧪 多因子合成回测报告</h1>

  <div class="meta">
    <div class="meta-item"><div class="val">4</div><div class="lbl">核心因子</div></div>
    <div class="meta-item"><div class="val">{top_n}</div><div class="lbl">持仓股数</div></div>
    <div class="meta-item"><div class="val">{rebalance}日</div><div class="lbl">调仓周期</div></div>
    <div class="meta-item"><div class="val">&gt;{min_score}</div><div class="lbl">Combo门槛</div></div>
    <div class="meta-item"><div class="val">&gt;{vol_min}x</div><div class="lbl">量比门槛</div></div>
    <div class="meta-item"><div class="val">{len(trade_log)}</div><div class="lbl">调仓次数</div></div>
    <div class="meta-item"><div class="val">{now}</div><div class="lbl">生成时间</div></div>
  </div>

  <div class="note">
    <b>状态机权重</b><br>
    <span class="tag tag-bull">牛市 RS_Beta×0.4 + LR_Slope×0.3 + MFI_14×0.2 + HV_ratio×0.1</span><br>
    <span class="tag tag-bear">熊市 HV_ratio×0.7 + MFI_14×(−0.3) 反向做超卖</span><br>
    <span class="tag tag-neu">震荡 HV_ratio×0.4 + MFI_14×(−0.4) 高抛低吸</span><br><br>
    <b>宏观状态样本：</b>{regime_summary}
  </div>

  <h2>1. 净值曲线 & 回撤对比</h2>
  <img src="data:image/png;base64,{equity_b64}" alt="净值曲线">

  <h2>2. 绩效统计</h2>
  <table>
    <thead><tr>{thead}</tr></thead>
    <tbody>{tbody}</tbody>
  </table>

  <h2>3. 因子截面相关性</h2>
  <p class="sub">
    每日对 165 只股票的因子值做截面排名，计算 Spearman 相关系数，取时间序列均值。
    绿色 (&lt;0.3) = 互补，越低越好；红色 (&gt;0.6) = 同质（相当于双倍权重同一类信号）
  </p>
  <img src="data:image/png;base64,{corr_b64}" alt="因子相关性">
  <table>
    <thead><tr><th>因子</th>{corr_cols}</tr></thead>
    <tbody>{corr_rows}</tbody>
  </table>

  <h2>4. 最近 12 次调仓记录</h2>
  <p class="sub">展示最近 12 次：调仓日 / 实际买入 vs 通过过滤的候选数 / 持仓股票</p>
  <table>
    <thead><tr><th>调仓日</th><th>买入/候选</th><th>持仓标的</th></tr></thead>
    <tbody>{trade_rows}</tbody>
  </table>
</div>
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML 报告 → {out_path.name}")


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="多因子合成回测")
    parser.add_argument("--since",     default="2022-01-01")
    parser.add_argument("--tf",        default="1d")
    parser.add_argument("--top-n",     type=int,   default=5)
    parser.add_argument("--min-score", type=float, default=1.0)
    parser.add_argument("--vol-min",   type=float, default=1.2)
    parser.add_argument("--rebalance", type=int,   default=5)
    args = parser.parse_args()

    # ── 数据加载 ──────────────────────────────────────────────────────────────
    symbols, _ = load_universe()
    print(f"加载 {len(symbols)} 只股票...")
    close, high, low, vol = load_panel(symbols, args.tf, since=args.since)
    qqq_close = load_benchmark("QQQ", args.tf, since=args.since).reindex(close.index)
    spy_close = load_benchmark("SPY", args.tf, since=args.since).reindex(close.index)
    print(f"  有效标的: {close.shape[1]} 只  {close.index[0].date()} ~ {close.index[-1].date()}")

    liquid = build_liquidity_mask(close, vol)

    # ── 计算因子 ──────────────────────────────────────────────────────────────
    print("计算因子...")
    all_factors = compute_factors(close, high, low, vol, qqq_close)
    core_panels = {f: all_factors[f] for f in CORE_FACTORS if f in all_factors}

    # Vol_Shock（过滤用，不纳入 Z-Score 合成）
    vol_ma20    = vol.rolling(20).mean().replace(0, np.nan)
    vol_shock   = (vol / vol_ma20).reindex(close.index)

    # ── 截面相关性 ────────────────────────────────────────────────────────────
    print("计算因子截面相关性...")
    corr_df = compute_factor_corr(core_panels, liquid)
    print("\n因子截面 Spearman 相关性（时间序列均值）：")
    print(corr_df.round(3).to_string())

    # ── Z-Score 标准化 ────────────────────────────────────────────────────────
    print("\nZ-Score 标准化...")
    z_panels = zscore_factors(core_panels, liquid)

    # ── 宏观状态开关 ──────────────────────────────────────────────────────────
    spy_regime, spy_ma = compute_spy_regime(spy_close)
    regime_days = {
        "牛市": int((spy_regime == 1).sum()),
        "熊市": int((spy_regime == -1).sum()),
        "震荡": int((spy_regime == 0).sum()),
    }
    print(f"宏观状态分布: {regime_days}")

    # ── Combo Score ───────────────────────────────────────────────────────────
    print("计算 Combo Score...")
    combo = compute_combo(z_panels, spy_regime)

    # ── 回测 ──────────────────────────────────────────────────────────────────
    print(f"回测（持仓前{args.top_n}名，每{args.rebalance}日调仓，"
          f"Combo>{args.min_score}，Vol>{args.vol_min}x）...")
    equity, spy_eq, qqq_eq, port_ret, trade_log = run_backtest(
        combo, vol_shock, close, liquid, spy_close, qqq_close,
        min_score=args.min_score, vol_min=args.vol_min,
        top_n=args.top_n, rebalance=args.rebalance,
    )
    print(f"  调仓次数: {len(trade_log)}")

    # ── 绩效统计 ──────────────────────────────────────────────────────────────
    spy_ret_s = spy_close.pct_change().reindex(close.index).fillna(0)
    qqq_ret_s = qqq_close.pct_change().reindex(close.index).fillna(0)

    stats_rows = [
        perf_stats(equity,    port_ret,  "策略 Combo"),
        perf_stats(spy_eq,    spy_ret_s, "SPY"),
        perf_stats(qqq_eq,    qqq_ret_s, "QQQ"),
    ]
    print("\n" + "=" * 60)
    print(f"{'标的':<12}{'总收益':>10}{'年化收益':>10}{'Sharpe':>8}{'最大回撤':>10}")
    print("-" * 60)
    for row in stats_rows:
        print(f"{row['标的']:<12}{row['总收益']:>10}{row['年化收益']:>10}"
              f"{row['Sharpe']:>8}{row['最大回撤']:>10}")
    print("=" * 60)

    # ── 输出 ──────────────────────────────────────────────────────────────────
    corr_path   = DATA_DIR / "combo_corr.png"
    equity_path = DATA_DIR / "combo_equity.png"
    html_path   = DATA_DIR / "combo_report.html"

    print("\n生成图表...")
    plot_corr(corr_df, corr_path)
    plot_equity(equity, spy_eq, qqq_eq, spy_regime, spy_ma, port_ret, equity_path)

    generate_report(
        stats_rows, corr_df, trade_log,
        corr_path, equity_path, html_path,
        regime_days=regime_days,
        top_n=args.top_n, min_score=args.min_score,
        vol_min=args.vol_min, rebalance=args.rebalance,
    )

    print(f"\n全部输出已保存至 {DATA_DIR}/")
    print(f"  用浏览器打开: open {html_path}")


if __name__ == "__main__":
    main()
