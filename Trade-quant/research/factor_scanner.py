"""
因子有效性扫描（IC Analysis）+ HTML 报告。

因子列表（第一批）：
  Ret_5      — 5日时序动量（短期）
  Ret_20     — 20日时序动量（月度，学术主流动量窗口）
  BIAS_20    — 20日乖离率（均值回归，预期负向 IC）
  VPT_slope  — VPT 5日斜率（量价趋势）
  HV_ratio   — 短期/长期历史波动率比（异动预警）
  LR_Slope   — 20日线性回归斜率 × R²（平滑动量，过滤单日暴涨噪音）
  RS_Beta    — Beta 调整后残差动量（剔除大盘 Beta，真正的个股独立强度）

市场环境分期（基于实际 SPY 数据）：
  熊市(2022)    2022-01-04 ~ 2022-10-12  SPY -23.8%，全程在MA200下方
  反弹(22Q4)    2022-10-13 ~ 2022-12-31  熊市反弹，不确定性高
  AI牛市(23-24) 2023-01-01 ~ 2024-12-31  两年强牛 +26%/+25%，低波动
  关税震荡(25H1) 2025-01-01 ~ 2025-06-05  高点回调-17%，关税冲击
  复苏(25H2+)   2025-06-06 ~ 今           从低点强力反弹，持续新高

输出（data/ 目录）：
  factor_ic_heatmap.png   IC均值热力图
  factor_ic_series.png    各因子IC时序图（fwd_5d）
  factor_ic_summary.csv   完整统计表
  factor_report.html      交互式HTML报告（含全部图表和分析）

用法：
  python factor_scanner.py
  python factor_scanner.py --since 2021-01-01
  python factor_scanner.py --min-stocks 30
"""

import argparse
import base64
import json
from datetime import datetime
from pathlib import Path

import matplotlib
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

# ── 中文字体 ──────────────────────────────────────────────────────────────────
def _setup_font():
    candidates = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "Noto Sans CJK SC"]
    available = {f.name for f in fm.fontManager.ttflist}
    for font in candidates:
        if font in available:
            matplotlib.rcParams["font.family"] = font
            break
    matplotlib.rcParams["axes.unicode_minus"] = False

_setup_font()

DATA_DIR    = Path(__file__).parent.parent / "data"
UNIVERSE_PATH = DATA_DIR / "universe.json"

HORIZONS    = [1, 3, 5, 10, 20]   # 预测周期（交易日）
MIN_STOCKS  = 20                   # 每日最少有效股票数
MIN_HISTORY = 60                   # 单只股票最少行数

# ── 市场环境分期（基于真实 SPY 数据核验）────────────────────────────────────
# 判据：SPY 最大回撤、相对 MA200 位置、季度涨跌幅
REGIMES = {
    "熊市(2022)":     ("2022-01-04", "2022-10-12"),   # SPY -23.8%，全程在MA200以下
    "反弹(22Q4)":     ("2022-10-13", "2022-12-31"),   # 熊市反弹，高波动
    "AI牛市(23-24)":  ("2023-01-01", "2024-12-31"),   # 两年强牛，年化低波动
    "关税震荡(25H1)": ("2025-01-01", "2025-06-05"),   # 关税冲击，SPY -17%
    "复苏(25H2+)":    ("2025-06-06", "2099-12-31"),   # 反弹创新高
}


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def load_universe():
    with open(UNIVERSE_PATH, encoding="utf-8") as f:
        u = json.load(f)
    return u["symbols"], u["benchmarks"]


def _strip_tz(idx):
    if hasattr(idx, "tz") and idx.tz is not None:
        return idx.tz_localize(None)
    return idx


def load_panel(symbols, tf="1d", since=None, include_open=False):
    closes, highs, lows, vols, opens = {}, {}, {}, {}, {}
    missing = []
    cols = ["open", "high", "low", "close", "volume"] if include_open else ["high", "low", "close", "volume"]
    for sym in symbols:
        path = DATA_DIR / f"{sym.lower()}_{tf}_raw.parquet"
        if not path.exists():
            missing.append(sym)
            continue
        df = pd.read_parquet(path, columns=cols)
        df.index = _strip_tz(pd.to_datetime(df.index))
        if since:
            df = df.loc[since:]
        if len(df) < MIN_HISTORY:
            continue
        closes[sym] = df["close"]
        highs[sym]  = df["high"]
        lows[sym]   = df["low"]
        vols[sym]   = df["volume"]
        if include_open:
            opens[sym] = df["open"]
    if missing:
        print(f"  [{len(missing)} 只缺数据，跳过]")
    idx = pd.DataFrame(closes).sort_index().index
    panels = (pd.DataFrame(closes).reindex(idx),
              pd.DataFrame(highs).reindex(idx),
              pd.DataFrame(lows).reindex(idx),
              pd.DataFrame(vols).reindex(idx))
    if include_open:
        return panels + (pd.DataFrame(opens).reindex(idx),)
    return panels


def load_benchmark(ticker, tf="1d", since=None):
    path = DATA_DIR / f"{ticker.lower()}_{tf}_raw.parquet"
    df = pd.read_parquet(path, columns=["close"])
    df.index = _strip_tz(pd.to_datetime(df.index))
    if since:
        df = df.loc[since:]
    return df["close"]


# ── 流动性掩码 ────────────────────────────────────────────────────────────────

def build_liquidity_mask(close, vol, min_dollar_vol=5_000_000, min_price=2.0, window=20):
    dollar_vol_ma = (close * vol).rolling(window).mean()
    return (dollar_vol_ma >= min_dollar_vol) & (close >= min_price)


# ── 因子计算 ──────────────────────────────────────────────────────────────────

