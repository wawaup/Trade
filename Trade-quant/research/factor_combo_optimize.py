"""
多因子策略优化实验（第二轮）— 以 QQQ MA50 为新 Baseline

Baseline   — QQQ MA50 + 原权重（已采纳，主脚本当前版本）
Exp-A      — BIAS_20 + QQQ MA50（替换 LR_Slope，测试能否在新基准上继续改善）
Exp-B      — 合成动量（RS×0.25 + LR×0.25，第一轮已证明更差，作为反面对照保留）
Exp-D      — ATR 2.5x 止损 + QQQ MA50（第一轮单独更差，叠加新基准后再测）
Exp-E      — 最佳组合（Exp-A + Exp-D，以新基准为底）
Exp-F0     — 摩擦成本注入：仅手续费 0.1% 单边（轻度）
Exp-F1     — 摩擦成本注入：手续费 + 滑点 0.25% 单边（实盘保守估计）
W-Scan     — RS_Beta 权重扫描（0.20~0.60）—— 现在基于 QQQ MA50 重新跑
T-Scan     — 门槛敏感度（Combo/Vol 二维）—— 同上
N-Scan     — Top-N 持仓数量扫描（3/5/7/10/15）
Hold-out   — 样本内（2022-24）vs 样本外（2025-26）一致性检验

用法：
  python factor_combo_optimize.py
  python factor_combo_optimize.py --since 2022-01-01 --top-n 5 --rebalance 5
"""
import argparse
import base64
import sys
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

sys.path.insert(0, str(Path(__file__).parent))
from factor_scanner import (
    load_panel, load_benchmark, load_universe, build_liquidity_mask,
    compute_factors, compute_spy_regime, REGIMES,
    _DARK_BG, _DARK_AX, _GRID_CLR, _TEXT_CLR,
    _dark_fig,
)
from factor_combo_backtest import zscore_factors

DATA_DIR   = Path(__file__).parent.parent / "data"
REPORT_DIR = Path(__file__).parent.parent / "report"
REPORT_DIR.mkdir(exist_ok=True)

# ── 权重矩阵 ──────────────────────────────────────────────────────────────────

W_BASELINE = {
    1:  {"RS_Beta": 0.40, "MFI_14":  0.20, "LR_Slope":  0.30, "HV_ratio": 0.10},
    -1: {"RS_Beta": 0.00, "MFI_14": -0.30, "LR_Slope":  0.00, "HV_ratio": 0.70},
    0:  {"RS_Beta": 0.10, "MFI_14": -0.40, "LR_Slope":  0.10, "HV_ratio": 0.40},
}

# BIAS_20 是反转因子（IC 为负），负权重 = 惩罚过热股、奖励超卖股
W_BIAS20 = {
    1:  {"RS_Beta": 0.50, "MFI_14":  0.20, "BIAS_20": -0.20, "HV_ratio": 0.10},
    -1: {"RS_Beta": 0.00, "MFI_14": -0.30, "BIAS_20":  0.20, "HV_ratio": 0.50},
    0:  {"RS_Beta": 0.10, "MFI_14": -0.30, "BIAS_20":  0.10, "HV_ratio": 0.40},
}

# 合成动量：RS_Beta 和 LR_Slope 各给一半，降低相关性带来的权重虚胖
W_COMPOSITE = {
    1:  {"RS_Beta": 0.25, "MFI_14":  0.20, "LR_Slope": 0.25, "HV_ratio": 0.30},
    -1: {"RS_Beta": 0.00, "MFI_14": -0.30, "LR_Slope": 0.00, "HV_ratio": 0.70},
    0:  {"RS_Beta": 0.05, "MFI_14": -0.40, "LR_Slope": 0.05, "HV_ratio": 0.40},
}

# ── 基础计算工具 ───────────────────────────────────────────────────────────────

def compute_combo_generic(z_panels, regime, weights):
    """通用 Combo Score：接受任意 z_panels 子集和权重矩阵"""
    ref = next(iter(z_panels.values()))
    combo = pd.DataFrame(0.0, index=ref.index, columns=ref.columns)
    for regime_val, w_dict in weights.items():
        dates = regime[regime == regime_val].index.intersection(combo.index)
        if len(dates) == 0:
            continue
        for fname, w in w_dict.items():
            if w == 0 or fname not in z_panels:
                continue
            combo.loc[dates] += z_panels[fname].reindex(dates).fillna(0) * w
    return combo


def compute_atr(close, high, low, window=14):
    """True Range 均值（DataFrame 版，向量化）"""
    prev_c = close.shift(1)
    hl  = high - low
    hpc = (high - prev_c).abs()
    lpc = (low  - prev_c).abs()
    tr  = pd.DataFrame(
        np.maximum(hl.values, np.maximum(hpc.values, lpc.values)),
        index=close.index, columns=close.columns,
    )
    return tr.rolling(window).mean()


