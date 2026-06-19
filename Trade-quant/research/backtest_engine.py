"""
策略回测引擎（状态机版）。

与 QuantDinger ScriptStrategy 逻辑完全对齐：
  - 持仓时才能止损/止盈，空仓时只能入场
  - ATR 动态止损固定在建仓时
  - 分层止盈（最多 3 层）
  - 加仓只在盈利 0-1% 时
  - 入场：VWAP 回踩收复 或 MACD 动量
  - 建议在 1H 数据上运行（日内 T 交易）

用法:
    python backtest_engine.py --symbol NVDA --tf 1h
    python backtest_engine.py --symbol NVDA --tf 1h --since 2025-09-01 --log-limit 50
"""
import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

DATA_DIR = Path(__file__).parent.parent / "data"


# ── 数据加载 ───────────────────────────────────────────────────────────────

def load_raw(symbol: str, timeframe: str) -> pd.DataFrame:
    path = DATA_DIR / f"{symbol.lower()}_{timeframe}_raw.parquet"
    if not path.exists():
        raise FileNotFoundError(f"先运行 fetch_ohlcv.py: {path}")
    return pd.read_parquet(path)


# ── 指标计算（与 ScriptStrategy 内联版对齐）────────────────────────────────

def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def atr_pct_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_c = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_c).abs(),
        (df["low"]  - prev_c).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    return atr / df["close"]


def kdj(df: pd.DataFrame, period: int = 9, smooth: int = 3) -> pd.DataFrame:
    alpha = 1.0 / smooth
    lo = df["low"].rolling(period).min()
    hi = df["high"].rolling(period).max()
    rsv = ((df["close"] - lo) / (hi - lo).replace(0, np.nan) * 100).fillna(50)
    k_vals, d_vals = [], []
    k = d = 50.0
    for i, v in enumerate(rsv):
        if i >= period - 1:
            k = (1 - alpha) * k + alpha * v
            d = (1 - alpha) * d + alpha * k
        k_vals.append(k if i >= period - 1 else np.nan)
        d_vals.append(d if i >= period - 1 else np.nan)
    ks = pd.Series(k_vals, index=df.index)
    ds = pd.Series(d_vals, index=df.index)
    return pd.DataFrame({"K": ks, "D": ds, "J": 3 * ks - 2 * ds})


def session_vwap(df: pd.DataFrame, session_hour_utc: int = 13) -> pd.Series:
    """每天 session_hour_utc:30 UTC 重置的 VWAP（1H粒度）。"""
    typical = (df["high"] + df["low"] + df["close"]) / 3
    tp_vol  = typical * df["volume"]

    # 每 bar 所属的 session 起点（美股 13:30 UTC ≈ 13H）
    def _session_key(t):
        day_session = t.normalize() + pd.Timedelta(hours=session_hour_utc)
        return day_session if t >= day_session else day_session - pd.Timedelta(days=1)

    sessions = df.index.map(_session_key)

    cum_tpv = tp_vol.groupby(sessions).cumsum()
    cum_vol  = df["volume"].groupby(sessions).cumsum()
    return (cum_tpv / cum_vol.replace(0, np.nan)).rename("VWAP")


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["MA5"]  = sma(df["close"], 5)
    out["MA10"] = sma(df["close"], 10)
    out["MA20"] = sma(df["close"], 20)
    out["ATR_pct"] = atr_pct_wilder(df, 14)
    out["VWAP"] = session_vwap(df, session_hour_utc=13)
    kdj_df = kdj(df)
    out["KDJ_K"] = kdj_df["K"]
    out["KDJ_D"] = kdj_df["D"]
    out["KDJ_J"] = kdj_df["J"]
    ema12 = df["close"].ewm(span=12, min_periods=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, min_periods=26, adjust=False).mean()
    out["MACD_line"] = ema12 - ema26
    out["MACD_hist"] = out["MACD_line"] - out["MACD_line"].ewm(span=9, adjust=False).mean()
    out["vol_avg20"] = df["volume"].rolling(20).mean()
    out["vol_ratio"] = df["volume"] / out["vol_avg20"]
    return out.dropna(subset=["MA20", "KDJ_J"])


# ── 策略参数 ───────────────────────────────────────────────────────────────

@dataclass
class StrategyConfig:
    min_atr_pct:    float = 0.003
    atr_sl_mult:    float = 1.5
    vol_mult:       float = 1.5
    tp1:            float = 0.018
    tp2:            float = 0.014
    tp3:            float = 0.010
    max_layers:     int   = 3
    vwap_buffer:    float = 0.001
    initial_capital: float = 10_000.0


# ── 持仓状态 ───────────────────────────────────────────────────────────────