def _rolling_lr_slope_r2(price_df, window=20):
    """
    滚动线性回归斜率 × R²，按当前价格归一化。

    每根K线：对最近 window 天价格拟合直线，
      slope / price 近似"每天涨多少百分比"，× R² 惩罚杂乱走势。
    趋势越平滑、R² 越高 → 因子值越强；单日暴涨后 R² 会很低，自动过滤。
    """
    arr  = price_df.values.astype(float)   # (T, N)
    T, N = arr.shape
    x    = np.arange(window, dtype=float)
    x   -= x.mean()                         # 均值中心化
    x_ss = (x ** 2).sum()                  # Σ(xi - x̄)²

    out = np.full((T, N), np.nan)
    for i in range(window - 1, T):
        y        = arr[i - window + 1 : i + 1]      # (window, N)
        nan_col  = np.isnan(y).any(axis=0)           # 有 NaN 的列跳过
        y_mean   = np.nanmean(y, axis=0)             # (N,)
        y_c      = y - y_mean
        slope    = (x @ y_c) / x_ss                  # (N,) OLS 斜率
        y_hat    = y_mean + x[:, None] * slope        # (window, N)
        ss_res   = ((y - y_hat) ** 2).sum(axis=0)
        ss_tot   = (y_c        ** 2).sum(axis=0)
        r2       = np.clip(
            np.where(ss_tot > 0, 1 - ss_res / ss_tot, 0.0), 0, 1
        )
        cur_p    = arr[i]
        out[i]   = np.where(
            nan_col | (cur_p == 0) | np.isnan(cur_p),
            np.nan,
            slope / cur_p * r2,
        )
    return pd.DataFrame(out, index=price_df.index, columns=price_df.columns)


def compute_factors(close, high, low, vol, qqq_close):
    factors = {}

    # 时序动量：短期（5日）& 月度（20日）
    factors["Ret_5"]  = close.pct_change(5)
    factors["Ret_20"] = close.pct_change(20)

    # 乖离率：偏离20日均线（预期负向 IC，均值回归）
    ma20 = close.rolling(20).mean()
    factors["BIAS_20"] = (close - ma20) / ma20

    # VPT 5日斜率：量价趋势因子，捕捉机构吸筹/出货
    vpt       = (vol * close.pct_change()).cumsum()
    vpt_scale = vpt.abs().rolling(20).mean().replace(0, np.nan)
    factors["VPT_slope"] = vpt.diff(5) / vpt_scale

    # HV 比率：短期/长期历史波动率，近期异动程度
    log_ret = np.log(close / close.shift(1))
    hv5  = log_ret.rolling(5).std()
    hv20 = log_ret.rolling(20).std().replace(0, np.nan)
    factors["HV_ratio"] = hv5 / hv20

    # 线性回归动量：20日斜率 × R²，过滤单日暴涨噪音
    factors["LR_Slope"] = _rolling_lr_slope_r2(close, window=20)

    # 布林带 %B：股价在布林带中的相对位置（超买/超卖）
    std20 = close.rolling(20).std()
    bb_upper = ma20 + 2 * std20
    bb_lower = ma20 - 2 * std20
    factors["BB_pct"] = (close - bb_lower) / (bb_upper - bb_lower).replace(0, np.nan)

    # 成交量冲击：量比 × 价格方向（暴量上涨 vs 暴量下跌）
    vol_ma20 = vol.rolling(20).mean().replace(0, np.nan)
    vol_ratio = vol / vol_ma20
    factors["Vol_Shock"] = vol_ratio * np.sign(close.pct_change())

    # MFI（资金流量指数，14日）：综合价量的超买超卖指标
    tp = (high + low + close) / 3          # 典型价格
    rmf = tp * vol                          # 原始资金流
    tp_chg = tp.diff()
    pos_mf = rmf.where(tp_chg > 0, 0.0)
    neg_mf = rmf.where(tp_chg < 0, 0.0)
    pos_sum = pos_mf.rolling(14).sum()
    neg_sum = neg_mf.rolling(14).sum().replace(0, np.nan)
    factors["MFI_14"] = 100 - 100 / (1 + pos_sum / neg_sum)

    # Beta 调整残差动量：剔除大盘 Beta 后的个股独立强度
    # Beta = rolling 60日 cov(stock, QQQ) / var(QQQ)
    # RS_Beta = stock_ret20 - Beta * qqq_ret20（每只股票减去不同值，排名才有意义）
    daily_ret  = close.pct_change()
    qqq_daily  = qqq_close.pct_change()
    qqq_var60  = qqq_daily.rolling(60).var()
    rolling_cov = daily_ret.apply(lambda col: col.rolling(60).cov(qqq_daily))
    beta60     = rolling_cov.div(qqq_var60, axis=0)
    qqq_ret20  = qqq_close.pct_change(20)
    factors["RS_Beta"] = close.pct_change(20).sub(
        beta60.mul(qqq_ret20, axis=0)
    )

    return factors


def compute_forward_returns(close):
    return {h: close.shift(-h) / close - 1 for h in HORIZONS}


# ── IC 计算 ───────────────────────────────────────────────────────────────────

def compute_ic_series(factor_panel, fwd_panel, liquid_mask, min_stocks=MIN_STOCKS):
    """每日截面 Spearman IC 时序。Spearman 基于排名，天然鲁棒，无需截尾。"""
    common_dates = (factor_panel.index
                    .intersection(fwd_panel.index)
                    .intersection(liquid_mask.index))
    f_p = factor_panel.reindex(common_dates)
    r_p = fwd_panel.reindex(common_dates)
    lm  = liquid_mask.reindex(common_dates)

    ic_vals, ic_idx = [], []
    for date in common_dates:
        lm_row = lm.loc[date].reindex(f_p.columns).fillna(False)
        f_row  = f_p.loc[date].where(lm_row).dropna()
        r_row  = r_p.loc[date].dropna()
        syms   = f_row.index.intersection(r_row.index)
        if len(syms) < min_stocks:
            continue
        ic, _ = spearmanr(f_row[syms], r_row[syms])
        if not np.isnan(ic):
            ic_vals.append(ic)
            ic_idx.append(date)

    return pd.Series(ic_vals, index=ic_idx, name="IC")