def compute_qqq_regime(qqq_close, ma_window=50, buffer=0.0):
    """QQQ MA50 快速状态开关（buffer=0 = 无缓冲，更灵敏）"""
    ma = qqq_close.rolling(ma_window).mean()
    regime = pd.Series(0, index=qqq_close.index, name="regime")
    regime[qqq_close > ma * (1 + buffer)] =  1
    regime[qqq_close < ma * (1 - buffer)] = -1
    return regime


def perf_stats_raw(equity, ret_series, label=""):
    """返回原始浮点值，便于排序和比较"""
    n_days  = len(ret_series)
    n_years = n_days / 252
    total_r = equity.iloc[-1] - 1
    cagr    = equity.iloc[-1] ** (1 / n_years) - 1
    vol     = ret_series.std() * 252 ** 0.5
    sharpe  = (ret_series.mean() * 252) / vol if vol > 0 else 0
    dd      = equity / equity.cummax() - 1
    max_dd  = dd.min()
    calmar  = cagr / abs(max_dd) if max_dd != 0 else np.nan
    return {
        "实验":     label,
        "总收益":   total_r,
        "年化收益": cagr,
        "年化波动": vol,
        "Sharpe":   sharpe,
        "最大回撤": max_dd,
        "Calmar":   calmar,
    }


# ── 通用回测引擎 ───────────────────────────────────────────────────────────────