@dataclass
class Position:
    size:         float = 0.0    # 持仓数量（股/单位）
    entry_price:  float = 0.0    # 加权平均成本
    stop_pct:     float = 0.0    # 建仓时锁定的止损幅度
    layers:       int   = 0      # 当前层数

    @property
    def is_open(self) -> bool:
        return self.size > 0


# ── 交易记录 ───────────────────────────────────────────────────────────────

@dataclass
class Trade:
    time:       object
    action:     str     # BUY / SELL / STOP / TP
    price:      float
    size:       float
    reason:     str
    pnl_pct:    Optional[float] = None   # None for entries
    layers:     int = 0
    equity:     float = 0.0


# ── 回测引擎 ───────────────────────────────────────────────────────────────

def run_backtest(df: pd.DataFrame, cfg: StrategyConfig) -> tuple[list[Trade], pd.Series]:
    """
    逐 bar 状态机回测。返回 (trades, equity_series)。
    """
    pos     = Position()
    cash    = cfg.initial_capital
    trades: list[Trade] = []
    equity  = []

    for i in range(1, len(df)):
        bar  = df.iloc[i]
        prev = df.iloc[i - 1]
        close  = bar["close"]
        t      = bar.name

        # 当前净值
        cur_equity = cash + pos.size * close
        equity.append(cur_equity)

        # 跳过指标未就绪的 bar
        if pd.isna(bar["KDJ_J"]) or pd.isna(bar["VWAP"]):
            continue

        atr    = bar["ATR_pct"]
        vwap   = bar["VWAP"]
        j      = bar["KDJ_J"]
        macd_h = bar["MACD_hist"]
        macd_l = bar["MACD_line"]
        vol_r  = bar["vol_ratio"]

        ma5, ma10, ma20 = bar["MA5"], bar["MA10"], bar["MA20"]

        if ma5 > ma10 > ma20:
            trend = "up"
        elif close < ma20 * 0.99:
            trend = "broken"
        else:
            trend = "neutral"

        vol_spike    = vol_r > cfg.vol_mult
        vwap_reclaim = prev["low"] < vwap and close > vwap
        macd_pos     = (macd_l > 0) and (macd_h > 0)
        j_ok         = j < 70
        above_vwap   = close > vwap * (1 - cfg.vwap_buffer)
        atr_ok       = (not pd.isna(atr)) and atr >= cfg.min_atr_pct

        # ── 持仓管理 ─────────────────────────────────────────────────────
        if pos.is_open:
            pnl = (close - pos.entry_price) / pos.entry_price

            # 止损
            if pnl <= -pos.stop_pct:
                proceeds = pos.size * close
                cash += proceeds
                trades.append(Trade(
                    time=t, action="STOP", price=close, size=pos.size,
                    reason=f"ATR止损 pnl={pnl:.2%} <= -{pos.stop_pct:.2%}",
                    pnl_pct=pnl, layers=pos.layers, equity=cash
                ))
                pos = Position()
                continue

            # 止盈（全仓平出，层数清零）
            tp_targets = [cfg.tp1, cfg.tp2, cfg.tp3]
            tp = tp_targets[min(pos.layers - 1, 2)]
            if pnl >= tp:
                proceeds = pos.size * close
                cash += proceeds
                trades.append(Trade(
                    time=t, action="TP", price=close, size=pos.size,
                    reason=f"止盈 layer={pos.layers} tp={tp:.1%} pnl={pnl:.2%}",
                    pnl_pct=pnl, layers=pos.layers, equity=cash
                ))
                pos = Position()
                continue

            # 加仓（盈利 0–1%，顺势）
            if (pos.layers < cfg.max_layers and trend == "up"
                    and vol_spike and 0 < pnl < 0.01 and atr_ok):
                add_size = (cur_equity * 0.05) / close   # 每次加 5% 资金
                if cash >= add_size * close:
                    total_cost = pos.entry_price * pos.size + close * add_size
                    pos.size  += add_size
                    pos.entry_price = total_cost / pos.size
                    pos.layers += 1
                    cash -= add_size * close
                    trades.append(Trade(
                        time=t, action="ADD", price=close, size=add_size,
                        reason=f"加仓 layer={pos.layers} pnl={pnl:.2%}",
                        layers=pos.layers, equity=cash + pos.size * close
                    ))
            continue

        # ── 入场（空仓）────────────────────────────────────────────────
        if trend != "up" or not above_vwap or not atr_ok:
            continue

        reason = ""
        if vwap_reclaim and j_ok and vol_spike:
            reason = f"VWAP回踩收复 J={j:.1f} vol_ratio={vol_r:.2f}"
        elif macd_pos and j_ok and vol_spike and close > prev["close"] * 1.002:
            reason = f"MACD动量 J={j:.1f} vol_ratio={vol_r:.2f}"
        else:
            continue

        buy_size = (cfg.initial_capital * 0.20) / close   # 每次用 20% 资金
        if cash < buy_size * close:
            continue

        stop_pct = atr * cfg.atr_sl_mult if not pd.isna(atr) else 0.035
        pos = Position(size=buy_size, entry_price=close,
                       stop_pct=stop_pct, layers=1)
        cash -= buy_size * close
        trades.append(Trade(
            time=t, action="BUY", price=close, size=buy_size,
            reason=reason, layers=1,
            equity=cash + pos.size * close
        ))

    # 最后一 bar：强制平仓
    if pos.is_open:
        last_close = df.iloc[-1]["close"]
        pnl = (last_close - pos.entry_price) / pos.entry_price
        cash += pos.size * last_close
        trades.append(Trade(
            time=df.index[-1], action="EOD_CLOSE", price=last_close, size=pos.size,
            reason="回测结束强制平仓", pnl_pct=pnl, layers=pos.layers, equity=cash
        ))

    eq_series = pd.Series(equity, index=df.index[1:])
    return trades, eq_series