def ic_stats(ic_series):
    n = len(ic_series)
    if n < 10:
        return dict(IC_mean=np.nan, IC_std=np.nan, ICIR=np.nan, t_stat=np.nan, n_days=n)
    mean = ic_series.mean()
    std  = ic_series.std()
    icir   = mean / std            if std > 0 else np.nan
    t_stat = mean / (std / n**0.5) if std > 0 else np.nan
    return dict(IC_mean=mean, IC_std=std, ICIR=icir, t_stat=t_stat, n_days=n)


# ── 宏观状态开关 ──────────────────────────────────────────────────────────────

def compute_spy_regime(spy_close, ma_window=200, buffer=0.01):
    """
    基于 SPY 收盘价 vs MA{ma_window} 确定宏观市场状态。

    buffer=1%：均线±1% 内视为中性，避免均线附近频繁切换。
    返回 Series:  1 = 牛市（SPY > MA × 1.01）
                 -1 = 熊市（SPY < MA × 0.99）
                  0 = 中性过渡区
    """
    ma = spy_close.rolling(ma_window).mean()
    regime = pd.Series(0, index=spy_close.index, name="regime")
    regime[spy_close > ma * (1 + buffer)] = 1
    regime[spy_close < ma * (1 - buffer)] = -1
    return regime, ma


def compute_conditional_ic(ic_series_all, factor_names, spy_regime, horizon=5):
    """按宏观状态（牛市/熊市）分别计算 IC 统计，用于制定因子权重开关策略。"""
    bull_dates = spy_regime[spy_regime == 1].index
    bear_dates = spy_regime[spy_regime == -1].index
    cond_results = {}
    for fname in factor_names:
        ic = ic_series_all.get((fname, horizon), pd.Series(dtype=float))
        for label, dates in [("牛市", bull_dates), ("熊市", bear_dates)]:
            subset = ic[ic.index.isin(dates)].dropna()
            cond_results[(fname, label)] = ic_stats(subset)
    return cond_results, len(bull_dates), len(bear_dates)


# ── 主扫描 ────────────────────────────────────────────────────────────────────

def run_scan(factors, fwd_returns, liquid_mask, min_stocks=MIN_STOCKS):
    results, ic_series_all = {}, {}
    total = len(factors) * len(HORIZONS)
    i = 0
    for fname, fpanel in factors.items():
        for h in HORIZONS:
            i += 1
            print(f"  [{i:2d}/{total}] {fname:<12} fwd_{h:2d}d ...", end=" ", flush=True)
            ic    = compute_ic_series(fpanel, fwd_returns[h], liquid_mask, min_stocks)
            stats = ic_stats(ic)
            results[(fname, h)]       = stats
            ic_series_all[(fname, h)] = ic
            ic_v = stats["IC_mean"]
            ir_v = stats["ICIR"]
            print(f"IC={ic_v:+.4f}  ICIR={ir_v:+.3f}  n={stats['n_days']}"
                  if not np.isnan(ic_v) else "N/A")
    return results, ic_series_all


def compute_regime_stats(ic_series_all, factor_names):
    """各市场环境下的分段 IC 统计（fwd_5d 和 fwd_10d）。"""
    out = {}
    for rname, (start, end) in REGIMES.items():
        for fname in factor_names:
            for h in [5, 10]:
                ic = ic_series_all.get((fname, h), pd.Series(dtype=float))
                seg = ic.loc[start:end]
                out[(rname, fname, h)] = ic_stats(seg)
    return out


def compute_yearly_stats(ic_series_all, factor_names, horizon=5):
    """分年度 IC 统计（fwd_{horizon}d）。"""
    out = {}
    years = set()
    for fname in factor_names:
        ic = ic_series_all.get((fname, horizon), pd.Series(dtype=float))
        for yr in ic.index.year.unique():
            years.add(yr)
            out[(fname, yr)] = ic_stats(ic[ic.index.year == yr])
    return out, sorted(years)


# ── 可视化 ────────────────────────────────────────────────────────────────────

_DARK_BG   = "#0d1117"   # 图表背景
_DARK_AX   = "#161b22"   # axes 底色
_GRID_CLR  = "#30363d"   # 网格线
_TEXT_CLR  = "#e6edf3"   # 标题/刻度文字
_POS_CLR   = "#39d353"   # 正向柱 / 强正向（亮绿）
_NEG_CLR   = "#f85149"   # 负向柱 / 强负向（亮红）
_CUM_CLR   = "#58a6ff"   # 累计IC折线（亮蓝）
_MEAN_CLR  = "#ffd60a"   # 均值虚线（黄）


def _dark_fig(nrows, figsize):
    fig, axes = plt.subplots(nrows, 1, figsize=figsize, sharex=(nrows > 1))
    fig.patch.set_facecolor(_DARK_BG)
    if nrows == 1:
        axes = [axes]
    for ax in axes:
        ax.set_facecolor(_DARK_AX)
        ax.tick_params(colors=_TEXT_CLR, labelsize=8)
        for spine in ax.spines.values():
            spine.set_edgecolor(_GRID_CLR)
    return fig, axes


