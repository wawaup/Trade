"""
因子有效性扫描（IC Analysis）。

第一批 5 个候选因子：
  1. Ret_5      — 5日时序动量
  2. BIAS_20    — 20日乖离率（均值回归，预期负向）
  3. VPT_slope  — VPT 5日斜率（量价趋势）
  4. HV_ratio   — 短期/长期历史波动率比（异动信号）
  5. RS_QQQ     — 个股5日收益 - QQQ 5日收益（独立强度）

IC 计算方法：每日截面 Spearman 相关系数
  - 每天：对所有有效股票，计算 factor[t] 与 fwd_return[t+N] 的 Spearman IC
  - 有效条件：liquid=True（20日均成交额 ≥ 500万 且 价格 ≥ 2美元）
  - 每天需要至少 20 只有效股票才计算 IC

评判标准：
  |IC 均值| > 0.03  ——  因子有预测能力
  |ICIR|   > 0.5   ——  因子表现稳定（不靠偶发事件）

输出（保存至 data/）：
  factor_ic_heatmap.png  — IC 均值热力图（因子 × 预测周期）
  factor_ic_series.png   — 各因子 IC 时序图（5日预测周期）
  factor_ic_summary.csv  — 完整统计表

用法：
    python factor_scanner.py
    python factor_scanner.py --since 2022-01-01
    python factor_scanner.py --min-stocks 30
"""

import argparse
import json
from pathlib import Path

import matplotlib
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

# ── 中文字体配置 ──────────────────────────────────────────────────────────────
def _setup_font():
    candidates = ["PingFang SC", "Heiti SC", "Arial Unicode MS", "Noto Sans CJK SC"]
    available = {f.name for f in fm.fontManager.ttflist}
    for font in candidates:
        if font in available:
            matplotlib.rcParams["font.family"] = font
            break
    matplotlib.rcParams["axes.unicode_minus"] = False

_setup_font()

DATA_DIR = Path(__file__).parent.parent / "data"
UNIVERSE_PATH = DATA_DIR / "universe.json"

HORIZONS = [1, 3, 5, 10]   # 预测周期（交易日）
MIN_STOCKS = 20             # 每日最少有效股票数
MIN_HISTORY = 60            # 股票至少需要多少行有效数据


# ── 数据加载 ──────────────────────────────────────────────────────────────────

def load_universe():
    with open(UNIVERSE_PATH, encoding="utf-8") as f:
        u = json.load(f)
    return u["symbols"], u["benchmarks"]


def _strip_tz(idx):
    """去掉时区信息，统一为 naive datetime。"""
    if hasattr(idx, "tz") and idx.tz is not None:
        return idx.tz_localize(None)
    return idx


def load_panel(symbols, tf="1d", since=None):
    """
    加载所有股票的收盘价和成交量面板。
    返回 close (date×symbol) 和 vol (date×symbol)。
    """
    closes, vols = {}, {}
    missing = []

    for sym in symbols:
        path = DATA_DIR / f"{sym.lower()}_{tf}_raw.parquet"
        if not path.exists():
            missing.append(sym)
            continue
        df = pd.read_parquet(path, columns=["close", "volume"])
        df.index = _strip_tz(pd.to_datetime(df.index))
        if since:
            df = df.loc[since:]
        if len(df) < MIN_HISTORY:
            continue
        closes[sym] = df["close"]
        vols[sym]   = df["volume"]

    if missing:
        print(f"  [{len(missing)} 只缺数据文件，跳过]")

    close = pd.DataFrame(closes).sort_index()
    vol   = pd.DataFrame(vols).sort_index()
    return close, vol


def load_benchmark(ticker, tf="1d", since=None):
    """加载单只基准（QQQ）的收盘价。"""
    path = DATA_DIR / f"{ticker.lower()}_{tf}_raw.parquet"
    df = pd.read_parquet(path, columns=["close"])
    df.index = _strip_tz(pd.to_datetime(df.index))
    if since:
        df = df.loc[since:]
    return df["close"]


# ── 流动性掩码 ────────────────────────────────────────────────────────────────

def build_liquidity_mask(close, vol, min_dollar_vol=5_000_000, min_price=2.0, window=20):
    """True = 该（日期, 股票）流动性合格，可参与 IC 计算。"""
    dollar_vol_ma = (close * vol).rolling(window).mean()
    return (dollar_vol_ma >= min_dollar_vol) & (close >= min_price)


# ── 因子计算 ──────────────────────────────────────────────────────────────────