def run_experiment(z_panels, vol_shock, close, high, low, liquid_mask,
                   spy_close, qqq_close, regime, weights,
                   min_score=1.0, vol_min=1.2, top_n=5, rebalance=5,
                   atr_stop_mult=None, friction=0.0):
    """
    通用实验回测：接受任意 z_panels / regime / weights。

    atr_stop_mult : 非 None 时开启 ATR 追踪止损（每日检查）。
    friction      : 单边摩擦成本率（手续费+滑点），调仓日按换手比例扣减。
                    0.0010 = 0.1% 单边（轻），0.0025 = 0.25% 单边（保守实盘）。
    """
    combo   = compute_combo_generic(z_panels, regime, weights)
    dates   = close.index
    fwd_ret = close.pct_change()
    spy_ret = spy_close.pct_change().reindex(dates).fillna(0)
    qqq_ret = qqq_close.pct_change().reindex(dates).fillna(0)

    port_ret     = pd.Series(0.0, index=dates)
    holdings     = {}
    entry_prices = {}
    trade_log    = []

    atr14 = compute_atr(close, high, low, 14) if atr_stop_mult else None

    for i, date in enumerate(dates):
        if i == 0:
            continue
        prev = dates[i - 1]

        # ATR 追踪止损（调仓日之前先检查）
        if atr_stop_mult and holdings:
            stopped = []
            for sym, ep in list(entry_prices.items()):
                if sym not in close.columns or prev not in close.index:
                    continue
                prev_p  = close.at[prev, sym]
                atr_val = atr14.at[prev, sym] if (prev in atr14.index and
                          sym in atr14.columns) else np.nan
                if not any(np.isnan(v) for v in [prev_p, ep, atr_val]) and ep > 0:
                    if prev_p < ep - atr_stop_mult * atr_val:
                        stopped.append(sym)
            for sym in stopped:
                holdings.pop(sym, None)
                entry_prices.pop(sym, None)
            if stopped and holdings:
                w = 1 / len(holdings)
                holdings = {s: w for s in holdings}

        # 定期调仓
        if i % rebalance == 0:
            scores = (combo.loc[prev]      if prev in combo.index
                      else pd.Series(dtype=float))
            vs     = (vol_shock.loc[prev]  if prev in vol_shock.index
                      else pd.Series(dtype=float))
            lm     = (liquid_mask.loc[prev].fillna(False)
                      if prev in liquid_mask.index
                      else pd.Series(True, index=scores.index))

            valid = scores[
                lm.reindex(scores.index, fill_value=False) &
                (scores > min_score) &
                (vs.reindex(scores.index, fill_value=0) > vol_min)
            ].dropna()

            top   = valid.nlargest(top_n)
            new_h = {s: 1 / len(top) for s in top.index} if len(top) > 0 else {}

            if atr_stop_mult:
                for sym in new_h:
                    if sym not in holdings and date in close.index and sym in close.columns:
                        ep = close.at[date, sym]
                        if not np.isnan(ep):
                            entry_prices[sym] = ep
                entry_prices = {s: ep for s, ep in entry_prices.items() if s in new_h}

            # 摩擦成本：新进仓 round-trip * 2，平仓 single * 1
            if friction > 0:
                n_exit    = sum(1 for s in holdings if s not in new_h)
                n_enter   = sum(1 for s in new_h    if s not in holdings)
                old_n     = len(holdings) if holdings else 1
                new_n     = len(new_h)    if new_h    else 1
                cost = (n_exit   / old_n) * friction + \
                       (n_enter  / new_n) * friction * 2
                port_ret.at[date] = port_ret.at[date] - cost

            holdings = new_h
            trade_log.append({
                "date": date, "n_valid": len(valid),
                "stocks": list(top.index), "held": len(holdings),
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
    return equity, spy_equity, qqq_equity, port_ret, trade_log


# ── 扫描实验 ───────────────────────────────────────────────────────────────────

def run_weight_scan(z_panels_base, vol_shock, close, high, low, liquid,
                    spy_close, qqq_close, spy_regime,
                    min_score, vol_min, top_n, rebalance):
    """RS_Beta 权重从 0.20~0.60 扫描，LR_Slope 补足差额"""
    print("  权重敏感度扫描...")
    rows = []
    for rs_w in np.round(np.arange(0.20, 0.65, 0.05), 2):
        lr_w = round(0.70 - rs_w, 2)   # RS + LR 合计 = 0.70（固定 MFI=0.20 HV=0.10）
        weights = {
            1:  {"RS_Beta": rs_w, "MFI_14": 0.20, "LR_Slope": lr_w, "HV_ratio": 0.10},
            -1: W_BASELINE[-1],
            0:  W_BASELINE[0],
        }
        eq, _, _, ret, _ = run_experiment(
            z_panels_base, vol_shock, close, high, low, liquid,
            spy_close, qqq_close, spy_regime, weights,
            min_score=min_score, vol_min=vol_min,
            top_n=top_n, rebalance=rebalance,
        )
        r = perf_stats_raw(eq, ret, f"RS={rs_w:.2f} LR={lr_w:.2f}")
        r["RS_w"] = rs_w
        rows.append(r)
    return pd.DataFrame(rows)


def run_threshold_scan(z_panels_base, vol_shock, close, high, low, liquid,
                       spy_close, qqq_close, spy_regime,
                       top_n, rebalance):
    """Combo 门槛 × Vol 门槛 二维扫描"""
    print("  门槛敏感度扫描...")
    rows = []
    for min_s in [0.5, 1.0, 1.5, 2.0]:
        for vol_m in [1.0, 1.2, 1.5]:
            eq, _, _, ret, _ = run_experiment(
                z_panels_base, vol_shock, close, high, low, liquid,
                spy_close, qqq_close, spy_regime, W_BASELINE,
                min_score=min_s, vol_min=vol_m,
                top_n=top_n, rebalance=rebalance,
            )
            r = perf_stats_raw(eq, ret, f"C>{min_s} V>{vol_m}")
            r["min_score"] = min_s
            r["vol_min"]   = vol_m
            rows.append(r)
    return pd.DataFrame(rows)


def run_holdout(z_panels_base, vol_shock, close, high, low, liquid,
                spy_close, qqq_close, spy_regime,
                min_score, vol_min, top_n, rebalance,
                split="2025-01-01"):
    """样本内（2022-24）vs 样本外（2025-26）一致性检验"""
    print("  Hold-out 验证...")
    eq_full, _, _, ret_full, _ = run_experiment(
        z_panels_base, vol_shock, close, high, low, liquid,
        spy_close, qqq_close, spy_regime, W_BASELINE,
        min_score=min_score, vol_min=vol_min,
        top_n=top_n, rebalance=rebalance,
    )
    split_ts = pd.Timestamp(split)

    ret_is  = ret_full[ret_full.index < split_ts]
    ret_oos = ret_full[ret_full.index >= split_ts]
    eq_is   = (1 + ret_is).cumprod()
    eq_oos  = (1 + ret_oos).cumprod()

    spy_ret_s  = spy_close.pct_change().reindex(close.index).fillna(0)
    ret_spy_is  = spy_ret_s[spy_ret_s.index < split_ts]
    ret_spy_oos = spy_ret_s[spy_ret_s.index >= split_ts]
    eq_spy_is   = (1 + ret_spy_is).cumprod()
    eq_spy_oos  = (1 + ret_spy_oos).cumprod()

    stats_is     = perf_stats_raw(eq_is,  ret_is,  f"策略 IS（<{split}）")
    stats_oos    = perf_stats_raw(eq_oos, ret_oos, f"策略 OOS（≥{split}）")
    stats_spy_is  = perf_stats_raw(eq_spy_is,  ret_spy_is,  f"SPY IS（<{split}）")
    stats_spy_oos = perf_stats_raw(eq_spy_oos, ret_spy_oos, f"SPY OOS（≥{split}）")

    return [stats_is, stats_oos, stats_spy_is, stats_spy_oos]


def run_topn_scan(z_panels_base, vol_shock, close, high, low, liquid,
                  spy_close, qqq_close, regime,
                  min_score, vol_min, rebalance):
    """持仓数量 Top-N 扫描（3/5/7/10/15）"""
    print("  Top-N 持仓数扫描...")
    rows = []
    for n in [3, 5, 7, 10, 15]:
        eq, _, _, ret, _ = run_experiment(
            z_panels_base, vol_shock, close, high, low, liquid,
            spy_close, qqq_close, regime, W_BASELINE,
            min_score=min_score, vol_min=vol_min,
            top_n=n, rebalance=rebalance,
        )
        r = perf_stats_raw(eq, ret, f"Top-{n}")
        r["top_n"] = n
        rows.append(r)
    return pd.DataFrame(rows)


# ── 图表 ───────────────────────────────────────────────────────────────────────

PALETTE = [
    "#ffd60a",  # Baseline   黄
    "#f85149",  # Exp-A      红
    "#79c0ff",  # Exp-B      蓝
    "#39d353",  # Exp-C      绿
    "#ff7b72",  # Exp-D      橙红
    "#d2a8ff",  # Exp-E      紫
    "#58a6ff",  # SPY        淡蓝
    "#3fb950",  # QQQ        淡绿
]


def plot_comparison(exp_dict, spy_eq, qqq_eq, out_path):
    """多实验净值曲线对比图"""
    fig, axes = _dark_fig(2, (15, 9))
    ax_eq, ax_dd = axes

    for ax in axes:
        for rname, (rs, re) in REGIMES.items():
            rc = {"熊市(2022)": "#8b0000", "反弹(22Q4)": "#7a4f00",
                  "AI牛市(23-24)": "#004d00", "关税震荡(25H1)": "#7a4f00",
                  "复苏(25H2+)": "#003d66"}.get(rname, "#333")
            x0 = max(pd.Timestamp(rs), spy_eq.index.min())
            x1 = min(pd.Timestamp(re), spy_eq.index.max())
            if x0 < x1:
                ax.axvspan(x0, x1, alpha=0.18, color=rc)

    for idx, (name, eq) in enumerate(exp_dict.items()):
        color = PALETTE[idx % len(PALETTE)]
        lw    = 2.2 if idx == 0 else 1.3
        ls    = "-"  if idx == 0 else ("--" if idx % 2 else "-")
        ax_eq.plot(eq.index, eq.values, color=color, lw=lw, ls=ls, label=name)
        dd = eq / eq.cummax() - 1
        ax_dd.plot(dd.index, dd.values, color=color, lw=lw * 0.7, ls=ls, alpha=0.85)

    ax_eq.plot(spy_eq.index, spy_eq.values, color="#58a6ff", lw=1.0, ls=":", label="SPY")
    ax_eq.plot(qqq_eq.index, qqq_eq.values, color="#3fb950", lw=1.0, ls=":", label="QQQ")
    spy_dd = spy_eq / spy_eq.cummax() - 1
    ax_dd.plot(spy_dd.index, spy_dd.values, color="#58a6ff", lw=0.8, ls=":", alpha=0.6)

    ax_eq.axhline(1, color=_GRID_CLR, lw=0.6, ls="--")
    ax_eq.set_ylabel("净值（起始=1）", color=_TEXT_CLR)
    ax_eq.set_title("各实验净值曲线对比", color=_TEXT_CLR, fontsize=11)
    ax_eq.legend(facecolor=_DARK_AX, labelcolor=_TEXT_CLR,
                 edgecolor=_GRID_CLR, fontsize=8, ncol=2)
    ax_eq.grid(alpha=0.12, color=_GRID_CLR)
    ax_eq.tick_params(colors=_TEXT_CLR)

    ax_dd.axhline(0, color=_GRID_CLR, lw=0.5)
    ax_dd.set_ylabel("回撤", color=_TEXT_CLR)
    ax_dd.set_title("最大回撤对比（越平越好）", color=_TEXT_CLR, fontsize=10)
    ax_dd.grid(alpha=0.12, color=_GRID_CLR)
    ax_dd.tick_params(colors=_TEXT_CLR)

    for ax in axes:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right",
                 fontsize=7, color=_TEXT_CLR)

    plt.suptitle("背景：暗红=熊市 暗橙=震荡 暗绿=牛市 暗蓝=复苏",
                 fontsize=9, color="#8b949e", y=1.002)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=_DARK_BG)
    plt.close()


