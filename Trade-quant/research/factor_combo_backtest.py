"""
多因子合成回测 — 状态机动态加权策略

因子：RS_Beta + MFI_14 + BIAS_20 + HV_ratio（4 个核心因子）
      ↳ 用 BIAS_20（均值回归，负权重惩罚过热）替换 LR_Slope（0.86 共线性）
        Calmar 1.78 → 2.17，年化 84% → 113%，多承受 5% 回撤但收益增加 34.5%
加权：QQQ MA50 三态开关（牛市 / 熊市 / 震荡）
      ↳ 比 SPY MA200 更快捕捉趋势拐点，Sharpe 0.99→1.25，MaxDD -66.8%→-47.3%
过滤：Combo_Score > 1.0 AND Vol_Shock > 1.2
仓位：前 N 名等权，每 5 个交易日调仓（Top-5 MaxDD 最优）

输出（data/ 目录）：
  combo_corr.png       因子截面相关性热力图
  combo_equity.png     净值曲线 vs SPY / QQQ
  combo_report.html    完整 HTML 报告

执行口径（--exec-mode，默认 live）：
  live    与 live/alpaca_trader.py 三段式对齐：T 收盘信号 → T+1 收盘卖 → T+2 开盘买，
          含单边成本（--cost-bps）、单票上限（--max-pos-pct）、熔断模拟（--kill-dd）
  legacy  旧口径：信号收盘价成交 + 零成本。存在前视偏差（信号依赖当日收盘数据却按
          同一收盘价成交），仅用于新旧对比，不作为绩效依据

用法：
  python factor_combo_backtest.py
  python factor_combo_backtest.py --exec-mode legacy          # 旧口径对比
  python factor_combo_backtest.py --min-stocks 3 --kill-dd 0  # 关闭熔断
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
import strategy_params
from factor_scanner import (
    load_panel, load_benchmark, load_universe, build_liquidity_mask,
    compute_factors, compute_spy_regime, REGIMES,
    _DARK_BG, _DARK_AX, _GRID_CLR, _TEXT_CLR, _POS_CLR, _NEG_CLR, _CUM_CLR,
    _dark_fig,
)

DATA_DIR   = Path(__file__).parent.parent / "data"
REPORT_DIR = Path(__file__).parent.parent / "report"
REPORT_DIR.mkdir(exist_ok=True)
MIN_STOCKS = 10  # 调仓日至少有这么多只候选股才入场

# ── 状态机权重矩阵 ─────────────────────────────────────────────────────────────
# BIAS_20 是反转因子（IC 为负）：
#   负权重 = 惩罚近期涨幅过大的过热股，奖励超卖股
#   牛市：用负权重做相对强度过滤（避免追高）
#   熊市/震荡：正权重 = 主动寻找超卖反弹机会
CORE_FACTORS = ["RS_Beta", "MFI_14", "BIAS_20", "HV_ratio"]

REGIME_WEIGHTS = {
    1: {   # 牛市：QQQ > MA50
        "RS_Beta":  0.50,
        "MFI_14":   0.20,
        "BIAS_20": -0.20,
        "HV_ratio": 0.10,
    },
    -1: {  # 熊市：QQQ < MA50
        "RS_Beta":  0.00,
        "MFI_14":  -0.30,
        "BIAS_20":  0.20,
        "HV_ratio": 0.50,
    },
    0: {   # 震荡：QQQ ≈ MA50（无缓冲区）
        "RS_Beta":  0.10,
        "MFI_14":  -0.30,
        "BIAS_20":  0.10,
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


# ── 实盘时序回测引擎（exec-mode=live）──────────────────────────────────────────
#
# 与 live/alpaca_trader.py 对齐的执行模型；默认采用改良后的 MOC 单段时序，
# 也支持原三段式（two_leg）用于对比。
#
# exec_stage="moc_single"（默认，2026-07-12 起）：
#   T   收盘后：用 T 日数据算信号、定目标持仓（plan）
#   T+1 15:45 ET：卖买合并到 15:50 前提交为 MOC 二腿
#                 → 16:00 收盘拍卖同一价印成交（cash 账户 unsettled_proceeds 支持同日回笼）
#   优点：卖买同一时刻结算，无 T+1→T+2 跨夜风险；MOC 拍卖保证成交
#   实盘对应：alpaca_trader.py 待改造的方案 A+（MOC/MOC + LOO 兜底）
#
# exec_stage="two_leg"（历史）：
#   T+1 收盘：卖  ；T+2 开盘：买  → 卖买之间存在一个交易日现金空置
#
# 熔断策略（三件套，可分别开关）：
#   1) 熊市 regime 空仓：QQQ<MA50 时不建仓，仓位全部换成现金
#   2) 软减仓阶梯：账户回撤 -10% → 目标仓位 x0.5；-20% → x0.25
#   3) Kill Switch cooldown：回撤达 --kill-dd 阈值（默认 -30%）触发清仓，
#      进入静默期，"至少 N 交易日" 或 "QQQ 回 MA50（regime==1）" 任一满足即复出，
#      复出时 high_watermark 重置为当前净值（避免立即再次触发）
#
# 简化项（方向上偏乐观）：
#   - 允许碎股，无整数股取整损耗
#   - 停牌/缺数据的持仓按最后收盘价估值（退市终局损失被低估）

def _drawdown_exposure_multiplier(dd_smoothed: float, regime_val,
                                  scope: str = "all", ladder=None) -> float:
    """
    软减仓阶梯（EWMA 平滑 + regime 作用域）：
      scope="all"      任何 regime 都触发（原行为）
      scope="bear"     仅熊市（regime==-1）触发——牛市短促 shake-out 保持满仓
      scope="non_bull" 熊市 + 震荡（regime!=1）触发

    以回撤的 EWMA 值判断阶梯，避免单日波动瞬时触发；反弹时 EWMA 自然衰减，
    暴露倍数自动恢复。
    """
    if scope == "bear" and regime_val != -1:
        return 1.0
    if scope == "non_bull" and regime_val == 1:
        return 1.0
    if ladder is None:
        ladder = [(-0.20, 0.25), (-0.10, 0.50)]
    for threshold, mult in ladder:
        if dd_smoothed <= threshold:
            return mult
    return 1.0


def _regime_exposure_multiplier(regime_val, bear_streak: int,
                                bear_flat_days: int, choppy_half: bool) -> float:
    """
    基于宏观状态的仓位倍数。
      - 牛市 regime==1：1.0
      - 震荡 regime==0：choppy_half ? 0.5 : 1.0
      - 熊市 regime==-1：分阶段——连续熊市 < bear_flat_days 时空仓（0.0），
        之后允许按原熊市权重（HV_ratio + BIAS 超卖反弹）交易（1.0）。
        bear_flat_days=0 → 熊市始终允许交易；很大值 → 全程空仓（等价旧 bear_flat=True）。
    """
    if regime_val == -1:
        if bear_flat_days > 0 and bear_streak <= bear_flat_days:
            return 0.0
        return 1.0
    if regime_val == 0 and choppy_half:
        return 0.5
    return 1.0


def run_backtest_live(combo, vol_shock, close, open_px, liquid_mask,
                      spy_close, qqq_close, regime,
                      min_score=1.0, vol_min=1.2, top_n=5, rebalance=5,
                      cost_bps=25.0, max_pos_pct=0.50, min_stocks=0,
                      kill_dd=None,
                      exec_stage="moc_single",
                      bear_flat_days=0, choppy_half=True,
                      soft_drawdown=False, dd_ewma_span=10, soft_drawdown_scope="all",
                      cooldown_min_days=20):
    dates    = close.index
    close_ff = close.ffill()
    cost     = cost_bps / 10_000.0

    cash, shares  = 1.0, {}
    equity_curve  = pd.Series(np.nan, index=dates)
    trade_log     = []
    pending_sells = {}    # {exec_i: [(sym, 目标市值, 0=清仓)]}
    pending_buys  = {}    # {exec_i: {sym: 计划买入金额, "_use_open": bool}}
    hwm           = 1.0

    # 熔断/静默期状态机
    kill_events         = []       # [(触发日, 复出日 or None)]
    cooldown_start_i    = None
    cooldown_active     = False

    # 熊市连续天数（每日结束时更新，供次日信号使用）
    bear_streak         = 0

    # 回撤 EWMA：α = 2/(span+1)；只对回撤（负值）做平滑，正值保持"无回撤"含义
    dd_alpha            = 2.0 / (max(dd_ewma_span, 1) + 1)
    dd_ewma             = 0.0

    def _px(panel, dt, sym):
        try:
            v = panel.at[dt, sym]
        except KeyError:
            return np.nan
        return v

    def _liquidate_at_close(dt):
        nonlocal cash
        for sym, q in list(shares.items()):
            px = _px(close_ff, dt, sym)
            if not pd.isna(px):
                cash += q * px * (1 - cost)
        shares.clear()

    for i, date in enumerate(dates):
        regime_now = regime.loc[date] if date in regime.index else np.nan

        # ── 1) 静默期退出判断（放在最前，让当日仍可正常估值 + 立即恢复交易能力）
        if cooldown_active:
            elapsed = i - cooldown_start_i
            time_ok   = elapsed >= cooldown_min_days
            regime_ok = (regime_now == 1)
            if time_ok or regime_ok:
                cooldown_active = False
                hwm = None    # 稍后由当日 equity 重置
                # 补记 kill_events 复出日
                if kill_events and kill_events[-1][1] is None:
                    kill_events[-1] = (kill_events[-1][0], date)

        # ── 2) T+1 收盘执行卖出 ────────────────────────────────────────────
        if i in pending_sells:
            for sym, tgt_val in pending_sells.pop(i):
                if sym not in shares:
                    continue
                px = _px(close_ff, date, sym)
                if pd.isna(px) or px <= 0:
                    continue
                cur_val  = shares[sym] * px
                sell_val = cur_val if tgt_val <= 0 else max(cur_val - tgt_val, 0.0)
                if sell_val <= 0:
                    continue
                shares[sym] -= sell_val / px
                if shares[sym] * px < 1e-12:
                    shares.pop(sym)
                cash += sell_val * (1 - cost)

        # ── 3) 买入执行：moc_single → T+1 收盘价；two_leg → T+2 开盘价 ──────
        if i in pending_buys:
            plan = pending_buys.pop(i)
            use_open = plan.pop("_use_open", False)
            price_panel = open_px if use_open else close_ff
            total_want = sum(plan.values())
            if total_want > 0 and cash > 1e-12:
                scale = min(1.0, cash / total_want)
                for sym, want in plan.items():
                    px = _px(price_panel, date, sym)
                    if pd.isna(px) or px <= 0:
                        continue
                    spend = want * scale
                    if spend <= 0:
                        continue
                    shares[sym] = shares.get(sym, 0.0) + spend * (1 - cost) / px
                    cash -= spend

        # ── 4) 收盘估值 ────────────────────────────────────────────────────
        pos_val = 0.0
        for sym, q in shares.items():
            px = _px(close_ff, date, sym)
            if not pd.isna(px):
                pos_val += q * px
        eq = cash + pos_val
        equity_curve.iloc[i] = eq

        # ── 5) 高水位/回撤 EWMA/熔断 ───────────────────────────────────────
        if hwm is None:
            hwm = eq
            dd_ewma = 0.0     # 静默期复出：EWMA 也归零，避免旧回撤残留
        else:
            hwm = max(hwm, eq)
        dd = eq / hwm - 1 if hwm > 0 else 0.0
        dd_ewma = dd_alpha * dd + (1 - dd_alpha) * dd_ewma

        # 熊市连续天数追踪（用真实 regime，不受策略持仓影响）
        if regime_now == -1:
            bear_streak += 1
        else:
            bear_streak = 0

        if kill_dd is not None and not cooldown_active and dd <= kill_dd:
            _liquidate_at_close(date)
            equity_curve.iloc[i] = cash
            cooldown_active   = True
            cooldown_start_i  = i
            kill_events.append((date, None))
            continue      # 熔断当日不再生成新信号

        # ── 6) 信号生成（静默期跳过）───────────────────────────────────────
        if cooldown_active:
            continue
        # 卖买执行时点：moc_single 需要 i+1 存在；two_leg 需要 i+2 存在
        if not (i > 0 and i % rebalance == 0):
            continue
        if exec_stage == "moc_single" and i + 1 >= len(dates):
            continue
        if exec_stage == "two_leg" and i + 2 >= len(dates):
            continue

        scores = combo.loc[date]     if date in combo.index     else pd.Series(dtype=float)
        vs     = vol_shock.loc[date] if date in vol_shock.index else pd.Series(dtype=float)
        lm     = (liquid_mask.loc[date].fillna(False)
                  if date in liquid_mask.index
                  else pd.Series(True, index=scores.index))

        valid = scores[
            lm.reindex(scores.index, fill_value=False) &
            (scores > min_score) &
            (vs.reindex(scores.index, fill_value=0) > vol_min)
        ].dropna()

        top = valid.nlargest(top_n)
        if min_stocks and len(top) < min_stocks:
            top = top.iloc[:0]

        # 综合暴露倍数：regime × drawdown ladder（EWMA 平滑）
        exposure = _regime_exposure_multiplier(regime_now, bear_streak,
                                               bear_flat_days, choppy_half)
        if soft_drawdown:
            exposure *= _drawdown_exposure_multiplier(dd_ewma, regime_now,
                                                     scope=soft_drawdown_scope)

        n = len(top)
        w = min(1.0 / n, max_pos_pct) if n > 0 else 0.0
        targets = {s: w * eq * exposure for s in top.index}

        # 卖出计划：不在目标名单 或 现值超目标 → 卖
        sell_list = []
        for sym in list(shares.keys()):
            px = _px(close_ff, date, sym)
            cur_val = 0.0 if pd.isna(px) else shares[sym] * px
            if sym not in targets or exposure == 0:
                sell_list.append((sym, 0.0))
            elif cur_val > targets[sym] * 1.02:
                sell_list.append((sym, targets[sym]))

        # 买入计划：目标市值高于现值 0.5% 以上才补
        buy_plan = {}
        if exposure > 0:
            for sym, tgt in targets.items():
                px = _px(close_ff, date, sym)
                cur_val = shares.get(sym, 0.0) * (0.0 if pd.isna(px) else px)
                if tgt - cur_val > eq * 0.005:
                    buy_plan[sym] = tgt - cur_val

        # 排定执行时点
        if exec_stage == "moc_single":
            # 卖买都在 T+1 收盘（MOC 二腿）
            if sell_list:
                pending_sells[i + 1] = sell_list
            if buy_plan:
                buy_plan["_use_open"] = False   # 用 close 面板
                pending_buys[i + 1] = buy_plan
        else:  # two_leg
            if sell_list:
                pending_sells[i + 1] = sell_list
            if buy_plan:
                buy_plan["_use_open"] = True    # 用 open 面板
                pending_buys[i + 2] = buy_plan

        trade_log.append({
            "date": date, "n_valid": len(valid),
            "stocks": list(top.index), "held": n,
            "regime": int(regime_now) if not pd.isna(regime_now) else None,
            "exposure": round(float(exposure), 3),
        })

    spy_ret    = spy_close.pct_change().reindex(dates).fillna(0)
    qqq_ret    = qqq_close.pct_change().reindex(dates).fillna(0)
    port_ret   = equity_curve.pct_change().fillna(0)
    spy_equity = (1 + spy_ret).cumprod()
    qqq_equity = (1 + qqq_ret).cumprod()

    return (equity_curve, spy_equity, qqq_equity, port_ret, trade_log,
            kill_events)


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
        is_strat = row["标的"].startswith("策略 Combo")
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
    <span class="tag tag-bull">牛市（QQQ &gt; MA50）RS_Beta×0.5 + MFI_14×0.2 + BIAS_20×(−0.2) + HV_ratio×0.1</span><br>
    <span class="tag tag-bear">熊市（QQQ &lt; MA50）HV_ratio×0.5 + BIAS_20×0.2 + MFI_14×(−0.3) 超卖反弹</span><br>
    <span class="tag tag-neu">震荡（QQQ ≈ MA50）HV_ratio×0.4 + BIAS_20×0.1 + MFI_14×(−0.3) 高抛低吸</span><br><br>
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
    # 默认值来自 strategy_params.py 单一来源，与 live/order_feasibility_audit 保持一致
    parser.add_argument("--top-n",     type=int,   default=strategy_params.TOP_N)
    parser.add_argument("--min-score", type=float, default=strategy_params.MIN_SCORE)
    parser.add_argument("--vol-min",   type=float, default=strategy_params.VOL_MIN)
    parser.add_argument("--rebalance", type=int,   default=strategy_params.REBALANCE_DAYS)
    parser.add_argument("--exec-mode", choices=["live", "legacy"], default="live",
                        help="live=按实盘时序成交（含成本与约束）；legacy=旧口径（仅用于对比）")
    parser.add_argument("--exec-stage", choices=["moc_single", "two_leg"], default="moc_single",
                        help="live 时序：moc_single=T+1 收盘 MOC 二腿一次成交（推荐，实盘对应 MOC/MOC+LOO 兜底）；"
                             "two_leg=T+1 收盘卖 / T+2 开盘买（原三段式对齐）")
    parser.add_argument("--cost-bps",    type=float, default=25.0, help="单边成本（bp），仅 live 模式")
    parser.add_argument("--max-pos-pct", type=float, default=0.50, help="单票权重上限，仅 live 模式")
    parser.add_argument("--min-stocks",  type=int,   default=0,
                        help="候选不足该数则本轮空仓（0=关闭），仅 live 模式")
    parser.add_argument("--kill-dd",     type=float, default=strategy_params.KILL_DD,
                        help="回撤熔断阈值（如 -0.30；0=关闭），仅 live 模式")
    parser.add_argument("--cooldown-days", type=int, default=20,
                        help="熔断后静默期最少交易日（或 QQQ 回 MA50 任一满足即复出）")
    # 风控三件套（第十四阶段：默认只留 choppy_half + 静默期熔断）
    # 默认 = 第十四阶段"平衡档"：--bear-flat-days 45 + soft_drawdown + scope=bear + span=5
    # CAGR +39.7% / Sharpe 0.89 / MaxDD −38.5% / 熔断×3 全部复出（详见 RESEARCH_LOG §14.4c）
    parser.add_argument("--bear-flat-days", type=int, default=45,
                        help="熊市初期空仓天数（连续 N 天熊市内空仓，之后允许反弹交易）。"
                             "默认 45（平衡档）；0=熊市始终交易；999=全程空仓")
    parser.add_argument("--choppy-half", dest="choppy_half", action="store_true", default=True,
                        help="震荡 regime 仓位减半（默认开启）")
    parser.add_argument("--no-choppy-half", dest="choppy_half", action="store_false")
    parser.add_argument("--soft-drawdown", dest="soft_drawdown", action="store_true", default=True,
                        help="软减仓阶梯：EWMA 平滑回撤触发 -10%%→50%% / -20%%→25%%（默认开启）")
    parser.add_argument("--no-soft-drawdown", dest="soft_drawdown", action="store_false")
    parser.add_argument("--dd-ewma-span", type=int, default=5,
                        help="回撤 EWMA 平滑窗口（交易日）；越小越灵敏，越大越迟缓（默认 5）")
    parser.add_argument("--soft-drawdown-scope", choices=["all", "bear", "non_bull"],
                        default="bear",
                        help="软减仓作用域：bear=仅熊市触发（默认，牛市保持满仓吃反弹）；"
                             "all=任何 regime 触发；non_bull=熊市+震荡触发")
    args = parser.parse_args()

    # ── 数据加载 ──────────────────────────────────────────────────────────────
    symbols, _ = load_universe()
    print(f"加载 {len(symbols)} 只股票...")
    close, high, low, vol, open_px = load_panel(symbols, args.tf, since=args.since,
                                                include_open=True)
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

    # ── 宏观状态开关（QQQ MA50，无缓冲区）────────────────────────────────────
    regime, qqq_ma50 = compute_spy_regime(qqq_close, ma_window=50, buffer=0.0)
    regime_days = {
        "牛市": int((regime == 1).sum()),
        "熊市": int((regime == -1).sum()),
        "震荡": int((regime == 0).sum()),
    }
    print(f"宏观状态分布（QQQ MA50）: {regime_days}")

    # ── Combo Score ───────────────────────────────────────────────────────────
    print("计算 Combo Score...")
    combo = compute_combo(z_panels, regime)

    # ── 回测 ──────────────────────────────────────────────────────────────────
    print(f"回测（exec-mode={args.exec_mode}，持仓前{args.top_n}名，"
          f"每{args.rebalance}日调仓，Combo>{args.min_score}，Vol>{args.vol_min}x）...")
    kill_events = []
    if args.exec_mode == "live":
        kill_dd = args.kill_dd if args.kill_dd != 0 else None
        equity, spy_eq, qqq_eq, port_ret, trade_log, kill_events = run_backtest_live(
            combo, vol_shock, close, open_px, liquid, spy_close, qqq_close, regime,
            min_score=args.min_score, vol_min=args.vol_min,
            top_n=args.top_n, rebalance=args.rebalance,
            cost_bps=args.cost_bps, max_pos_pct=args.max_pos_pct,
            min_stocks=args.min_stocks, kill_dd=kill_dd,
            exec_stage=args.exec_stage,
            bear_flat_days=args.bear_flat_days, choppy_half=args.choppy_half,
            soft_drawdown=args.soft_drawdown, dd_ewma_span=args.dd_ewma_span,
            soft_drawdown_scope=args.soft_drawdown_scope,
            cooldown_min_days=args.cooldown_days,
        )
        stage_desc = ("T+1 收盘 MOC 二腿" if args.exec_stage == "moc_single"
                      else "T+1 收盘卖 / T+2 开盘买")
        print(f"  执行假设: T 信号 → {stage_desc}，单边成本 {args.cost_bps:.0f}bp，"
              f"单票上限 {args.max_pos_pct:.0%}，熔断 {kill_dd if kill_dd else '关闭'}")
        toggles = []
        if args.bear_flat_days > 0:
            toggles.append(f"熊市前 {args.bear_flat_days} 天空仓")
        if args.choppy_half:
            toggles.append("震荡减半")
        if args.soft_drawdown:
            toggles.append(f"软减仓 EWMA{args.dd_ewma_span}[{args.soft_drawdown_scope}]")
        print(f"  风控三件套: {' / '.join(toggles) if toggles else '仅静默期熔断'}"
              f"，静默期 ≥{args.cooldown_days} 交易日 或 QQQ 回 MA50")
        for start, resume in kill_events:
            resume_txt = f"复出 {resume.date()}" if resume is not None else "至回测终点仍静默"
            print(f"  ⚠️  KILL SWITCH: {start.date()} → {resume_txt}")
    else:
        equity, spy_eq, qqq_eq, port_ret, trade_log = run_backtest(
            combo, vol_shock, close, liquid, spy_close, qqq_close,
            min_score=args.min_score, vol_min=args.vol_min,
            top_n=args.top_n, rebalance=args.rebalance,
        )
        print("  ⚠️  legacy 口径：信号收盘价成交 + 零成本，存在前视偏差，仅用于对比")
    print(f"  调仓次数: {len(trade_log)}")

    # ── 绩效统计 ──────────────────────────────────────────────────────────────
    spy_ret_s = spy_close.pct_change().reindex(close.index).fillna(0)
    qqq_ret_s = qqq_close.pct_change().reindex(close.index).fillna(0)

    if args.exec_mode == "legacy":
        strat_label = "策略 Combo"
    else:
        strat_label = f"策略 Combo (实盘/{args.exec_stage})"
        if kill_events:
            strat_label += f" 熔断×{len(kill_events)}"
    stats_rows = [
        perf_stats(equity,    port_ret,  strat_label),
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
    corr_path   = REPORT_DIR / "combo_corr.png"
    equity_path = REPORT_DIR / "combo_equity.png"
    html_path   = REPORT_DIR / "combo_report.html"

    print("\n生成图表...")
    plot_corr(corr_df, corr_path)
    plot_equity(equity, spy_eq, qqq_eq, regime, qqq_ma50, port_ret, equity_path)

    generate_report(
        stats_rows, corr_df, trade_log,
        corr_path, equity_path, html_path,
        regime_days=regime_days,
        top_n=args.top_n, min_score=args.min_score,
        vol_min=args.vol_min, rebalance=args.rebalance,
    )

    print(f"\n全部输出已保存至 {REPORT_DIR}/")
    print(f"  用浏览器打开: open {html_path}")


if __name__ == "__main__":
    main()