def plot_heatmap(results, factor_names, out_path):
    horizons  = HORIZONS
    ic_grid   = np.array([[results[(f, h)]["IC_mean"] for h in horizons] for f in factor_names])
    icir_grid = np.array([[results[(f, h)]["ICIR"]    for h in horizons] for f in factor_names])

    fig, (ax,) = _dark_fig(1, (11, max(4, len(factor_names) * 1.0)))
    vmax = max(0.06, np.nanmax(np.abs(ic_grid)))

    # 自定义深色友好的 RdYlGn（避免中间黄色在深色底不清晰）
    from matplotlib.colors import LinearSegmentedColormap
    dark_rwg = LinearSegmentedColormap.from_list(
        "dark_rwg",
        ["#f85149", "#30363d", "#39d353"],  # 红 → 暗灰 → 亮绿
    )
    im = ax.imshow(ic_grid, cmap=dark_rwg, vmin=-vmax, vmax=vmax, aspect="auto")

    horizon_labels = {1: "持仓1天", 3: "持仓3天", 5: "持仓5天\n(约1周)",
                      10: "持仓10天\n(约2周)", 20: "持仓20天\n(约1月)"}
    ax.set_xticks(range(len(horizons)))
    ax.set_xticklabels([horizon_labels.get(h, f"fwd_{h}d") for h in horizons],
                       fontsize=9, color=_TEXT_CLR)
    ax.set_yticks(range(len(factor_names)))
    ax.set_yticklabels(factor_names, fontsize=11, color=_TEXT_CLR)

    for i, fname in enumerate(factor_names):
        for j, h in enumerate(horizons):
            ic_v = ic_grid[i, j]
            ir_v = icir_grid[i, j]
            label = "N/A" if np.isnan(ic_v) else (
                f"{ic_v:+.3f}{'★' if not np.isnan(ir_v) and abs(ir_v) >= 0.5 else ''}"
            )
            ax.text(j, i, label, ha="center", va="center",
                    fontsize=10, color=_TEXT_CLR, fontweight="bold")

    cb = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cb.set_label("IC 均值", color=_TEXT_CLR)
    cb.ax.yaxis.set_tick_params(color=_TEXT_CLR)
    plt.setp(cb.ax.yaxis.get_ticklabels(), color=_TEXT_CLR)

    ax.set_title(
        "全局 IC 均值热力图    ★ = |ICIR| ≥ 0.5（稳定有效）\n"
        "绿=正向(动量)  红=负向(反转)",
        fontsize=10, pad=10, color=_TEXT_CLR,
    )
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=_DARK_BG)
    plt.close()


def plot_ic_series(ic_series_all, factor_names, out_path, horizon=5):
    n = len(factor_names)
    fig, axes = _dark_fig(n, (14, 3.5 * n))

    # 深色主题下的市场分期背景色（半透明覆盖层）
    regime_colors = {
        "熊市(2022)":     "#8b0000",  # 暗红
        "反弹(22Q4)":     "#7a4f00",  # 暗橙
        "AI牛市(23-24)":  "#004d00",  # 暗绿
        "关税震荡(25H1)": "#7a4f00",  # 暗橙
        "复苏(25H2+)":    "#003d66",  # 暗蓝
    }

    for ax, fname in zip(axes, factor_names):
        ic = ic_series_all.get((fname, horizon), pd.Series(dtype=float))
        if ic.empty:
            ax.set_title(f"{fname}（无数据）", color=_TEXT_CLR)
            continue

        x_min, x_max = ic.index.min(), ic.index.max()
        for rname, (rs, re) in REGIMES.items():
            rs_ts = max(pd.Timestamp(rs), x_min)
            re_ts = min(pd.Timestamp(re), x_max)
            if rs_ts >= re_ts:
                continue
            ax.axvspan(rs_ts, re_ts,
                       alpha=0.25, color=regime_colors.get(rname, "#333"), label=None)

        mean_v = ic.mean()
        std_v  = ic.std()
        icir_v = mean_v / std_v if std_v > 0 else 0
        cumulative = ic.cumsum()

        bar_clr = _POS_CLR if mean_v >= 0 else _NEG_CLR
        # 每根柱按正负上色
        colors = [_POS_CLR if v >= 0 else _NEG_CLR for v in ic]
        ax.bar(ic.index, ic, color=colors, alpha=0.55, width=2)
        ax.axhline(0,      color=_GRID_CLR, lw=0.8)
        ax.axhline(mean_v, color=_MEAN_CLR, lw=1.2, ls="--",
                   label=f"均值 {mean_v:+.4f}")

        ax2 = ax.twinx()
        ax2.plot(ic.index, cumulative, color=_CUM_CLR, lw=1.5, label="累计IC")
        ax2.set_facecolor(_DARK_AX)
        ax2.tick_params(colors=_TEXT_CLR, labelsize=7)
        ax2.set_ylabel("累计IC", fontsize=8, color=_CUM_CLR)
        for spine in ax2.spines.values():
            spine.set_edgecolor(_GRID_CLR)

        ax.set_title(
            f"{fname}  →  持仓{horizon}天    "
            f"IC均值={mean_v:+.4f}  ICIR={icir_v:+.3f}  n={len(ic)}天",
            fontsize=10, color=_TEXT_CLR,
        )
        ax.set_ylabel("IC", color=_TEXT_CLR)
        ax.legend(loc="upper left", fontsize=7, framealpha=0.4,
                  facecolor=_DARK_AX, labelcolor=_TEXT_CLR, edgecolor=_GRID_CLR)
        ax.grid(alpha=0.15, color=_GRID_CLR)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right",
                 fontsize=7, color=_TEXT_CLR)

    plt.suptitle(
        f"各因子 IC 时序（持仓{horizon}天）\n"
        "背景色：暗红=熊市  暗橙=震荡  暗绿=牛市  暗蓝=复苏",
        fontsize=11, y=1.002, color=_TEXT_CLR,
    )
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor=_DARK_BG)
    plt.close()


# ── HTML 报告 ─────────────────────────────────────────────────────────────────