def plot_weight_scan(df_scan, out_path):
    """权重扫描结果：Sharpe / 年化收益 / 最大回撤随 RS_Beta 权重变化"""
    fig, axes = _dark_fig(3, (12, 9))
    ax_s, ax_c, ax_d = axes
    xs = df_scan["RS_w"].values

    for ax, col, label, color in [
        (ax_s, "Sharpe",   "Sharpe 比率",  "#ffd60a"),
        (ax_c, "年化收益", "年化收益",      "#39d353"),
        (ax_d, "最大回撤", "最大回撤（负）", "#f85149"),
    ]:
        ys = df_scan[col].values
        ax.plot(xs, ys, color=color, lw=2, marker="o", markersize=6)
        best_i = np.argmax(ys) if col != "最大回撤" else np.argmax(ys)
        ax.axvline(xs[best_i], color=color, lw=0.8, ls="--", alpha=0.6)
        ax.scatter([xs[best_i]], [ys[best_i]], color=color, s=80, zorder=5)
        ax.set_ylabel(label, color=_TEXT_CLR)
        ax.set_xlabel("RS_Beta 牛市权重", color=_TEXT_CLR)
        ax.axvline(0.40, color="#8b949e", lw=0.8, ls=":", alpha=0.5, label="Baseline=0.40")
        ax.legend(facecolor=_DARK_AX, labelcolor=_TEXT_CLR, edgecolor=_GRID_CLR, fontsize=8)
        ax.grid(alpha=0.15, color=_GRID_CLR)
        ax.tick_params(colors=_TEXT_CLR)

    plt.suptitle("RS_Beta 牛市权重敏感度扫描（LR_Slope 补足差额至0.70）",
                 fontsize=10, color="#8b949e")
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=_DARK_BG)
    plt.close()