def compute_factors(close, vol, qqq_close):
    """
    返回因子字典，每项为 DataFrame (date × symbol)。
    注意：因子值不需要标准化，IC 计算时会做排名处理。
    """
    factors = {}

    # 1. 时序动量 Ret_5：过去5日涨跌幅
    factors["Ret_5"] = close.pct_change(5)

    # 2. 乖离率 BIAS_20：股价偏离20日均线的幅度（预期负向IC）
    ma20 = close.rolling(20).mean()
    factors["BIAS_20"] = (close - ma20) / ma20

    # 3. VPT_slope：量价趋势因子5日变化率
    #    VPT = 累积(成交量 × 当日涨跌幅)，捕捉机构吸筹/出货
    vpt = (vol * close.pct_change()).cumsum()
    vpt_scale = vpt.abs().rolling(20).mean().replace(0, np.nan)
    factors["VPT_slope"] = vpt.diff(5) / vpt_scale

    # 4. HV_ratio：5日波动率 / 20日波动率，突破1表示近期异动
    log_ret = np.log(close / close.shift(1))
    hv5  = log_ret.rolling(5).std()
    hv20 = log_ret.rolling(20).std().replace(0, np.nan)
    factors["HV_ratio"] = hv5 / hv20

    # 5. RS_QQQ：个股5日收益 - QQQ5日收益，剔除大盘 beta
    stock_ret5 = close.pct_change(5)
    qqq_ret5   = qqq_close.pct_change(5)
    factors["RS_QQQ"] = stock_ret5.sub(qqq_ret5, axis=0)

    return factors


def compute_forward_returns(close):
    """计算各预测周期的未来收益率面板。"""
    return {h: close.shift(-h) / close - 1 for h in HORIZONS}


# ── IC 计算 ───────────────────────────────────────────────────────────────────

def compute_ic_series(factor_panel, fwd_panel, liquid_mask, min_stocks=MIN_STOCKS):
    """
    逐日计算截面 Spearman IC。

    Spearman 本身基于排名，对因子和收益率的极端值天然鲁棒，
    不需要额外做截尾处理。
    """
    common_dates = factor_panel.index.intersection(fwd_panel.index)
    common_dates = common_dates.intersection(liquid_mask.index)

    f_panel  = factor_panel.reindex(common_dates)
    r_panel  = fwd_panel.reindex(common_dates)
    lm       = liquid_mask.reindex(common_dates)

    ic_vals, ic_idx = [], []

    for date in common_dates:
        lm_row = lm.loc[date].reindex(f_panel.columns).fillna(False)

        f_row = f_panel.loc[date].where(lm_row).dropna()
        r_row = r_panel.loc[date].dropna()
        syms  = f_row.index.intersection(r_row.index)

        if len(syms) < min_stocks:
            continue

        ic, _ = spearmanr(f_row[syms], r_row[syms])
        if not np.isnan(ic):
            ic_vals.append(ic)
            ic_idx.append(date)

    return pd.Series(ic_vals, index=ic_idx, name="IC")


def ic_stats(ic_series):
    """计算 IC 均值、IC_std、ICIR、t统计量。"""
    n = len(ic_series)
    if n < 20:
        return dict(IC_mean=np.nan, IC_std=np.nan, ICIR=np.nan, t_stat=np.nan, n_days=n)
    mean = ic_series.mean()
    std  = ic_series.std()
    icir   = mean / std            if std > 0 else np.nan
    t_stat = mean / (std / n**0.5) if std > 0 else np.nan
    return dict(IC_mean=mean, IC_std=std, ICIR=icir, t_stat=t_stat, n_days=n)


# ── 主扫描循环 ────────────────────────────────────────────────────────────────

def run_scan(factors, fwd_returns, liquid_mask, min_stocks=MIN_STOCKS):
    """遍历所有因子 × 预测周期，返回统计结果和IC时序。"""
    results     = {}  # (factor_name, horizon) → stats dict
    ic_series_all = {}  # (factor_name, horizon) → IC Series

    total = len(factors) * len(HORIZONS)
    i = 0
    for fname, fpanel in factors.items():
        for h in HORIZONS:
            i += 1
            print(f"  [{i:2d}/{total}] {fname:<12} fwd_{h:2d}d ...", end=" ", flush=True)
            ic    = compute_ic_series(fpanel, fwd_returns[h], liquid_mask, min_stocks)
            stats = ic_stats(ic)
            results[(fname, h)]      = stats
            ic_series_all[(fname, h)] = ic

            ic_v = stats["IC_mean"]
            ir_v = stats["ICIR"]
            ic_str = f"{ic_v:+.4f}" if not np.isnan(ic_v) else "  N/A "
            ir_str = f"{ir_v:+.3f}" if not np.isnan(ir_v) else "  N/A"
            print(f"IC={ic_str}  ICIR={ir_str}  n={stats['n_days']}")

    return results, ic_series_all