def _ic_color(ic, icir=None, alpha=True):
    """根据 IC 值和 ICIR 返回单元格背景色。"""
    if np.isnan(ic):
        return "#f5f5f5"
    effective = icir is not None and not np.isnan(icir) and abs(icir) >= 0.5
    strong    = abs(ic) > 0.03
    if strong and effective:
        return "#4caf50" if ic > 0 else "#f44336"   # 深绿 / 深红
    if strong:
        return "#a5d6a7" if ic > 0 else "#ef9a9a"   # 浅绿 / 浅红
    if abs(ic) > 0.01:
        return "#fffde7"                             # 浅黄（有微弱信号）
    return "#f5f5f5"                                 # 灰（噪音）


def _ic_text(ic, icir=None):
    if np.isnan(ic):
        return "N/A"
    star = " ★" if icir is not None and not np.isnan(icir) and abs(icir) >= 0.5 else ""
    return f"{ic:+.4f}{star}"


def _img_b64(path):
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


def generate_html_report(
    results, ic_series_all, regime_stats, yearly_stats, years,
    factor_names, since, n_stocks, liquid_pct,
    heatmap_path, series_path, out_path,
    cond_ic=None, n_bull=0, n_bear=0,
    spy_last=None, ma200_last=None, current_regime=0,
):
    heatmap_b64 = _img_b64(heatmap_path)
    series_b64  = _img_b64(series_path)
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 持仓周期的中文说明
    horizon_label = {1: "1天后", 3: "3天后", 5: "5天后\n(约1周)",
                     10: "10天后\n(约2周)", 20: "20天后\n(约1月)"}

    css = """
    * { box-sizing: border-box; }
    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           margin: 0; background: #f0f2f5; color: #212529; }
    /* 外层撑满可用宽度（减去右侧固定面板，用 vw 跟随视口） */
    .content-wrap { margin-right: 30vw; }
    /* 主内容区：限制最大宽度并居中，在大屏上不会撑满 */
    .main { max-width: 1150px; margin: 0 auto;
            padding: 28px 32px 140px; /* 底部 140px 避免浮窗遮住最后一张图 */ }
    /* 左下角小浮窗：颜色速查 */
    .float-legend { position: fixed; left: 18px; bottom: 18px; z-index: 999;
                    background: rgba(13,17,23,0.92); color: #e6edf3;
                    border: 1px solid #30363d; border-radius: 10px;
                    padding: 12px 14px; width: 186px;
                    font-size: 0.79em; line-height: 1.55;
                    box-shadow: 0 4px 18px rgba(0,0,0,.5); }
    .fl-title { font-weight: 700; font-size: 0.92em; color: #fff;
                border-bottom: 1px solid #30363d;
                padding-bottom: 7px; margin-bottom: 9px; }
    .fl-row { display: flex; align-items: center; gap: 8px; margin: 5px 0; }
    .fl-dot { width: 13px; height: 13px; border-radius: 3px; flex-shrink: 0; }
    .fl-star { color: #ffa726; }
    .fl-note { margin-top: 9px; border-top: 1px solid #30363d;
               padding-top: 8px; font-size: 0.88em; color: #8b949e; }
    /* 右侧固定面板：解读指南 */
    .right-panel { position: fixed; right: 0; top: 0; bottom: 0;
                   width: 30vw; min-width: 280px; max-width: 560px;
                   background: #fff; border-left: 1px solid #dde;
                   padding: 18px 14px; overflow-y: auto; z-index: 100;
                   font-size: 0.81em; }
    .right-panel h3 { color: #1a1a2e; font-size: 0.95em; margin-top: 0;
                      border-bottom: 2px solid #4361ee; padding-bottom: 6px; }
    .right-panel h4 { color: #4361ee; font-size: 0.85em; margin: 14px 0 5px; }
    /* 标题 */
    h1 { color: #1a1a2e; border-bottom: 3px solid #4361ee;
         padding-bottom: 10px; margin-top: 0; font-size: 1.4em; }
    h2 { color: #1a1a2e; margin-top: 40px; font-size: 1.05em;
         border-left: 4px solid #4361ee; padding-left: 12px; }
    /* 顶部元信息 */
    .meta { background: #fff; border-radius: 10px; padding: 14px 18px;
            display: flex; flex-wrap: wrap; gap: 18px;
            box-shadow: 0 1px 4px rgba(0,0,0,.08); margin-bottom: 6px; }
    .meta-item { text-align: center; min-width: 80px; }
    .meta-item .val { font-size: 1.35em; font-weight: 700; color: #4361ee; }
    .meta-item .lbl { font-size: 0.76em; color: #888; margin-top: 2px; }
    /* 表格 */
    table { border-collapse: collapse; width: 100%; margin: 10px 0;
            background: #fff; border-radius: 10px; overflow: hidden;
            box-shadow: 0 1px 4px rgba(0,0,0,.08); font-size: 0.87em; }
    th { background: #1a1a2e; color: #fff; padding: 8px 10px;
         text-align: center; white-space: pre-line; line-height: 1.4; }
    td { padding: 7px 10px; text-align: center; border-bottom: 1px solid #f0f0f0; }
    tr:last-child td { border-bottom: none; }
    td.fn { text-align: left; font-weight: 600; font-family: monospace;
            white-space: nowrap; }
    /* 图片 */
    img { max-width: 100%; border-radius: 10px;
          box-shadow: 0 2px 10px rgba(0,0,0,.12); margin: 6px 0; }
    /* 备注框 */
    .note { background: #e8f4fd; border-left: 4px solid #4361ee;
            padding: 10px 14px; border-radius: 0 8px 8px 0;
            margin: 10px 0; font-size: 0.87em; line-height: 1.7; }
    .note b { color: #1a1a2e; }
    .sub { font-size: 0.79em; color: #777; margin: 3px 0 10px; }
    /* 右侧面板术语 */
    .term { margin: 5px 0; font-size: 0.88em; line-height: 1.5; }
    .term b { color: #1a1a2e; display: block; margin-bottom: 1px; }
    """

    # ── 表格生成函数 ──────────────────────────────────────────────────────────
    def global_table():
        cols = "".join(
            f'<th>{horizon_label.get(h, f"fwd_{h}d")}</th>' for h in HORIZONS
        )
        rows = ""
        for fname in factor_names:
            row = f'<td class="fn">{fname}</td>'
            for h in HORIZONS:
                s = results[(fname, h)]
                ic, ir = s["IC_mean"], s["ICIR"]
                row += f'<td style="background:{_ic_color(ic,ir)}">{_ic_text(ic,ir)}</td>'
            rows += f"<tr>{row}</tr>"
        return f'<table><thead><tr><th>因子</th>{cols}</tr></thead><tbody>{rows}</tbody></table>'

    def regime_table(h=5):
        rnames = list(REGIMES.keys())
        cols = "".join(f"<th>{r}</th>" for r in rnames)
        rows = ""
        for fname in factor_names:
            row = f'<td class="fn">{fname}</td>'
            for rname in rnames:
                s = regime_stats.get((rname, fname, h), {})
                ic, ir = s.get("IC_mean", np.nan), s.get("ICIR", np.nan)
                n = s.get("n_days", 0)
                row += (f'<td style="background:{_ic_color(ic,ir)}" title="样本天数={n}">'
                        f'{_ic_text(ic,ir)}</td>')
            rows += f"<tr>{row}</tr>"
        return f'<table><thead><tr><th>因子</th>{cols}</tr></thead><tbody>{rows}</tbody></table>'

    def yearly_table():
        cols = "".join(f"<th>{yr}年</th>" for yr in years)
        rows = ""
        for fname in factor_names:
            row = f'<td class="fn">{fname}</td>'
            for yr in years:
                s = yearly_stats.get((fname, yr), {})
                ic, ir = s.get("IC_mean", np.nan), s.get("ICIR", np.nan)
                row += f'<td style="background:{_ic_color(ic,ir)}">{_ic_text(ic,ir)}</td>'
            rows += f"<tr>{row}</tr>"
        return f'<table><thead><tr><th>因子</th>{cols}</tr></thead><tbody>{rows}</tbody></table>'

    # ── 左下角小浮窗：颜色速查 ──────────────────────────────────────────────
    left_panel = """
    <div class="float-legend">
      <div class="fl-title">🎨 颜色含义速查</div>
      <div class="fl-row"><div class="fl-dot" style="background:#4caf50"></div>强有效·正向（动量）</div>
      <div class="fl-row"><div class="fl-dot" style="background:#f44336"></div>强有效·负向（反转）</div>
      <div class="fl-row"><div class="fl-dot" style="background:#a5d6a7"></div>弱有效·正向</div>
      <div class="fl-row"><div class="fl-dot" style="background:#ef9a9a"></div>弱有效·负向</div>
      <div class="fl-row"><div class="fl-dot" style="background:#fffde7;border:1px solid #555"></div>微弱信号</div>
      <div class="fl-row"><div class="fl-dot" style="background:#e0e0e0"></div>噪音·无意义</div>
      <div class="fl-note">
        <span class="fl-star">★</span> = ICIR ≥ 0.5（稳定有效）<br>
        标准：|IC| &gt; 0.03 且 |ICIR| &gt; 0.5
      </div>
    </div>
    """

    # ── 右侧固定面板：解读指南 ───────────────────────────────────────────────
    right_panel = """
    <div class="right-panel">
      <h3>📖 评判标准与解读</h3>

      <h4>有效因子双重标准</h4>
      <div class="term">
        <b>|IC 均值| &gt; 0.03</b>
        因子对未来涨跌有预测能力。IC=0 完全随机，>0.03 才有统计意义。
      </div>
      <div class="term">
        <b>|ICIR| &gt; 0.5</b>
        因子表现稳定，不靠偶发暴涨拉高均值。ICIR = IC均值 ÷ IC标准差，类似夏普比率。
      </div>

      <h4>术语速查</h4>
      <div class="term">
        <b>IC（信息系数）</b>
        当天所有股票「因子值排名」vs「未来涨跌排名」的相关系数。+1=完美预测，-1=完美反向，0=无效。
      </div>
      <div class="term">
        <b>ICIR（IC信息比率）</b>
        IC的稳定性得分。越高说明因子越可靠，非靠运气。
      </div>
      <div class="term">
        <b>持仓X天</b>
        买入后持有X个交易日的累计涨跌幅。"持仓5天"= 买后约一周的收益率。
      </div>
      <div class="term">
        <b>正向因子</b>因子值越大→预期未来涨幅越大（动量逻辑）
      </div>
      <div class="term">
        <b>负向因子</b>因子值越大→预期未来跌幅越大（反转逻辑），反向使用同样有效
      </div>

      <h4>各因子简介</h4>
      <div class="term"><b>Ret_5</b>过去5日涨跌幅（短期动量）</div>
      <div class="term"><b>Ret_20</b>过去20日涨跌幅（月度动量）</div>
      <div class="term"><b>BIAS_20</b>股价偏离20日均线（乖离率，反转因子）</div>
      <div class="term"><b>VPT_slope</b>量价趋势5日斜率（机构吸筹/出货）</div>
      <div class="term"><b>HV_ratio</b>短期÷长期波动率（近期异动程度）</div>
      <div class="term"><b>LR_Slope</b>20日线性回归斜率×R²，过滤单日暴涨噪音，比 Ret_20 平滑</div>
      <div class="term"><b>RS_Beta</b>个股20日收益 − Beta×QQQ收益，剔除大盘解释后的真实Alpha强度</div>
      <div class="term"><b>BB_pct</b>布林带%B：股价在布林带中的相对位置，>1超买，&lt;0超卖（反转因子）</div>
      <div class="term"><b>Vol_Shock</b>量比×价格方向：暴量上涨=正，暴量下跌=负（量价共振因子）</div>
      <div class="term"><b>MFI_14</b>14日资金流量指数，综合价量，>80超买，&lt;20超卖（反转因子）</div>

    </div>
    """

    # ── 市场分期说明框 ────────────────────────────────────────────────────────
    regime_note = """
    <div class="note">
      <b>市场分期依据（基于实际 SPY 日线数据核验）</b><br>
      🔴 <b>熊市(2022)</b> 01-04 ~ 10-12：SPY 446→340（-23.8%），全程在 MA200 以下，年化波动 23.8%<br>
      🟠 <b>反弹(22Q4)</b> 10-13 ~ 12-31：熊末反弹，高不确定性<br>
      🟢 <b>AI牛市(23-24)</b> 2023-01-01 ~ 2024-12-31：两年强牛 +26%/+25%，MA200 上方，低波动<br>
      🟠 <b>关税震荡(25H1)</b> 2025-01-01 ~ 06-05：关税冲击 SPY 601→497（-17.2%），4月8日最低<br>
      🔵 <b>复苏(25H2+)</b> 2025-06-06 ~ 今：强力反弹，持续创新高
    </div>
    """

    # ── 宏观状态开关：条件 IC 表 + 当前状态 ─────────────────────────────────
    def regime_switch_section():
        if cond_ic is None:
            return ""
        regime_label = {1: "🟢 牛市", -1: "🔴 熊市", 0: "🟡 中性过渡"}.get(current_regime, "未知")
        spy_vs_ma = ((spy_last / ma200_last - 1) * 100) if ma200_last else 0
        vs_str    = f"{spy_vs_ma:+.1f}%"
        badge_clr = "#39d353" if current_regime == 1 else ("#f85149" if current_regime == -1 else "#ffd60a")

        # 推荐权重逻辑（简单启发式）
        def recommend(fname, bull_ic, bear_ic):
            if current_regime == 1:
                ic = bull_ic
            elif current_regime == -1:
                ic = bear_ic
            else:
                ic = (bull_ic + bear_ic) / 2 if not (np.isnan(bull_ic) or np.isnan(bear_ic)) else np.nan
            if np.isnan(ic) or abs(ic) < 0.015:
                return '<span style="color:#666">暂不使用</span>'
            return ('<span style="color:#39d353">↑ 正向加权</span>' if ic > 0
                    else '<span style="color:#f85149">↓ 反向加权</span>')

        rows = ""
        for fname in factor_names:
            bs = cond_ic.get((fname, "牛市"), {})
            br = cond_ic.get((fname, "熊市"), {})
            bull_ic = bs.get("IC_mean", np.nan)
            bear_ic = br.get("IC_mean", np.nan)
            bull_ir = bs.get("ICIR", np.nan)
            bear_ir = br.get("ICIR", np.nan)
            rec     = recommend(fname, bull_ic, bear_ic)
            rows += (
                f'<tr>'
                f'<td class="fn">{fname}</td>'
                f'<td style="background:{_ic_color(bull_ic,bull_ir)}">{_ic_text(bull_ic,bull_ir)}</td>'
                f'<td style="background:{_ic_color(bear_ic,bear_ir)}">{_ic_text(bear_ic,bear_ir)}</td>'
                f'<td>{rec}</td>'
                f'</tr>'
            )

        return f"""
  <h2>6. 宏观状态开关分析（SPY MA200 信号）</h2>
  <div class="note">
    <b>当前市场状态：</b>
    <span style="background:{badge_clr};color:#000;padding:2px 10px;border-radius:12px;
                 font-weight:700;font-size:1em">{regime_label}</span>
    &nbsp;&nbsp;SPY {spy_last:.2f} vs MA200 {ma200_last:.2f}（偏离 {vs_str}）<br><br>
    <b>信号逻辑：</b>SPY 收盘价 &gt; MA200×1.01 = 牛市，&lt; MA200×0.99 = 熊市，±1% 缓冲区避免频繁切换。<br>
    <b>使用方式：</b>牛市时对正向因子加权；熊市时切换至反转因子（BIAS_20 反向）或现金观望。
  </div>
  <p class="sub">牛市样本 {n_bull} 天 · 熊市样本 {n_bear} 天（基于 SPY MA200 信号）</p>
  <table>
    <thead>
      <tr>
        <th>因子</th>
        <th>牛市 IC<br><span style="font-weight:400;font-size:0.85em">(SPY &gt; MA200)</span></th>
        <th>熊市 IC<br><span style="font-weight:400;font-size:0.85em">(SPY &lt; MA200)</span></th>
        <th>当前建议</th>
      </tr>
    </thead>
    <tbody>{rows}</tbody>
  </table>"""

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>因子 IC 分析报告</title>
  <style>{css}</style>
