"""
调仓历史分析：每个调仓日的信号、订单数量、成交可行性审计

覆盖范围：
  - 部署期：约 2026-06-09 ～ 数据截止日（parquet 约到 2026-06-18）
  - 1年回溯：2025-06-09 ～ 数据截止日

检验维度：
  1. 信号有效性  —— 候选股数量、Top-N 分数
  2. 下单数量    —— 按 equity/N 计算，同时考虑 open gap 影响
  3. 流动性      —— 单笔订单 / 当日美元成交量（impact ratio）
  4. 资金充裕性  —— 卖出释放资金 vs 买入总需求
  5. 跳空风险    —— 下单日 open vs 信号日 close（等于实际成交价偏差）

用法：
  cd Trade-quant
  python research/order_feasibility_audit.py
  python research/order_feasibility_audit.py --since 2024-01-01
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── 路径 ─────────────────────────────────────────────────────────────────────
ROOT_DIR    = Path(__file__).parent.parent
RESEARCH_DIR = ROOT_DIR / "research"
DATA_DIR    = ROOT_DIR / "data"
sys.path.insert(0, str(RESEARCH_DIR))

from factor_scanner import (
    load_universe, build_liquidity_mask, compute_factors,
)
from factor_combo_backtest import zscore_factors, CORE_FACTORS, REGIME_WEIGHTS
from strategy_params import TOP_N, MIN_SCORE, VOL_MIN, REBALANCE_DAYS

# ── 策略参数（TOP_N/MIN_SCORE/VOL_MIN/REBALANCE_DAYS 从 strategy_params.py 导入）
EQUITY        = 5_000.0      # 实际账户净值 $5,000
MIN_DATA_ROWS = 150          # 单只股票最少有效行
DATA_WARMUP   = "2023-06-01" # 数据加载起点（2年回溯留足 warmup）
GAP_BUFFER    = 1.05         # 与 alpaca_trader.py 一致的 5% 开盘缓冲

# ── 成交可行性阈值 ─────────────────────────────────────────────────────────────
IMPACT_WARN = 0.01   # 订单 / 日均美元成交量 > 1%，警告
IMPACT_CRIT = 0.05   # > 5%，严重
GAP_WARN    = 0.03   # 次日 open vs 信号日 close 偏差 > ±3%，提示
HALT_THRESH = 100    # 当日成交量 < 100 股，视为停牌/异常


# ── 数据加载 ─────────────────────────────────────────────────────────────────
def _strip_tz(idx):
    if hasattr(idx, "tz") and idx.tz is not None:
        return idx.tz_localize(None)
    return idx


def load_panel_with_open(symbols, since=None):
    """加载 close / open / high / low / volume，返回 5 个 DataFrame。"""
    closes, opens, highs, lows, vols = {}, {}, {}, {}, {}
    missing = []
    for sym in symbols:
        path = DATA_DIR / f"{sym.lower()}_1d_raw.parquet"
        if not path.exists():
            missing.append(sym)
            continue
        df = pd.read_parquet(path, columns=["open", "high", "low", "close", "volume"])
        df.index = _strip_tz(pd.to_datetime(df.index))
        if since:
            df = df.loc[since:]
        if len(df) < MIN_DATA_ROWS:
            continue
        closes[sym] = df["close"]
        opens[sym]  = df["open"]
        highs[sym]  = df["high"]
        lows[sym]   = df["low"]
        vols[sym]   = df["volume"]
    if missing:
        print(f"  [{len(missing)} 只缺 parquet，跳过]: {missing[:10]}{'...' if len(missing)>10 else ''}")
    idx = pd.DataFrame(closes).sort_index().index
    def _panel(d):
        return pd.DataFrame(d).reindex(idx)
    return (_panel(closes), _panel(opens), _panel(highs),
            _panel(lows),   _panel(vols))


def load_benchmark_open(ticker, since=None):
    path = DATA_DIR / f"{ticker.lower()}_1d_raw.parquet"
    df = pd.read_parquet(path, columns=["close"])
    df.index = _strip_tz(pd.to_datetime(df.index))
    if since:
        df = df.loc[since:]
    return df["close"]


# ── 信号计算（复用 alpaca_trader 逻辑）──────────────────────────────────────
def compute_signals_on_date(close, high, low, vol, qqq_close, as_of_idx):
    """
    给定截至 as_of_idx 的数据切片，返回 (top_syms, regime_str, scores_series)。
    as_of_idx: 信号日在 close.index 中的整数位置（使用该日及之前数据）。
    """
    c = close.iloc[:as_of_idx + 1]
    h = high.iloc[:as_of_idx + 1]
    l_ = low.iloc[:as_of_idx + 1]
    v = vol.iloc[:as_of_idx + 1]
    q = qqq_close.iloc[:as_of_idx + 1]

    stocks = [s for s in c.columns if s not in ("QQQ", "SPY")]
    cs, hs, ls, vs = c[stocks], h[stocks], l_[stocks], v[stocks]

    all_f = compute_factors(cs, hs, ls, vs, q)
    core  = {f: all_f[f] for f in CORE_FACTORS if f in all_f}

    liquid = build_liquidity_mask(cs, vs)
    z_panels = zscore_factors(core, liquid)

    qqq_last = float(q.iloc[-1])
    qqq_ma50 = float(q.rolling(50).mean().iloc[-1])
    if qqq_last > qqq_ma50:
        regime, rstr = 1, "牛市"
    elif qqq_last < qqq_ma50:
        regime, rstr = -1, "熊市"
    else:
        regime, rstr = 0, "震荡"

    weights = REGIME_WEIGHTS[regime]
    combo = pd.Series(0.0, index=cs.columns)
    for fname, w in weights.items():
        if w != 0 and fname in z_panels:
            combo = combo.add(z_panels[fname].iloc[-1] * w, fill_value=0)

    vol_shock = (vs.iloc[-1] / vs.rolling(20).mean().iloc[-1]).fillna(0)
    liquid_now = liquid.iloc[-1].reindex(combo.index, fill_value=False)
    mask = (
        liquid_now
        & (combo > MIN_SCORE)
        & (vol_shock.reindex(combo.index, fill_value=0) > VOL_MIN)
    )
    candidates = combo[mask].dropna()
    top_syms   = candidates.nlargest(TOP_N).index.tolist()

    return top_syms, rstr, candidates, qqq_last, qqq_ma50


# ── 订单量 & 可行性 ──────────────────────────────────────────────────────────
def compute_orders(
    target_syms, prev_holdings,
    close_on_signal, open_on_next, vol_on_next, dollar_vol_ma,
    equity, top_n,
):
    """
    target_syms   : 当次目标持仓列表（经过等权计算）
    prev_holdings : 上次目标列表（用来计算 kept/exit）
    close_on_signal: 信号日的 close Series（用来算 qty）
    open_on_next  : 下单日的 open Series（实际成交参考价）
    vol_on_next   : 下单日的 volume Series（当日成交量）
    dollar_vol_ma : 20日均美元成交量 Series（流动性参考）
    equity        : 当前账户净值

    返回 orders: list of dict
    """
    target_val = equity / len(target_syms) if target_syms else 0.0
    prev_set   = set(prev_holdings)
    target_set = set(target_syms)

    orders = []
    total_sell = 0.0
    total_buy  = 0.0

    # 退出仓位（全仓平）
    for sym in sorted(prev_set - target_set):
        # 假设以 open 价成交
        entry_price = open_on_next.get(sym, float("nan")) if hasattr(open_on_next, "get") else open_on_next.get(sym, float("nan"))
        prev_close  = close_on_signal.get(sym, float("nan")) if hasattr(close_on_signal, "get") else float("nan")
        prev_val    = equity / len(prev_set) if prev_set else 0
        qty_est     = max(1, int(prev_val / (prev_close * GAP_BUFFER))) if prev_close > 0 else 0
        sell_val    = qty_est * (entry_price if not np.isnan(entry_price) else prev_close)
        total_sell += sell_val
        orders.append({
            "action": "SELL_CLOSE",
            "symbol": sym,
            "qty_est": qty_est,
            "ref_close": round(prev_close, 4) if not np.isnan(prev_close) else None,
            "exec_open": round(entry_price, 4) if not np.isnan(entry_price) else None,
            "sell_val": round(sell_val, 2),
            "buy_val": None,
            "open_gap_pct": None,
            "impact_pct": None,
            "flags": [],
        })

    # 目标仓位：计算每只的 delta
    kept_syms = sorted(target_set & prev_set)
    new_syms  = sorted(target_set - prev_set)

    # kept 持仓：trim 或 add（简化：只记录现有仓位偏差，不做实际计算）
    kept_value = 0.0
    for sym in kept_syms:
        prev_close = close_on_signal.get(sym, float("nan")) if hasattr(close_on_signal, "get") else float("nan")
        prev_val   = equity / len(prev_set) if prev_set else 0
        curr_qty   = max(1, int(prev_val / (prev_close * GAP_BUFFER))) if prev_close > 0 else 0
        target_qty = max(1, int(target_val / (prev_close * GAP_BUFFER))) if prev_close > 0 else 0
        curr_val   = curr_qty * prev_close
        kept_value += curr_val
        delta_qty  = target_qty - curr_qty
        exec_open  = open_on_next.get(sym, float("nan")) if hasattr(open_on_next, "get") else float("nan")
        gap        = (exec_open / prev_close - 1) if (prev_close > 0 and not np.isnan(exec_open)) else float("nan")
        dvol       = dollar_vol_ma.get(sym, 0)
        abs_delta  = abs(delta_qty)
        impact     = (abs_delta * prev_close / dvol) if dvol > 0 else float("nan")
        flags = []
        if not np.isnan(gap) and abs(gap) > GAP_WARN:
            flags.append(f"GAP{gap:+.1%}")
        if not np.isnan(impact) and impact > IMPACT_WARN:
            flags.append(f"IMPACT{impact:.2%}")
        daily_vol = vol_on_next.get(sym, 0)
        if daily_vol < HALT_THRESH:
            flags.append("HALT?")
        if delta_qty < 0:
            action = "TRIM"
            total_sell += abs_delta * (exec_open if not np.isnan(exec_open) else prev_close)
        elif delta_qty > 0:
            action = "ADD"
            total_buy += abs_delta * (exec_open if not np.isnan(exec_open) else prev_close)
        else:
            action = "HOLD"
        orders.append({
            "action": action,
            "symbol": sym,
            "qty_est": delta_qty,
            "ref_close": round(prev_close, 4) if not np.isnan(prev_close) else None,
            "exec_open": round(exec_open, 4) if not np.isnan(exec_open) else None,
            "sell_val": round(abs_delta * (exec_open if not np.isnan(exec_open) else prev_close), 2) if delta_qty < 0 else None,
            "buy_val": round(abs_delta * (exec_open if not np.isnan(exec_open) else prev_close), 2) if delta_qty > 0 else None,
            "open_gap_pct": round(gap * 100, 2) if not np.isnan(gap) else None,
            "impact_pct": round(impact * 100, 4) if not np.isnan(impact) else None,
            "flags": flags,
        })

    # 新建仓位
    for sym in new_syms:
        ref_close = close_on_signal.get(sym, float("nan")) if hasattr(close_on_signal, "get") else float("nan")
        exec_open = open_on_next.get(sym, float("nan")) if hasattr(open_on_next, "get") else float("nan")
        dvol      = dollar_vol_ma.get(sym, 0)
        if np.isnan(ref_close) or ref_close <= 0:
            orders.append({"action": "BUY_SKIP", "symbol": sym, "flags": ["NO_PRICE"]})
            continue
        qty       = max(1, int(target_val / (ref_close * GAP_BUFFER)))
        buy_val   = qty * (exec_open if not np.isnan(exec_open) else ref_close)
        gap       = (exec_open / ref_close - 1) if (not np.isnan(exec_open) and ref_close > 0) else float("nan")
        impact    = (qty * ref_close / dvol) if dvol > 0 else float("nan")
        flags = []
        if not np.isnan(gap) and abs(gap) > GAP_WARN:
            flags.append(f"GAP{gap:+.1%}")
        if not np.isnan(impact) and impact > IMPACT_WARN:
            flags.append(f"IMPACT{impact:.2%}")
        daily_vol = vol_on_next.get(sym, 0)
        if daily_vol < HALT_THRESH:
            flags.append("HALT?")
        total_buy += buy_val
        orders.append({
            "action": "BUY",
            "symbol": sym,
            "qty_est": qty,
            "ref_close": round(ref_close, 4),
            "exec_open": round(exec_open, 4) if not np.isnan(exec_open) else None,
            "sell_val": None,
            "buy_val": round(buy_val, 2),
            "open_gap_pct": round(gap * 100, 2) if not np.isnan(gap) else None,
            "impact_pct": round(impact * 100, 4) if not np.isnan(impact) else None,
            "flags": flags,
        })

    # 资金充裕性检查
    cash_freed      = total_sell
    cash_needed     = total_buy
    cash_available  = equity - kept_value + cash_freed  # 卖后可用
    shortfall       = max(0.0, cash_needed - cash_available)

    return orders, {
        "target_val_each": round(target_val, 2),
        "kept_value":      round(kept_value, 2),
        "total_sell":      round(total_sell, 2),
        "total_buy":       round(total_buy, 2),
        "cash_available":  round(cash_available, 2),
        "shortfall":       round(shortfall, 2),
        "cash_ok":         shortfall < 100,   # 小于$100视为正常（精度损耗）
    }


# ── 主分析循环 ────────────────────────────────────────────────────────────────
def run_audit(analyze_since: str):
    symbols, benchmarks = load_universe()
    print(f"\n加载数据 (since={DATA_WARMUP})...")
    close, open_, high, low, vol = load_panel_with_open(symbols, since=DATA_WARMUP)
    qqq_close = load_benchmark_open("QQQ", since=DATA_WARMUP)
    spy_close = load_benchmark_open("SPY", since=DATA_WARMUP)

    # 仅保留股票列（剔除 QQQ/SPY）
    stock_cols = [c for c in close.columns if c not in ("QQQ", "SPY")]
    close_ = close[stock_cols]
    open__ = open_[stock_cols]
    high_  = high[stock_cols]
    low_   = low[stock_cols]
    vol_   = vol[stock_cols]

    qqq_close = qqq_close.reindex(close.index).ffill()
    spy_close = spy_close.reindex(close.index).ffill()

    all_dates = close_.index
    data_end  = all_dates[-1].date()
    print(f"  有效标的: {len(stock_cols)} 只  {all_dates[0].date()} ～ {data_end}")

    # 分析区间
    analyze_start = pd.Timestamp(analyze_since)
    analyze_dates = all_dates[all_dates >= analyze_start]
    if len(analyze_dates) == 0:
        print(f"  ❌ analyze_since={analyze_since} 超出数据范围，退出")
        return

    print(f"  分析区间: {analyze_dates[0].date()} ～ {data_end}")

    # 20日均美元成交量（流动性参考，提前算好）
    dollar_vol    = close_ * vol_
    dollar_vol_ma = dollar_vol.rolling(20).mean()

    rebalance_records = []
    prev_holdings = []
    rebalance_counter = 0  # 在全历史时间线上计数（确保5日节律与 alpaca_trader 一致）

    # 全历史时间线遍历，但只保存 analyze_since 之后的调仓记录
    for i, date in enumerate(all_dates):
        if i == 0:
            continue

        if i % REBALANCE_DAYS != 0:
            continue

        rebalance_counter += 1
        signal_date = date
        signal_idx  = i

        # 下单日：信号日的下一个交易日
        if i + 1 >= len(all_dates):
            continue   # 数据末尾，没有下单日
        order_date = all_dates[i + 1]

        # 只记录 analyze_since 以后的结果
        if signal_date < analyze_start:
            # 仍需计算持仓（为了 prev_holdings 的连续性）
            try:
                top_syms, regime_str, _, _, _ = compute_signals_on_date(
                    close_, high_, low_, vol_, qqq_close, signal_idx
                )
                prev_holdings = top_syms
            except Exception:
                pass
            continue

        # ── 计算信号 ────────────────────────────────────────────────────────
        try:
            top_syms, regime_str, candidates, qqq_last, qqq_ma50 = compute_signals_on_date(
                close_, high_, low_, vol_, qqq_close, signal_idx
            )
        except Exception as e:
            rebalance_records.append({
                "signal_date": signal_date.date(),
                "order_date":  order_date.date(),
                "regime":      "ERROR",
                "n_candidates": 0,
                "top_syms":    [],
                "orders":      [],
                "cash_summary":{},
                "error":       str(e),
            })
            prev_holdings = []
            continue

        # ── 计算订单 ────────────────────────────────────────────────────────
        ref_close   = close_.loc[signal_date]
        exec_open   = open__.loc[order_date] if order_date in open__.index else pd.Series(dtype=float)
        exec_vol    = vol_.loc[order_date]   if order_date in vol_.index   else pd.Series(dtype=float)
        dvol_ma     = dollar_vol_ma.loc[signal_date] if signal_date in dollar_vol_ma.index else pd.Series(dtype=float)

        orders, cash_summary = compute_orders(
            top_syms or [],
            prev_holdings,
            ref_close.to_dict(),
            exec_open.to_dict(),
            exec_vol.to_dict(),
            dvol_ma.to_dict(),
            EQUITY, TOP_N,
        )

        rebalance_records.append({
            "signal_date":  signal_date.date(),
            "order_date":   order_date.date(),
            "regime":       regime_str,
            "qqq":          round(qqq_last, 2),
            "qqq_ma50":     round(qqq_ma50, 2),
            "n_candidates": len(candidates),
            "top_syms":     top_syms,
            "orders":       orders,
            "cash_summary": cash_summary,
            "error":        None,
        })

        prev_holdings = top_syms

    return rebalance_records, data_end


# ── 报告输出 ──────────────────────────────────────────────────────────────────
def print_report(records, label: str):
    SEP = "=" * 72
    print(f"\n{SEP}")
    print(f"  {label}")
    print(SEP)

    total_days    = len(records)
    days_no_cand  = sum(1 for r in records if not r.get("top_syms"))
    days_error    = sum(1 for r in records if r.get("error"))
    days_cash_bad = sum(1 for r in records if not r.get("cash_summary", {}).get("cash_ok", True))
    all_orders    = [o for r in records for o in r.get("orders", [])]
    gap_warns     = [o for o in all_orders if any("GAP" in f for f in (o.get("flags") or []))]
    impact_warns  = [o for o in all_orders if any("IMPACT" in f for f in (o.get("flags") or []))]
    halt_warns    = [o for o in all_orders if any("HALT" in f for f in (o.get("flags") or []))]

    print(f"\n  总调仓日: {total_days}")
    print(f"  无候选股（NO_CANDIDATE）: {days_no_cand}  ({days_no_cand/total_days:.0%})")
    print(f"  有效调仓（有候选股）    : {total_days - days_no_cand - days_error}")
    print(f"  计算出错                : {days_error}")
    print(f"  资金不足预警            : {days_cash_bad}")
    print(f"  跳空预警 (gap>3%)       : {len(gap_warns)}")
    print(f"  市场冲击预警 (>1% dvol) : {len(impact_warns)}")
    print(f"  停牌/异常预警           : {len(halt_warns)}")

    print(f"\n{'─'*72}")
    print(f"  逐次调仓详情")
    print(f"{'─'*72}")

    for r in records:
        sig_d  = r["signal_date"]
        ord_d  = r["order_date"]
        regime = r.get("regime", "?")
        n_cand = r.get("n_candidates", 0)
        syms   = r.get("top_syms", [])
        cs     = r.get("cash_summary", {})
        error  = r.get("error")

        status = "✅" if syms and cs.get("cash_ok", True) else ("⚠️" if syms else "○")
        if error:
            status = "❌"

        header = f"\n  [{sig_d}→{ord_d}] {status} {regime}  QQQ={r.get('qqq','?')} MA50={r.get('qqq_ma50','?')}  候选={n_cand}"
        print(header)

        if error:
            print(f"    ❌ 错误: {error}")
            continue

        if not syms:
            print(f"    ○ 无候选股（双重过滤后为空），保持原仓位不动")
            continue

        target_val = cs.get("target_val_each", 0)
        print(f"    目标: {syms}  每仓≈${target_val:,.0f}")
        print(f"    资金: 卖出释放=${cs.get('total_sell',0):>9,.0f}  买入需求=${cs.get('total_buy',0):>9,.0f}  "
              f"缺口=${cs.get('shortfall',0):>6,.0f}  {'✅OK' if cs.get('cash_ok') else '❌CASH_SHORT'}")

        for o in r.get("orders", []):
            action  = o.get("action", "?")
            sym     = o.get("symbol", "?")
            qty     = o.get("qty_est", 0)
            ref_c   = o.get("ref_close")
            exec_o  = o.get("exec_open")
            gap_pct = o.get("open_gap_pct")
            impact  = o.get("impact_pct")
            bv      = o.get("buy_val")
            sv      = o.get("sell_val")
            flags   = o.get("flags") or []

            val_str = f"${bv:,.0f}" if bv else (f"-${sv:,.0f}" if sv else "")
            gap_str = f"  gap={gap_pct:+.1f}%" if gap_pct is not None else ""
            imp_str = f"  impact={impact:.3f}%" if impact is not None else ""
            flag_str= f"  ⚠️ {' '.join(flags)}" if flags else ""

            if action in ("HOLD",):
                print(f"      HOLD   {sym:8s}  不变")
            elif action == "BUY_SKIP":
                print(f"      SKIP   {sym:8s}  无价格数据")
            else:
                qty_str = f"×{abs(qty)}" if qty else ""
                print(f"      {action:10s} {sym:8s} {qty_str:>5}  ref_close=${ref_c}  exec_open=${exec_o}  {val_str}{gap_str}{imp_str}{flag_str}")

    # ── 问题汇总 ────────────────────────────────────────────────────────────
    if gap_warns or impact_warns or halt_warns or days_cash_bad:
        print(f"\n{'─'*72}")
        print(f"  ⚠️  问题汇总")
        print(f"{'─'*72}")
        if days_cash_bad:
            for r in records:
                cs = r.get("cash_summary", {})
                if not cs.get("cash_ok", True):
                    print(f"    CASH_SHORT  {r['signal_date']}  缺口=${cs.get('shortfall',0):,.0f}")
        if gap_warns:
            for o in gap_warns:
                print(f"    GAP_WARN    {o['symbol']:8s}  {o['open_gap_pct']:+.1f}%  exec_open=${o['exec_open']}")
        if impact_warns:
            for o in impact_warns:
                print(f"    IMPACT_WARN {o['symbol']:8s}  {o['impact_pct']:.3f}%")
        if halt_warns:
            for o in halt_warns:
                print(f"    HALT?       {o['symbol']:8s}")
    else:
        print(f"\n  ✅ 无可行性问题")

    print()


# ── 入口 ─────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="调仓历史成交可行性审计")
    parser.add_argument("--since", default="2025-06-09",
                        help="1年回溯起点（默认 2025-06-09）")
    args = parser.parse_args()

    # 第1轮：部署期（约上上个星期开始）
    deploy_since = "2026-06-09"

    print("=" * 72)
    print("  Trade Quant 调仓历史 & 成交可行性审计")
    print("=" * 72)
    print(f"  账户净值: ${EQUITY:,}  Top-N: {TOP_N}  调仓周期: {REBALANCE_DAYS}日")
    print(f"  最小分数: {MIN_SCORE}  最小成交量倍数: {VOL_MIN}")

    records, data_end = run_audit(analyze_since=args.since)

    # 拆成两段输出
    deploy_records = [r for r in records if str(r["signal_date"]) >= deploy_since]
    year_records   = records   # 全部

    print_report(deploy_records, f"[Part 1] 部署期 {deploy_since} ～ {data_end}")
    print_report(year_records,   f"[Part 2] 1年回溯 {args.since} ～ {data_end}")


if __name__ == "__main__":
    main()