# ── 可视化 ────────────────────────────────────────────────────────────────────

def plot_heatmap(results, factor_names, horizons, out_path):
    """IC 均值热力图（因子 × 预测周期）。"""
    ic_grid   = np.array([[results[(f, h)]["IC_mean"] for h in horizons] for f in factor_names])
    icir_grid = np.array([[results[(f, h)]["ICIR"]    for h in horizons] for f in factor_names])

    fig, ax = plt.subplots(figsize=(9, max(4, len(factor_names) * 1.0)))
    vmax = max(0.06, np.nanmax(np.abs(ic_grid)))
    im = ax.imshow(ic_grid, cmap="RdYlGn", vmin=-vmax, vmax=vmax, aspect="auto")

    ax.set_xticks(range(len(horizons)))
    ax.set_xticklabels([f"fwd_{h}d" for h in horizons], fontsize=11)
    ax.set_yticks(range(len(factor_names)))
    ax.set_yticklabels(factor_names, fontsize=11)

    for i, fname in enumerate(factor_names):
        for j, h in enumerate(horizons):
            ic_v = ic_grid[i, j]
            ir_v = icir_grid[i, j]
            if np.isnan(ic_v):
                label = "N/A"
            else:
                star = " ★" if not np.isnan(ir_v) and abs(ir_v) >= 0.5 else ""
                label = f"{ic_v:+.3f}{star}"
            brightness = abs(ic_v) / vmax if not np.isnan(ic_v) else 0
            text_color = "white" if brightness > 0.65 else "black"
            ax.text(j, i, label, ha="center", va="center",
                    fontsize=10, color=text_color, fontweight="bold")

    plt.colorbar(im, ax=ax, label="IC 均值", fraction=0.046, pad=0.04)
    ax.set_title(
        "因子 IC 热力图    ★ = |ICIR| ≥ 0.5（因子稳定有效）\n"
        "绿色=正向因子（动量）  红色=负向因子（反转）",
        fontsize=10, pad=10,
    )
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"热力图 → {out_path.name}")


def plot_ic_series(ic_series_all, factor_names, out_path, horizon=5):
    """各因子在 fwd_{horizon}d 上的 IC 时序图。"""
    n = len(factor_names)
    fig, axes = plt.subplots(n, 1, figsize=(14, 3.5 * n), sharex=True)
    if n == 1:
        axes = [axes]

    for ax, fname in zip(axes, factor_names):
        ic = ic_series_all.get((fname, horizon), pd.Series(dtype=float))
        if ic.empty:
            ax.set_title(f"{fname}（无数据）")
            continue

        mean_v = ic.mean()
        std_v  = ic.std()
        icir_v = mean_v / std_v if std_v > 0 else 0
        cumulative = ic.cumsum()

        bar_color = "#2dc653" if mean_v >= 0 else "#e63946"
        ax.bar(ic.index, ic, color=bar_color, alpha=0.4, width=2, label="每日 IC")
        ax.axhline(0,      color="#aaa",       lw=0.5, ls="-")
        ax.axhline(mean_v, color=bar_color,    lw=1.0, ls="--",
                   label=f"均值 {mean_v:+.4f}")

        ax2 = ax.twinx()
        ax2.plot(ic.index, cumulative, color="#1a1a2e", lw=1.2, label="累计 IC")
        ax2.set_ylabel("累计 IC", fontsize=8, color="#1a1a2e")

        ax.set_title(
            f"{fname}  →  fwd_{horizon}d    "
            f"IC均值={mean_v:+.4f}  ICIR={icir_v:+.3f}  "
            f"n={len(ic)}天",
            fontsize=10,
        )
        ax.set_ylabel("IC")
        ax.legend(loc="upper left", fontsize=7, framealpha=0.7)
        ax.grid(alpha=0.2)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=6))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha="right", fontsize=7)

    plt.suptitle(f"各因子 IC 时序（预测周期 fwd_{horizon}d）", fontsize=11, y=1.005)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"IC时序图 → {out_path.name}")


# ── 控制台汇总表 ──────────────────────────────────────────────────────────────