</head>
<body>

<!-- ── 左侧固定面板：颜色速查 ── -->
{left_panel}

<!-- ── 右侧固定面板：解读指南 ── -->
{right_panel}

<!-- ── 主内容区（右侧留给固定面板，内容本身 max-width 居中） ── -->
<div class="content-wrap"><div class="main">
  <h1>📊 因子 IC 分析报告</h1>
  <div class="meta">
    <div class="meta-item"><div class="val">{n_stocks}</div><div class="lbl">有效股票</div></div>
    <div class="meta-item"><div class="val">{since}</div><div class="lbl">数据起始</div></div>
    <div class="meta-item"><div class="val">{liquid_pct:.1f}%</div><div class="lbl">流动性达标</div></div>
    <div class="meta-item"><div class="val">{len(factor_names)}</div><div class="lbl">测试因子</div></div>
    <div class="meta-item"><div class="val">{now}</div><div class="lbl">生成时间</div></div>
  </div>

  <h2>1. 全局 IC 均值热力图</h2>
  <p class="sub">横轴 = 买入后持仓多少天；纵轴 = 因子名称；数值越偏离 0 颜色越深</p>
  <img src="data:image/png;base64,{heatmap_b64}" alt="IC热力图">

  <h2>2. 全局 IC 汇总表（全时段平均）</h2>
  <p class="sub">全局均值接近 0 不代表因子无用，可能是牛熊年份相互抵消——看下方「分年度」才是真相</p>
  {global_table()}

  <h2>3. 分市场环境 IC（持仓5天）</h2>
  {regime_note}
  {regime_table(h=5)}

  <h2>4. 分年度 IC（持仓5天）</h2>
  <p class="sub">同一因子在不同年份表现差异巨大，说明它是「有条件有效」的因子，需配合市场状态开关</p>
  {yearly_table()}

  <h2>5. IC 时序图（各因子·持仓5天·含市场分期背景）</h2>
  <p class="sub">柱状图 = 每日IC；深色折线 = 累计IC（斜率向上=因子持续有效）；背景色 = 市场分期</p>
  <img src="data:image/png;base64,{series_b64}" alt="IC时序图">

  {regime_switch_section()}