# ── HTML 报告 ─────────────────────────────────────────────────────────────────

def _b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def _fmt(val, col):
    if isinstance(val, float) and np.isnan(val):
        return "N/A"
    if col in ("总收益", "年化收益", "年化波动", "最大回撤"):
        return f"{val:+.1%}" if col != "年化波动" else f"{val:.1%}"
    if col in ("Sharpe", "Calmar"):
        return f"{val:.2f}"
    return str(val)


def _row_color(stats, baseline_stats, col):
    """比 Baseline 好 → 绿色，差 → 红色"""
    v  = stats.get(col)
    b  = baseline_stats.get(col)
    if v is None or b is None or not isinstance(v, float) or np.isnan(v):
        return ""
    better = (v > b) if col != "最大回撤" else (v > b)  # 回撤负数，更大（接近0）= 更好
    if abs(v - b) < 1e-6:
        return ""
    return "color:#39d353;font-weight:700" if better else "color:#f85149"


def generate_report(main_stats, scan_w, scan_t, scan_n, holdout_stats,
                    cmp_img, wscan_img, out_path):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    cmp_b64   = _b64(cmp_img)
    wscan_b64 = _b64(wscan_img)
    baseline  = main_stats[0]   # Baseline 永远是第一行

    # 主实验表
    cols_main = ["实验", "总收益", "年化收益", "年化波动", "Sharpe", "最大回撤", "Calmar"]
    main_header = "".join(f"<th>{c}</th>" for c in cols_main)
    main_body   = ""
    for row in main_stats:
        is_base = row["实验"] == "Baseline"
        style_row = ' style="background:#1f2d1f;font-weight:700"' if is_base else ""
        cells = ""
        for c in cols_main:
            v = row.get(c, "")
            fv = _fmt(v, c) if isinstance(v, float) else str(v)
            cs = _row_color(row, baseline, c) if not is_base and isinstance(v, float) else ""
            cells += f'<td style="{cs}">{fv}</td>'
        main_body += f"<tr{style_row}>{cells}</tr>"

    # 权重扫描表
    scan_cols = ["实验", "年化收益", "Sharpe", "最大回撤", "Calmar"]
    wscan_header = "".join(f"<th>{c}</th>" for c in scan_cols)
    wscan_body = ""
    best_sharpe_i = scan_w["Sharpe"].idxmax()
    for i, row in scan_w.iterrows():
        is_base = abs(row.get("RS_w", 99) - 0.40) < 0.01
        is_best = (i == best_sharpe_i)
        style_row = ' style="background:#1f2d1f"' if is_base else (
                    ' style="background:#0d2f0d"' if is_best else "")
        cells = ""
        for c in scan_cols:
            v = row.get(c, "")
            fv = _fmt(v, c) if isinstance(v, float) else str(v)
            cells += f"<td>{fv}</td>"
        main_body_tag = " ★最优 Sharpe" if is_best else (" (Baseline)" if is_base else "")
        wscan_body += f"<tr{style_row}><td>{row['实验']}{main_body_tag}</td>{''.join(f'<td>{_fmt(row[c],c)}</td>' for c in scan_cols[1:])}</tr>"

    # 门槛扫描表（Pivot: rows=min_score, cols=vol_min）
    thresh_pivot = scan_t.pivot(index="min_score", columns="vol_min", values="Sharpe")
    thresh_header = "<th>Combo↓ Vol→</th>" + "".join(
        f"<th>Vol>{v}</th>" for v in thresh_pivot.columns)
    thresh_body = ""
    for ms, row_t in thresh_pivot.iterrows():
        cells = f"<td style='font-weight:700'>C>{ms}</td>"
        for v in thresh_pivot.columns:
            val = row_t[v]
            is_base = (abs(ms - 1.0) < 0.01 and abs(v - 1.2) < 0.01)
            best    = (thresh_pivot.values.max() == val)
            style   = ("background:#0d2f0d;font-weight:700" if best
                       else ("background:#1f2d1f" if is_base else ""))
            cells += f'<td style="{style}">{val:.2f}{"★" if best else ""}</td>'
        thresh_body += f"<tr>{cells}</tr>"

    # Top-N 表
    topn_body = ""
    best_n_i  = scan_n["Sharpe"].idxmax()
    for i, row in scan_n.iterrows():
        is_base = (int(row.get("top_n", 0)) == 5)
        is_best = (i == best_n_i)
        style_row = (' style="background:#0d2f0d;font-weight:700"' if is_best
                     else ' style="background:#1f2d1f"' if is_base else "")
        cells = f"<td>Top-{int(row['top_n'])}{'★' if is_best else ''}</td>"
        for c in ["总收益", "年化收益", "年化波动", "Sharpe", "最大回撤", "Calmar"]:
            v = row.get(c, np.nan)
            cells += f"<td>{_fmt(v, c)}</td>"
        topn_body += f"<tr{style_row}>{cells}</tr>"

    # Hold-out 表
    ho_header = "".join(f"<th>{c}</th>" for c in cols_main)
    ho_body   = ""
    for row in holdout_stats:
        is_oos  = "OOS" in row.get("实验", "")
        is_strat = "策略" in row.get("实验", "")
        style_row = ' style="background:#1f2d1f;font-weight:700"' if is_strat and is_oos else (
                    ' style="background:#161f28"' if is_strat else "")
        cells = ""
        for c in cols_main:
            v = row.get(c, "")
            fv = _fmt(v, c) if isinstance(v, float) else str(v)
            cells += f"<td>{fv}</td>"
        ho_body += f"<tr{style_row}>{cells}</tr>"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <title>多因子策略优化实验报告</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            margin: 0; background: #0d1117; color: #e6edf3; }}
    .main {{ max-width: 1200px; margin: 0 auto; padding: 32px 36px 80px; }}
    h1 {{ color: #fff; border-bottom: 3px solid #ffd60a; padding-bottom: 10px;
          font-size: 1.5em; margin-top: 0; }}
    h2 {{ color: #ffd60a; font-size: 1.05em; margin-top: 44px;
          border-left: 4px solid #ffd60a; padding-left: 12px; }}
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
    .sub {{ font-size: 0.79em; color: #8b949e; margin: 3px 0 10px; }}
    .meta {{ display:flex; flex-wrap:wrap; gap:14px; margin:12px 0 }}
    .meta-item {{ background:#161b22; border:1px solid #30363d; border-radius:8px;
                  padding:10px 16px; text-align:center; min-width:100px }}
    .meta-item .val {{ font-size:1.2em; font-weight:700; color:#ffd60a }}
    .meta-item .lbl {{ font-size:0.76em; color:#8b949e; margin-top:3px }}
    .tag {{ display:inline-block; padding:3px 10px; border-radius:12px;
            font-size:0.82em; font-weight:700; margin:2px }}
  </style>
</head>
<body>
<div class="main">
  <h1>⚗️ 多因子策略优化实验报告</h1>
  <p class="sub">生成时间：{now} &nbsp;|&nbsp; 绿色 = 优于 Baseline &nbsp; 红色 = 劣于 Baseline</p>

  <div class="note">
    <b>实验说明（第二轮，Baseline = QQQ MA50 + 原权重）：</b><br>
    <span class="tag" style="background:#2d2800;color:#ffd60a">Baseline</span>
    QQQ MA50 状态开关 + RS_Beta×0.4 + LR_Slope×0.3 + MFI_14×0.2 + HV_ratio×0.1（主脚本当前版本）<br>
    <span class="tag" style="background:#2d0a0a;color:#f85149">Exp-A</span>
    BIAS_20 替换 LR_Slope（在新基准上能否继续改善？）<br>
    <span class="tag" style="background:#0a1a2d;color:#79c0ff">Exp-B</span>
    合成动量：RS_Beta/LR_Slope 各×0.25，HV_ratio 提至0.3<br>
    <span class="tag" style="background:#0a2d0a;color:#39d353">Exp-C</span>
    QQQ MA50 快速状态开关（替换 SPY MA200，更快捕捉趋势转折）<br>
    <span class="tag" style="background:#2d1200;color:#ff7b72">Exp-D</span>
    ATR 2.5x 追踪止损（每日检查，不等调仓日）<br>
    <span class="tag" style="background:#1a0d2d;color:#d2a8ff">Exp-E</span>
    最佳组合：Exp-A + Exp-D（基于新 Baseline）<br>
    <span class="tag" style="background:#0a1a0a;color:#3fb950">Exp-F0</span>
    摩擦成本轻度：手续费 0.1% 单边（约 IBKR 散户水平）<br>
    <span class="tag" style="background:#0a1a0a;color:#3fb950">Exp-F1</span>
    摩擦成本保守：手续费+滑点 0.25% 单边（实盘保守估计）
  </div>

  <h2>1. 主实验对比</h2>
  <p class="sub">SPY/QQQ 同期绩效仅供参照，不参与颜色对比</p>
  <table>
    <thead><tr>{main_header}</tr></thead>
    <tbody>{main_body}</tbody>
  </table>

  <h2>2. 净值曲线 & 回撤对比</h2>
  <img src="data:image/png;base64,{cmp_b64}" alt="净值曲线对比">

  <h2>3. 权重敏感度扫描</h2>
  <p class="sub">RS_Beta 牛市权重从 0.20 到 0.60 扫描，LR_Slope 补足（RS+LR=0.70）。
  曲线若是"山峰"则参数脆弱；若是"高原"则鲁棒。</p>
  <img src="data:image/png;base64,{wscan_b64}" alt="权重敏感度">
  <table>
    <thead><tr>{wscan_header}</tr></thead>
    <tbody>{wscan_body}</tbody>
  </table>

  <h2>4. 门槛敏感度（Sharpe 矩阵）</h2>
  <p class="sub">行=Combo 门槛，列=量比门槛。★ = 最高 Sharpe，黑底 = Baseline 位置</p>
  <table>
    <thead><tr>{thresh_header}</tr></thead>
    <tbody>{thresh_body}</tbody>
  </table>

  <h2>5. 持仓数量（Top-N）扫描</h2>
  <p class="sub">持仓越少集中度越高；越多分散但会稀释 alpha。找 Sharpe 最优的持仓数量。</p>
  <table>
    <thead><tr><th>持仓数</th><th>总收益</th><th>年化收益</th><th>年化波动</th><th>Sharpe</th><th>最大回撤</th><th>Calmar</th></tr></thead>
    <tbody>{topn_body}</tbody>
  </table>

  <h2>6. Hold-out 样本外验证</h2>
  <p class="sub">用 IS（2022-2024）调好参数，在 OOS（2025-2026）盲跑。
  若 OOS Sharpe/回撤 与 IS 差距 &lt;30%，说明策略泛化能力较好。</p>
  <table>
    <thead><tr>{ho_header}</tr></thead>
    <tbody>{ho_body}</tbody>
  </table>
</div>
</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML 报告 → {out_path.name}")


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="多因子策略优化实验")
    parser.add_argument("--since",     default="2022-01-01")
    parser.add_argument("--tf",        default="1d")
    parser.add_argument("--top-n",     type=int,   default=5)
    parser.add_argument("--min-score", type=float, default=1.0)
    parser.add_argument("--vol-min",   type=float, default=1.2)
    parser.add_argument("--rebalance", type=int,   default=5)
    parser.add_argument("--holdout-split", default="2025-01-01")
    args = parser.parse_args()

    # ── 数据加载 ──────────────────────────────────────────────────────────────
    symbols, _ = load_universe()
    print(f"加载 {len(symbols)} 只股票...")
    close, high, low, vol = load_panel(symbols, args.tf, since=args.since)
    qqq_close = load_benchmark("QQQ", args.tf, since=args.since).reindex(close.index)
    spy_close = load_benchmark("SPY", args.tf, since=args.since).reindex(close.index)
    print(f"  {close.index[0].date()} ~ {close.index[-1].date()}  {close.shape[1]} 只")

    liquid = build_liquidity_mask(close, vol)

    # ── 因子计算 ──────────────────────────────────────────────────────────────
    print("计算因子...")
    all_factors = compute_factors(close, high, low, vol, qqq_close)

    vol_ma20  = vol.rolling(20).mean().replace(0, np.nan)
    vol_shock = (vol / vol_ma20).reindex(close.index)

    # ── Z-Score（两套面板：含 BIAS_20 和 不含 BIAS_20）────────────────────────
    print("Z-Score 标准化...")
    factors_base   = {f: all_factors[f] for f in
                      ["RS_Beta", "MFI_14", "LR_Slope", "HV_ratio"] if f in all_factors}
    factors_bias20 = {f: all_factors[f] for f in
                      ["RS_Beta", "MFI_14", "HV_ratio", "BIAS_20"] if f in all_factors}

    z_base   = zscore_factors(factors_base,   liquid)
    z_bias20 = zscore_factors(factors_bias20, liquid)

    # ── 宏观状态（QQQ MA50 为新基准）─────────────────────────────────────────
    qqq_regime = compute_qqq_regime(qqq_close, ma_window=50)
    regime_days = {k: int((qqq_regime == v).sum())
                   for k, v in {"牛":1, "熊":-1, "震荡":0}.items()}
    print(f"  QQQ MA50 状态分布: {regime_days}")

    # ── 主实验（Baseline = QQQ MA50 当前主脚本版本）──────────────────────────
    print("\n运行主实验...")
    common_kw = dict(
        vol_shock=vol_shock, close=close, high=high, low=low,
        liquid_mask=liquid, spy_close=spy_close, qqq_close=qqq_close,
        min_score=args.min_score, vol_min=args.vol_min,
        top_n=args.top_n, rebalance=args.rebalance,
    )

    # (name, z_panels, regime, weights, atr_stop_mult, friction)
    experiments_cfg = [
        ("Baseline (QQQ MA50)", z_base,   qqq_regime, W_BASELINE,  None, 0.0),
        ("Exp-A: BIAS_20",      z_bias20, qqq_regime, W_BIAS20,    None, 0.0),
        ("Exp-B: 合成动量",     z_base,   qqq_regime, W_COMPOSITE, None, 0.0),
        ("Exp-D: ATR止损",      z_base,   qqq_regime, W_BASELINE,  2.5,  0.0),
        ("Exp-E: A+D",          z_bias20, qqq_regime, W_BIAS20,    2.5,  0.0),
        ("Exp-F0: 手续费0.1%",  z_base,   qqq_regime, W_BASELINE,  None, 0.001),
        ("Exp-F1: 滑点+费0.25%",z_base,   qqq_regime, W_BASELINE,  None, 0.0025),
    ]

    main_stats  = []
    equity_dict = {}
    spy_eq = qqq_eq = None

    for name, zp, regime, weights, atr_mult, fric in experiments_cfg:
        print(f"  {name}...")
        eq, spy_eq, qqq_eq, ret, _ = run_experiment(
            z_panels=zp, regime=regime, weights=weights,
            atr_stop_mult=atr_mult, friction=fric, **common_kw,
        )
        main_stats.append(perf_stats_raw(eq, ret, name))
        equity_dict[name] = eq

    # ── 扫描实验 ──────────────────────────────────────────────────────────────
    print("\n运行扫描实验...")
    scan_kw = dict(
        vol_shock=vol_shock, close=close, high=high, low=low,
        liquid=liquid, spy_close=spy_close, qqq_close=qqq_close,
        top_n=args.top_n, rebalance=args.rebalance,
    )
    scan_w = run_weight_scan(
        z_base, min_score=args.min_score, vol_min=args.vol_min,
        spy_regime=qqq_regime, **scan_kw,
    )
    scan_t = run_threshold_scan(z_base, spy_regime=qqq_regime, **scan_kw)
    scan_n = run_topn_scan(
        z_base, vol_shock, close, high, low, liquid,
        spy_close, qqq_close, qqq_regime,
        args.min_score, args.vol_min, args.rebalance,
    )
    holdout_stats = run_holdout(
        z_base, vol_shock, close, high, low, liquid,
        spy_close, qqq_close, qqq_regime,
        args.min_score, args.vol_min, args.top_n, args.rebalance,
        split=args.holdout_split,
    )

    # ── 打印汇总 ──────────────────────────────────────────────────────────────
    print("\n" + "=" * 82)
    print(f"{'实验':<26}{'总收益':>10}{'年化':>8}{'Sharpe':>8}{'最大回撤':>10}{'Calmar':>8}")
    print("-" * 82)
    baseline = main_stats[0]
    for row in main_stats:
        flag = ""
        if row["实验"] != baseline["实验"]:
            flag = " ↑" if row["Sharpe"] > baseline["Sharpe"] else " ↓"
        print(f"{row['实验']:<26}"
              f"{row['总收益']:>10.1%}{row['年化收益']:>8.1%}"
              f"{row['Sharpe']:>8.2f}{row['最大回撤']:>10.1%}"
              f"{row['Calmar']:>8.2f}{flag}")
    print("=" * 82)

    best_w_idx = scan_w["Sharpe"].idxmax()
    print(f"\n权重扫描最优 Sharpe={scan_w.loc[best_w_idx,'Sharpe']:.2f} "
          f"（RS_Beta={scan_w.loc[best_w_idx,'RS_w']:.2f}）")
    print("\nTop-N 扫描：")
    for _, r in scan_n.iterrows():
        print(f"  Top-{int(r['top_n']):<3} Sharpe={r['Sharpe']:.2f}  "
              f"年化={r['年化收益']:.1%}  MaxDD={r['最大回撤']:.1%}")
    print("\nHold-out：")
    for r in holdout_stats:
        print(f"  {r['实验']:<30} Sharpe={r['Sharpe']:.2f}  MaxDD={r['最大回撤']:.1%}")

    # ── 图表 ──────────────────────────────────────────────────────────────────
    print("\n生成图表...")
    cmp_path   = REPORT_DIR / "optimize_comparison.png"
    wscan_path = REPORT_DIR / "optimize_weight_scan.png"
    plot_comparison(equity_dict, spy_eq, qqq_eq, cmp_path)
    plot_weight_scan(scan_w, wscan_path)

    # ── HTML 报告 ─────────────────────────────────────────────────────────────
    spy_ret_s = spy_close.pct_change().reindex(close.index).fillna(0)
    qqq_ret_s = qqq_close.pct_change().reindex(close.index).fillna(0)
    main_stats += [
        perf_stats_raw((1 + spy_ret_s).cumprod(), spy_ret_s, "SPY"),
        perf_stats_raw((1 + qqq_ret_s).cumprod(), qqq_ret_s, "QQQ"),
    ]

    html_path = REPORT_DIR / "combo_optimize_report.html"
    generate_report(
        main_stats, scan_w, scan_t, scan_n, holdout_stats,
        cmp_path, wscan_path, html_path,
    )
    print(f"\n全部输出 → {REPORT_DIR}/")
    print(f"  用浏览器打开: open {html_path}")


if __name__ == "__main__":
    main()