def print_summary(results, factor_names, horizons):
    W = 70
    print("\n" + "=" * W)
    print("因子 IC 分析汇总  （|IC均值| > 0.03 且 |ICIR| > 0.5 为有效）")
    print("=" * W)

    header = f"{'因子':<14}" + "".join(f"  fwd_{h:2d}d" for h in horizons)
    print(header)
    print("-" * W)

    for fname in factor_names:
        row = f"{fname:<14}"
        for h in horizons:
            s  = results[(fname, h)]
            ic = s["IC_mean"]
            ir = s["ICIR"]
            if np.isnan(ic):
                row += "     N/A"
            else:
                star = "★" if not np.isnan(ir) and abs(ir) >= 0.5 else " "
                row += f"  {ic:+.3f}{star}"
        print(row)

    print("-" * W)
    print("★=|ICIR|≥0.5  正值=动量因子  负值=反转因子")

    print("\n推荐纳入策略的因子（满足双重标准）：")
    best = [
        (fname, h, s["IC_mean"], s["ICIR"])
        for (fname, h), s in results.items()
        if abs(s.get("IC_mean") or 0) > 0.03 and abs(s.get("ICIR") or 0) > 0.5
    ]
    if best:
        for fname, h, ic, icir in sorted(best, key=lambda x: abs(x[3]), reverse=True):
            direction = "正向(追涨)" if ic > 0 else "负向(反转)"
            print(f"  {fname:<14} fwd_{h}d  IC={ic:+.4f}  ICIR={icir:+.3f}  {direction}")
    else:
        print("  暂无满足双重标准的因子，可尝试调整 --since 或 --min-stocks。")
    print("=" * W)


# ── 入口 ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="因子IC扫描器")
    parser.add_argument("--symbols",    nargs="+", default=None,
                        help="指定标的（默认读 universe.json）")
    parser.add_argument("--since",      default="2021-01-01", help="数据起始日期")
    parser.add_argument("--tf",         default="1d")
    parser.add_argument("--min-stocks", type=int, default=MIN_STOCKS,
                        help=f"每日最少有效股票数（默认 {MIN_STOCKS}）")
    parser.add_argument("--no-show",    action="store_true", help="只保存图片不弹窗")
    args = parser.parse_args()

    # ── 加载数据 ──────────────────────────────────────────────────────────────
    if args.symbols:
        symbols    = [s.upper() for s in args.symbols]
    else:
        symbols, _ = load_universe()

    print(f"加载 {len(symbols)} 只股票（since {args.since}）...")
    close, vol = load_panel(symbols, args.tf, since=args.since)
    print(f"  有效标的: {close.shape[1]} 只  日期范围: "
          f"{close.index[0].date()} ~ {close.index[-1].date()}  "
          f"({close.shape[0]} 行)")

    qqq_close = load_benchmark("QQQ", args.tf, since=args.since).reindex(close.index)

    # ── 流动性掩码 ────────────────────────────────────────────────────────────
    liquid = build_liquidity_mask(close, vol)
    pct    = liquid.stack().mean() * 100
    print(f"  流动性达标比例: {pct:.1f}% （不达标的(日期,股票)对排除在IC计算外）")

    # ── 因子 & 未来收益 ───────────────────────────────────────────────────────
    print("计算因子...")
    factors     = compute_factors(close, vol, qqq_close)
    fwd_returns = compute_forward_returns(close)

    # ── IC 扫描 ───────────────────────────────────────────────────────────────
    print(f"IC 扫描（{len(factors)} 因子 × {len(HORIZONS)} 预测周期）...")
    results, ic_series_all = run_scan(factors, fwd_returns, liquid, args.min_stocks)

    # ── 输出 ──────────────────────────────────────────────────────────────────
    factor_names = list(factors.keys())

    print_summary(results, factor_names, HORIZONS)

    # CSV
    rows = [{"factor": f, "horizon": f"fwd_{h}d", **s}
            for (f, h), s in results.items()]
    csv_path = DATA_DIR / "factor_ic_summary.csv"
    pd.DataFrame(rows).to_csv(csv_path, index=False)
    print(f"CSV → {csv_path.name}")

    # 图表
    plot_heatmap(results, factor_names, HORIZONS,
                 DATA_DIR / "factor_ic_heatmap.png")
    plot_ic_series(ic_series_all, factor_names,
                   DATA_DIR / "factor_ic_series.png", horizon=5)


if __name__ == "__main__":
    main()