</div></div>

</body>
</html>"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML报告 → {out_path.name}")


# ── 控制台打印 ────────────────────────────────────────────────────────────────

def print_summary(results, factor_names, yearly_stats, years, regime_stats):
    W = 72
    print("\n" + "=" * W)
    print("全局 IC 汇总（|IC均值|>0.03 且 |ICIR|>0.5 为有效因子）")
    print("=" * W)
    header = f"{'因子':<14}" + "".join(f" fwd_{h:2d}d" for h in HORIZONS)
    print(header)
    print("-" * W)
    for fname in factor_names:
        row = f"{fname:<14}"
        for h in HORIZONS:
            s  = results[(fname, h)]
            ic = s["IC_mean"]
            ir = s["ICIR"]
            if np.isnan(ic):
                row += "    N/A "
            else:
                star = "★" if not np.isnan(ir) and abs(ir) >= 0.5 else " "
                row += f"  {ic:+.3f}{star}"
        print(row)
    print("-" * W)

    print("\n分年度 IC（fwd_5d）：")
    print(f"{'因子':<14}" + "".join(f"  {yr}" for yr in years))
    print("-" * W)
    for fname in factor_names:
        row = f"{fname:<14}"
        for yr in years:
            s  = yearly_stats.get((fname, yr), {})
            ic = s.get("IC_mean", np.nan)
            row += f"  {ic:+.3f}" if not np.isnan(ic) else "   N/A"
        print(row)
    print("=" * W)


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="因子IC扫描器")
    parser.add_argument("--symbols",    nargs="+", default=None)
    parser.add_argument("--since",      default="2022-01-01")
    parser.add_argument("--tf",         default="1d")
    parser.add_argument("--min-stocks", type=int, default=MIN_STOCKS)
    parser.add_argument("--no-show",    action="store_true")
    args = parser.parse_args()

    # 加载数据
    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    else:
        symbols, _ = load_universe()

    print(f"加载 {len(symbols)} 只股票（since {args.since}）...")
    close, high, low, vol = load_panel(symbols, args.tf, since=args.since)
    print(f"  有效标的: {close.shape[1]} 只  "
          f"{close.index[0].date()} ~ {close.index[-1].date()}")

    qqq_close = load_benchmark("QQQ", args.tf, since=args.since).reindex(close.index)
    spy_close = load_benchmark("SPY", args.tf, since=args.since).reindex(close.index)

    liquid = build_liquidity_mask(close, vol)
    liquid_pct = liquid.stack().mean() * 100
    print(f"  流动性达标: {liquid_pct:.1f}%")

    print("计算因子...")
    factors     = compute_factors(close, high, low, vol, qqq_close)
    fwd_returns = compute_forward_returns(close)

    print(f"IC 扫描（{len(factors)} 因子 × {len(HORIZONS)} 预测周期）...")
    results, ic_series_all = run_scan(factors, fwd_returns, liquid, args.min_stocks)

    factor_names = list(factors.keys())

    # 分环境 & 分年度
    regime_stats = compute_regime_stats(ic_series_all, factor_names)
    yearly_stats, years = compute_yearly_stats(ic_series_all, factor_names, horizon=5)

    # 宏观状态开关
    spy_regime, spy_ma200 = compute_spy_regime(spy_close)
    cond_ic, n_bull, n_bear = compute_conditional_ic(
        ic_series_all, factor_names, spy_regime, horizon=5
    )
    spy_last   = spy_close.dropna().iloc[-1]
    ma200_last = spy_ma200.dropna().iloc[-1]
    current_regime = spy_regime.dropna().iloc[-1]

    print_summary(results, factor_names, yearly_stats, years, regime_stats)

    # 保存 CSV
    rows = [{"factor": f, "horizon": f"fwd_{h}d", **s}
            for (f, h), s in results.items()]
    pd.DataFrame(rows).to_csv(DATA_DIR / "factor_ic_summary.csv", index=False)

    # 图表
    heatmap_path = DATA_DIR / "factor_ic_heatmap.png"
    series_path  = DATA_DIR / "factor_ic_series.png"
    html_path    = DATA_DIR / "factor_report.html"

    plot_heatmap(results, factor_names, heatmap_path)
    plot_ic_series(ic_series_all, factor_names, series_path, horizon=5)

    generate_html_report(
        results, ic_series_all, regime_stats, yearly_stats, years,
        factor_names, args.since, close.shape[1], liquid_pct,
        heatmap_path, series_path, html_path,
        cond_ic=cond_ic, n_bull=n_bull, n_bear=n_bear,
        spy_last=spy_last, ma200_last=ma200_last, current_regime=int(current_regime),
    )

    print(f"\n全部输出已保存至 {DATA_DIR}/")
    print(f"  用浏览器打开: open {html_path}")


if __name__ == "__main__":
    main()