# ── 日志输出 ───────────────────────────────────────────────────────────────

def print_trade_log(trades: list[Trade], limit: int = 60):
    entries = [t for t in trades if t.action == "BUY"]
    exits   = [t for t in trades if t.action in ("TP", "STOP", "EOD_CLOSE")]
    adds    = [t for t in trades if t.action == "ADD"]

    print(f"\n{'='*70}")
    print(f"  交易日志  共 {len(trades)} 条  "
          f"[入场 {len(entries)} | 加仓 {len(adds)} | 出场 {len(exits)}]")
    print(f"{'='*70}")

    shown = trades[:limit]
    for t in shown:
        pnl_str = f"  pnl={t.pnl_pct:+.2%}" if t.pnl_pct is not None else ""
        time_str = str(t.time)[:16]
        print(f"  [{time_str}] {t.action:<10} ${t.price:.2f}  "
              f"层={t.layers}{pnl_str}  |  {t.reason}")

    if len(trades) > limit:
        print(f"  ... 省略 {len(trades) - limit} 条 (用 --log-limit 调大)")
    print(f"{'='*70}\n")


def print_summary(trades: list[Trade], initial: float):
    entries = [t for t in trades if t.action == "BUY"]
    exits   = [t for t in trades if t.action in ("TP", "STOP", "EOD_CLOSE")]
    stops   = [t for t in trades if t.action == "STOP"]
    tps     = [t for t in trades if t.action == "TP"]

    pnls = [t.pnl_pct for t in exits if t.pnl_pct is not None]
    win_rate = sum(1 for p in pnls if p > 0) / len(pnls) * 100 if pnls else 0

    # T 交易：入场和出场在同一天
    t_trades = 0
    for e in entries:
        for x in exits:
            if (x.time > e.time and
                    pd.Timestamp(x.time).date() == pd.Timestamp(e.time).date()):
                t_trades += 1
                break

    final_equity = trades[-1].equity if trades else initial
    total_ret    = (final_equity / initial - 1) * 100

    print(f"  总收益:    {total_ret:+.1f}%  (${initial:.0f} → ${final_equity:.0f})")
    print(f"  入场次数:  {len(entries)}")
    print(f"  出场次数:  {len(exits)}  (止盈 {len(tps)} | 止损 {len(stops)})")
    print(f"  胜率:      {win_rate:.1f}%")
    print(f"  日内T交易: {t_trades} 笔  (当天买当天卖)")
    print()


# ── main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol",    default="NVDA")
    parser.add_argument("--tf",        default="1h")
    parser.add_argument("--since",     default=None)
    parser.add_argument("--until",     default=None)
    parser.add_argument("--log-limit", type=int, default=80,
                        help="最多打印多少条日志（默认80）")
    args = parser.parse_args()

    print(f"\n加载数据: {args.symbol} {args.tf} ...")
    raw = load_raw(args.symbol, args.tf)

    if args.since:
        raw = raw.loc[args.since:]
    if args.until:
        raw = raw.loc[:args.until]

    print(f"共 {len(raw)} 根 K 线  {raw.index[0]}  ~  {raw.index[-1]}")

    print("计算指标 ...")
    df = compute_indicators(raw)
    print(f"有效 bar 数（指标就绪）: {len(df)}")

    cfg    = StrategyConfig()
    trades, equity = run_backtest(df, cfg)

    print_trade_log(trades, limit=args.log_limit)
    print("  === 汇总 ===")
    print_summary(trades, cfg.initial_capital)


if __name__ == "__main__":
    main()
