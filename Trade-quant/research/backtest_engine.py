"""
策略回测引擎（核心仓 + T仓 双轨版）。

核心仓（70% 资金）：MA 趋势建仓，MA20 破位止损，不做日内止盈。
T  仓（20% 资金）：VWAP/MACD 信号入场，ATR 止损，分层止盈。

双参数档位：
  --stock-type bluechip   → vol_mult=1.2（蓝筹，成交量放大门槛低）
  --stock-type volatile   → vol_mult=1.5（高波动，需更明显的量能）

用法:
    python backtest_engine.py --symbol NVDA --tf 1h
    python backtest_engine.py --symbol NVDA --tf 1h --stock-type volatile --since 2025-01-01
"""
import argparse
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


# ── 指标计算 ───────────────────────────────────────────────────────────────

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

    def _session_key(t):
        day_session = t.normalize() + pd.Timedelta(hours=session_hour_utc)
        return day_session if t >= day_session else day_session - pd.Timedelta(days=1)

    sessions = df.index.map(_session_key)
    cum_tpv = tp_vol.groupby(sessions).cumsum()
    cum_vol  = df["volume"].groupby(sessions).cumsum()
    return (cum_tpv / cum_vol.replace(0, np.nan)).rename("VWAP")


def add_daily_weekly_trend(out: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """
    日线宏观趋势。移位1天避免前视偏差（昨日日线信号 → 今日决策）。

    存入各档日线MA值供 run_backtest 按 cfg.d_trend_min_mas 动态计算多空判断：
      3 = close>MA5 & MA5>MA10>MA20
      4 = 3 + MA20>MA30
      5 = 4 + MA30>MA60
      6 = 5 + MA60>MA250（MA250无数据时跳过）

    日线空头：MA5 < MA10 且 MA5 较前日下行（正常回踩支撑不算空头）
    """
    try:
        daily = df[["close"]].resample("1D").agg({"close": "last"}).dropna()
        daily["d_close"] = daily["close"]
        daily["d_ma5"]   = daily["close"].rolling(5,   min_periods=4).mean()
        daily["d_ma10"]  = daily["close"].rolling(10,  min_periods=8).mean()
        daily["d_ma20"]  = daily["close"].rolling(20,  min_periods=15).mean()
        daily["d_ma30"]  = daily["close"].rolling(30,  min_periods=20).mean()
        daily["d_ma60"]  = daily["close"].rolling(60,  min_periods=40).mean()
        daily["d_ma250"] = daily["close"].rolling(250, min_periods=200).mean()

        # 日线空头：MA5 下穿 MA10 且 MA5 本身在下行
        daily["d_trend_dn"] = (
            (daily["d_ma5"] < daily["d_ma10"]) &
            (daily["d_ma5"] < daily["d_ma5"].shift(1))
        )

        sig_cols = ["d_close", "d_ma5", "d_ma10", "d_ma20",
                    "d_ma30", "d_ma60", "d_ma250", "d_trend_dn"]
        daily_sig = daily[sig_cols].copy()
        daily_sig.index = daily_sig.index + pd.Timedelta(days=1)
        daily_sig = daily_sig.reindex(out.index, method="ffill")

        for col in ["d_close", "d_ma5", "d_ma10", "d_ma20",
                    "d_ma30", "d_ma60", "d_ma250"]:
            out[col] = daily_sig[col]

        # backward-compat aliases
        out["d_ma20_col"] = daily_sig["d_ma20"].fillna(out["MA20"])
        out["d_ma50"]     = daily_sig["d_ma60"].fillna(out["MA50"])
        out["d_trend_dn"] = daily_sig["d_trend_dn"].fillna(False).astype(bool)
    except Exception:
        for col in ["d_close", "d_ma5", "d_ma10", "d_ma20",
                    "d_ma30", "d_ma60", "d_ma250"]:
            out[col] = np.nan
        out["d_trend_dn"] = False
        out["d_ma20_col"] = out["MA20"]
        out["d_ma50"]     = out["MA50"]

    return out


def add_weekly_monthly_trend(out: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """
    周K/月K宏观趋势过滤。
    周线空头：W_MA5 < W_MA10 且 W_MA5 较前周下行
    月线空头：M_MA3 < M_MA5（月线MA本身已很平滑，无需额外下行判断）
    均移位1周/1月避免前视偏差。
    """
    try:
        weekly = df[["close"]].resample("W").agg({"close": "last"}).dropna()
        weekly["w_ma5"]  = weekly["close"].rolling(5,  min_periods=3).mean()
        weekly["w_ma10"] = weekly["close"].rolling(10, min_periods=6).mean()
        weekly["w_trend_dn"] = (
            (weekly["w_ma5"] < weekly["w_ma10"]) &
            (weekly["w_ma5"] < weekly["w_ma5"].shift(1))
        )
        weekly_sig = weekly[["w_trend_dn"]].copy()
        weekly_sig.index = weekly_sig.index + pd.Timedelta(weeks=1)
        out["w_trend_dn"] = weekly_sig["w_trend_dn"].reindex(out.index, method="ffill").fillna(False).astype(bool)
    except Exception:
        out["w_trend_dn"] = False

    try:
        monthly = df[["close"]].resample("ME").agg({"close": "last"}).dropna()
        monthly["m_ma3"] = monthly["close"].rolling(3, min_periods=2).mean()
        monthly["m_ma5"] = monthly["close"].rolling(5, min_periods=3).mean()
        monthly["m_trend_dn"] = (
            monthly["m_ma3"] < monthly["m_ma5"]
        )
        monthly_sig = monthly[["m_trend_dn"]].copy()
        monthly_sig.index = monthly_sig.index + pd.DateOffset(months=1)
        out["m_trend_dn"] = monthly_sig["m_trend_dn"].reindex(out.index, method="ffill").fillna(False).astype(bool)
    except Exception:
        out["m_trend_dn"] = False

    return out


def add_2h_indicators(out: pd.DataFrame, df: pd.DataFrame) -> pd.DataFrame:
    """
    2H级别技术指标，用于核心仓入场信号。
    移位1根2H K线避免前视偏差（上一根完成的2H K线 → 当前决策）。

    入场逻辑：
      信号根：2H K线低点触及 MA10/MA20/MA30 其中之一，且收盘收回该均线之上
      确认根：紧接的下一根2H K线收盘继续站在该均线之上
    """
    try:
        df_2h = df[["open", "high", "low", "close", "volume"]].resample("2h").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"
        }).dropna()

        df_2h["h2_ma5"]  = df_2h["close"].rolling(5,  min_periods=4).mean()
        df_2h["h2_ma10"] = df_2h["close"].rolling(10, min_periods=8).mean()
        df_2h["h2_ma20"] = df_2h["close"].rolling(20, min_periods=15).mean()
        df_2h["h2_ma30"] = df_2h["close"].rolling(30, min_periods=20).mean()

        # 触及支撑后收回：低点 <= MA 且 收盘 > MA
        for n in [10, 20, 30]:
            ma = df_2h[f"h2_ma{n}"]
            df_2h[f"h2_touch{n}"] = (df_2h["low"] <= ma) & (df_2h["close"] > ma)

        df_2h["h2_touch"] = (
            df_2h["h2_touch10"] | df_2h["h2_touch20"] | df_2h["h2_touch30"]
        )

        # 收盘在任意支撑之上（第二根确认用）
        df_2h["h2_above"] = (
            df_2h["close"].gt(df_2h["h2_ma10"].fillna(0)) |
            df_2h["close"].gt(df_2h["h2_ma20"].fillna(0)) |
            df_2h["close"].gt(df_2h["h2_ma30"].fillna(0))
        )

        # T仓出场：收盘跌破2H MA5
        df_2h["h2_below_ma5"] = df_2h["close"] < df_2h["h2_ma5"].fillna(0)

        sig_cols = ["h2_ma5", "h2_ma10", "h2_ma20", "h2_ma30",
                    "h2_touch", "h2_above", "h2_below_ma5"]
        # 移位1根2H K线：使用上一根已完成的2H K线信号
        sig = df_2h[sig_cols].shift(1)
        sig_ff = sig.reindex(out.index, method="ffill")

        out["h2_ma5"]       = sig_ff["h2_ma5"].fillna(out["MA5"])
        out["h2_ma10"]      = sig_ff["h2_ma10"].fillna(out["MA10"])
        out["h2_ma20"]      = sig_ff["h2_ma20"].fillna(out["MA20"])
        out["h2_ma30"]      = sig_ff["h2_ma30"].fillna(out["MA20"])
        out["h2_touch"]     = sig_ff["h2_touch"].fillna(False).astype(bool)
        out["h2_above"]     = sig_ff["h2_above"].fillna(False).astype(bool)
        out["h2_below_ma5"] = sig_ff["h2_below_ma5"].fillna(False).astype(bool)
    except Exception:
        out["h2_touch"]     = False
        out["h2_above"]     = False
        out["h2_below_ma5"] = False
        out["h2_ma5"]       = out["MA5"]
        out["h2_ma10"]      = out["MA10"]
        out["h2_ma20"]      = out["MA20"]
        out["h2_ma30"]      = out["MA20"]

    return out


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["MA5"]  = sma(df["close"], 5)
    out["MA10"] = sma(df["close"], 10)
    out["MA20"] = sma(df["close"], 20)
    out["MA50"] = sma(df["close"], 50)
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
    # 宏观趋势（前视安全：日线移位1天，周线移位1周，月线移位1月）
    out = add_daily_weekly_trend(out, df)
    out = add_weekly_monthly_trend(out, df)
    # 2H入场信号（前视安全：移位1根2H K线）
    out = add_2h_indicators(out, df)
    return out.dropna(subset=["MA20", "MA50", "KDJ_J"])


# ── 策略参数 ───────────────────────────────────────────────────────────────

@dataclass
class StrategyConfig:
    # 波动等级：small_vol（低波动）| large_vol（高波动）
    # small_vol：NVDA/TSLA/AAPL/MSFT/小米等，周期性回调明显，适合浮盈保护
    # large_vol：MU/BTC/ETH/韩股等，趋势延伸极强，止损容易被踢出主升浪
    stock_type:           str   = "small_vol"
    small_vol_vol_mult:   float = 1.2   # 低波动：成交量放大1.2x即触发
    large_vol_vol_mult:   float = 1.5   # 高波动：需要1.5x量能确认
    # 共用参数
    min_atr_pct:          float = 0.003
    atr_sl_mult:          float = 1.5
    tp1:                  float = 0.018
    tp2:                  float = 0.014
    tp3:                  float = 0.010
    max_t_layers:         int   = 3
    vwap_buffer:          float = 0.001
    commission_pct:       float = 0.001   # T仓单边手续费0.1%
    initial_capital:      float = 10_000.0
    # 仓位比例（低波动核心仓80%，高波动核心仓70%）
    small_vol_core_pct:   float = 0.80
    large_vol_core_pct:   float = 0.70
    t_pct:                float = 0.20
    # 核心仓入场
    core_entry_h2bars:    int   = 2
    # 日线多头过滤强度：3=MA5>10>20  4=+30  5=+60  6=+250
    d_trend_min_mas:      int   = 4
    # T仓出场：连续几根2H收盘跌破MA5
    t_exit_h2_break:      int   = 2
    # small_vol 专属止损（large_vol 不启用：超级趋势股中途震仓会被踢出主升浪）
    # 1) 浮盈归零保护：曾盈利超 core_profit_lock，后跌回建仓成本 → 清仓
    # 2) 早期亏损止损：建仓后 core_early_loss_window 根1H bar 内亏损≥core_early_loss → 清仓
    #    窗口后不再触发，避免牛市正常回调被踢出
    core_profit_lock:          float = 0.03   # 触发浮盈归零保护所需最低历史盈利
    core_early_loss:           float = 0.03   # 早期止损阈值（3%）
    core_early_loss_window:    int   = 20     # 早期止损有效窗口（根1H bar，≈3个交易日）

    @property
    def core_pct(self) -> float:
        return self.small_vol_core_pct if self.stock_type == "small_vol" else self.large_vol_core_pct

    @property
    def vol_mult(self) -> float:
        return self.small_vol_vol_mult if self.stock_type == "small_vol" else self.large_vol_vol_mult


# ── 持仓状态 ───────────────────────────────────────────────────────────────

@dataclass
class Position:
    size:        float = 0.0
    entry_price: float = 0.0
    stop_pct:    float = 0.0
    layers:      int   = 0

    @property
    def is_open(self) -> bool:
        return self.size > 0


# ── 交易记录 ───────────────────────────────────────────────────────────────

@dataclass
class Trade:
    time:    object
    action:  str            # CORE_BUY/CORE_STOP/CORE_EOD | T_BUY/T_TP/T_STOP/T_ADD/T_EOD
    price:   float
    size:    float
    reason:  str
    pnl_pct: Optional[float] = None
    layers:  int = 0
    equity:  float = 0.0


# ── 回测引擎 ───────────────────────────────────────────────────────────────

def _compute_d_trend_up(bar, min_mas: int) -> bool:
    """根据 d_trend_min_mas 档位动态计算日线多头。"""
    dc   = bar.get("d_close",  np.nan)
    ma5  = bar.get("d_ma5",   np.nan)
    ma10 = bar.get("d_ma10",  np.nan)
    ma20 = bar.get("d_ma20",  np.nan)
    ma30 = bar.get("d_ma30",  np.nan)
    ma60 = bar.get("d_ma60",  np.nan)
    ma250= bar.get("d_ma250", np.nan)

    if any(pd.isna(v) for v in [dc, ma5, ma10, ma20]):
        return False

    ok = (dc > ma5) and (ma5 > ma10) and (ma10 > ma20)  # 档位3（基础）
    if not ok or min_mas <= 3:
        return ok
    if pd.isna(ma30):
        return ok
    ok = ok and (ma20 > ma30)                             # 档位4
    if not ok or min_mas <= 4:
        return ok
    if pd.isna(ma60):
        return ok
    ok = ok and (ma30 > ma60)                             # 档位5
    if not ok or min_mas <= 5:
        return ok
    if pd.isna(ma250):
        return ok                                          # 档位6：MA250无数据时跳过
    return ok and (ma60 > ma250)                          # 档位6


def run_backtest(df: pd.DataFrame, cfg: StrategyConfig) -> tuple[list[Trade], pd.Series]:
    """
    核心仓：日线多头(d_trend_min_mas档) + 2H K线双根确认建仓，建仓后不主动减仓。
    T仓：2H触及支撑信号 + MACD/KDJ/量能辅助确认入场；
         2H连续 t_exit_h2_break 根收盘跌破2H MA5 出场（ATR止损保底）。
    """
    core_pos           = Position()
    core_h2_count      = 0    # 0=无信号 1=第1根2H触及支撑 2=第2根2H站稳(可建仓)
    core_hold_bars     = 0    # 建仓后累计1H bar数，用于早期亏损止损窗口
    portfolio_peak     = 0.0  # 核心仓持仓以来最高总资产（整体收益率追踪）
    core_entry_equity  = 0.0  # 建仓时总资产
    prev_t             = None

    t_pos          = Position()
    t_h2_below_cnt = 0   # 连续2H收盘在 h2_ma5 下方的计数（T出场信号）
    cash           = cfg.initial_capital
    trades: list[Trade] = []
    equity         = []

    for i in range(1, len(df)):
        bar  = df.iloc[i]
        prev = df.iloc[i - 1]
        close = bar["close"]
        t     = bar.name

        cur_equity = cash + core_pos.size * close + t_pos.size * close
        equity.append(cur_equity)

        # 2H边界检测（在任何 continue 前更新 prev_t）
        is_new_h2 = (prev_t is None) or (t.floor("2h") != prev_t.floor("2h"))
        prev_t = t

        if pd.isna(bar["KDJ_J"]) or pd.isna(bar["VWAP"]):
            continue

        atr    = bar["ATR_pct"]
        vwap   = bar["VWAP"]
        j      = bar["KDJ_J"]
        k      = bar["KDJ_K"]
        d_kdj  = bar["KDJ_D"]
        macd_h = bar["MACD_hist"]
        macd_l = bar["MACD_line"]
        vol_r  = bar["vol_ratio"]
        ma5, ma10, ma20, ma50 = bar["MA5"], bar["MA10"], bar["MA20"], bar["MA50"]

        # 1H 短周期趋势
        above_ma20 = close > ma20 * 0.99
        trend_up   = ma5 > ma10 > ma20
        trend      = "up" if trend_up else ("broken" if not above_ma20 else "neutral")

        # 日线宏观趋势（按 cfg.d_trend_min_mas 档位动态判断）
        d_trend_up = _compute_d_trend_up(bar, cfg.d_trend_min_mas)

        # 2H 信号（已 shift(1) 防前视）
        h2_touch     = bool(bar.get("h2_touch",     False))
        h2_above     = bool(bar.get("h2_above",     False))
        h2_below_ma5 = bool(bar.get("h2_below_ma5", False))

        vol_spike = vol_r > cfg.vol_mult
        macd_pos  = (macd_l > 0) and (macd_h > 0)          # MACD 均在零轴上
        macd_xup  = (macd_h > 0) and (prev["MACD_hist"] <= 0)  # MACD hist 金叉
        j_ok      = j < 70                                   # KDJ 未超买
        kdj_xup   = (k > d_kdj) and (prev["KDJ_K"] <= prev["KDJ_D"])  # KDJ 金叉
        above_vwap = close > vwap * (1 - cfg.vwap_buffer)
        atr_ok     = (not pd.isna(atr)) and atr >= cfg.min_atr_pct

        # ── 2H边界：更新 T仓出场计数 ─────────────────────────────────────
        if is_new_h2:
            if t_pos.is_open:
                if h2_below_ma5:
                    t_h2_below_cnt += 1
                else:
                    t_h2_below_cnt = 0
            else:
                t_h2_below_cnt = 0

        # 空头标志：日线 MA5<MA10 且 MA5 下行
        d_trend_dn   = bool(bar.get("d_trend_dn", False))
        any_trend_dn = d_trend_dn
        t_trend_dn   = d_trend_dn

        # ══ 核心仓：2H双根信号建仓，建后不动 ════════════════════════════════
        # 入场门槛：日线非空头时等待2H信号
        if not core_pos.is_open:
            if not any_trend_dn:
                if is_new_h2:
                    if h2_touch:
                        core_h2_count = 1          # 第1根2H：触及支撑且收盘站上
                    elif core_h2_count == 1 and h2_above:
                        core_h2_count = 2          # 第2根2H：站稳 → 可建仓
                    else:
                        core_h2_count = 0

                if core_h2_count >= cfg.core_entry_h2bars and above_vwap:
                    buy_value = cfg.initial_capital * cfg.core_pct
                    buy_size  = buy_value / close
                    if cash >= buy_value:
                        core_pos          = Position(size=buy_size, entry_price=close,
                                                   stop_pct=0.0, layers=1)
                        cash             -= buy_value
                        core_h2_count     = 0
                        core_hold_bars    = 0
                        core_entry_equity = cash + core_pos.size * close + t_pos.size * close
                        portfolio_peak    = core_entry_equity
                        trades.append(Trade(
                            time=t, action="CORE_BUY", price=close, size=buy_size,
                            reason="2H双根确认+VWAP(非日线空头)",
                            equity=cash + core_pos.size * close
                        ))
            else:
                core_h2_count = 0   # 日线空头中，重置2H计数

        # ══ small_vol 专属止损 ══════════════════════════════════════════════
        # large_vol 不启用：超级趋势股中途震仓易被踢出主升浪
        if core_pos.is_open and cfg.stock_type == "small_vol":
            core_hold_bars += 1
            core_pnl        = (close - core_pos.entry_price) / core_pos.entry_price
            portfolio_peak  = max(portfolio_peak, cur_equity)
            profit_lock_ok  = (portfolio_peak >= cfg.initial_capital * (1 + cfg.core_profit_lock)
                               and cur_equity <= core_entry_equity)
            # 早期亏损止损：只在建仓初期（前 early_loss_window 根1H bar）有效
            # 避免在上升趋势的正常回调中被踢出
            in_early_window = core_hold_bars <= cfg.core_early_loss_window
            early_loss_ok   = in_early_window and core_pnl <= -cfg.core_early_loss
            if profit_lock_ok or early_loss_ok:
                cash += core_pos.size * close
                if profit_lock_ok:
                    stop_reason = (f"浮盈归零 峰值={portfolio_peak/cfg.initial_capital-1:.2%} "
                                   f"持仓pnl={core_pnl:.2%}")
                else:
                    stop_reason = f"早期亏损 建仓{core_hold_bars}bar内 持仓pnl={core_pnl:.2%}"
                trades.append(Trade(
                    time=t, action="CORE_STOP", price=close, size=core_pos.size,
                    reason=stop_reason,
                    pnl_pct=core_pnl,
                    equity=cash + t_pos.size * close
                ))
                core_pos          = Position()
                core_h2_count     = 0
                core_hold_bars    = 0
                portfolio_peak    = 0.0
                core_entry_equity = 0.0

        # ══ T仓管理（核心仓不存在时跳过）══════════════════════════════════
        if not core_pos.is_open:
            continue

        if t_pos.is_open:
            pnl = (close - t_pos.entry_price) / t_pos.entry_price

            # T仓出场：2H连续跌破MA5（主出场逻辑）
            if t_h2_below_cnt >= cfg.t_exit_h2_break:
                gross = t_pos.size * close
                net   = gross * (1 - cfg.commission_pct)
                cash += net
                trades.append(Trade(
                    time=t, action="T_STOP", price=close, size=t_pos.size,
                    reason=f"2H连续{t_h2_below_cnt}根跌破MA5 pnl={pnl:.2%}",
                    pnl_pct=pnl, layers=t_pos.layers,
                    equity=cash + core_pos.size * close
                ))
                t_pos = Position()
                t_h2_below_cnt = 0
                continue

            # ATR止损保底（单根快速跌破）
            if pnl <= -t_pos.stop_pct:
                gross = t_pos.size * close
                net   = gross * (1 - cfg.commission_pct)
                cash += net
                trades.append(Trade(
                    time=t, action="T_STOP", price=close, size=t_pos.size,
                    reason=f"ATR保底止损 pnl={pnl:.2%} <= -{t_pos.stop_pct:.2%}",
                    pnl_pct=pnl, layers=t_pos.layers,
                    equity=cash + core_pos.size * close
                ))
                t_pos = Position()
                t_h2_below_cnt = 0
                continue

            # T仓止盈
            tp_targets = [cfg.tp1, cfg.tp2, cfg.tp3]
            tp = tp_targets[min(t_pos.layers - 1, 2)]
            if pnl >= tp:
                gross = t_pos.size * close
                net   = gross * (1 - cfg.commission_pct)
                cash += net
                trades.append(Trade(
                    time=t, action="T_TP", price=close, size=t_pos.size,
                    reason=f"止盈 layer={t_pos.layers} tp={tp:.1%} pnl={pnl:.2%}",
                    pnl_pct=pnl, layers=t_pos.layers,
                    equity=cash + core_pos.size * close
                ))
                t_pos = Position()
                t_h2_below_cnt = 0
                continue

            # T仓加仓（顺势 + 量能 + 浮盈 0-1%）
            if (t_pos.layers < cfg.max_t_layers and trend == "up"
                    and vol_spike and 0 < pnl < 0.01 and atr_ok):
                add_value = cfg.initial_capital * 0.05
                add_size  = add_value / close
                comm      = add_value * cfg.commission_pct
                if cash >= add_value + comm:
                    total_cost    = t_pos.entry_price * t_pos.size + close * add_size
                    t_pos.size   += add_size
                    t_pos.entry_price = total_cost / t_pos.size
                    t_pos.layers += 1
                    cash -= add_value + comm
                    trades.append(Trade(
                        time=t, action="T_ADD", price=close, size=add_size,
                        reason=f"T仓加仓 layer={t_pos.layers} pnl={pnl:.2%}",
                        layers=t_pos.layers,
                        equity=cash + core_pos.size * close + t_pos.size * close
                    ))
            continue

        # ── T仓入场（空T仓）────────────────────────────────────────────────
        # 日线空头禁用T仓入场
        if t_trend_dn:
            continue
        # 基础条件：1H趋势向上 + 在VWAP上方 + ATR足够
        if trend != "up" or not above_vwap or not atr_ok:
            continue

        vwap_reclaim = prev["low"] < vwap and close > vwap

        reason = ""
        if vwap_reclaim and j_ok and vol_spike:
            reason = f"VWAP回踩收复 J={j:.1f} vol={vol_r:.2f}"
        elif macd_pos and j_ok and vol_spike and close > prev["close"] * 1.002:
            reason = f"MACD动量 J={j:.1f} vol={vol_r:.2f}"
        else:
            continue

        buy_value = cfg.initial_capital * cfg.t_pct
        comm      = buy_value * cfg.commission_pct
        buy_size  = buy_value / close
        if cash < buy_value + comm:
            continue

        stop_pct = atr * cfg.atr_sl_mult if not pd.isna(atr) else 0.035
        t_pos     = Position(size=buy_size, entry_price=close,
                             stop_pct=stop_pct, layers=1)
        cash     -= buy_value + comm      # 买入价 + 手续费
        t_h2_below_cnt = 0
        trades.append(Trade(
            time=t, action="T_BUY", price=close, size=buy_size,
            reason=reason, layers=1,
            equity=cash + core_pos.size * close + t_pos.size * close
        ))

    # 回测结束：强平所有仓位
    last_close = df.iloc[-1]["close"]
    if t_pos.is_open:
        pnl  = (last_close - t_pos.entry_price) / t_pos.entry_price
        gross = t_pos.size * last_close
        cash += gross * (1 - cfg.commission_pct)   # T仓EOD也扣手续费
        trades.append(Trade(
            time=df.index[-1], action="T_EOD", price=last_close, size=t_pos.size,
            reason="回测结束强平T仓", pnl_pct=pnl, layers=t_pos.layers,
            equity=cash + core_pos.size * last_close
        ))
    if core_pos.is_open:
        pnl = (last_close - core_pos.entry_price) / core_pos.entry_price
        cash += core_pos.size * last_close
        trades.append(Trade(
            time=df.index[-1], action="CORE_EOD", price=last_close, size=core_pos.size,
            reason="回测结束强平核心仓", pnl_pct=pnl, equity=cash
        ))

    eq_series = pd.Series(equity, index=df.index[1:])
    return trades, eq_series


# ── 日志输出 ───────────────────────────────────────────────────────────────

def print_trade_log(trades: list[Trade], limit: int = 80):
    core_trades = [t for t in trades if t.action.startswith("CORE")]
    t_trades_list = [t for t in trades if t.action.startswith("T_")]

    print(f"\n{'='*72}")
    print(f"  交易日志  共 {len(trades)} 条  "
          f"[核心仓 {len(core_trades)} | T仓 {len(t_trades_list)}]")
    print(f"{'='*72}")

    shown = trades[:limit]
    for t in shown:
        pnl_str  = f"  pnl={t.pnl_pct:+.2%}" if t.pnl_pct is not None else ""
        time_str = str(t.time)[:16]
        tag      = "CORE" if t.action.startswith("CORE") else "T   "
        print(f"  [{time_str}] {tag} {t.action:<12} ${t.price:.2f}"
              f"{pnl_str}  |  {t.reason}")

    if len(trades) > limit:
        print(f"  ... 省略 {len(trades) - limit} 条 (用 --log-limit 调大)")
    print(f"{'='*72}\n")


def print_summary(trades: list[Trade], cfg: StrategyConfig):
    initial = cfg.initial_capital

    core_buys  = [t for t in trades if t.action == "CORE_BUY"]
    core_eod   = [t for t in trades if t.action == "CORE_EOD"]

    t_buys  = [t for t in trades if t.action == "T_BUY"]
    t_exits = [t for t in trades if t.action in ("T_TP", "T_STOP", "T_EOD")]
    t_stops = [t for t in trades if t.action == "T_STOP"]
    t_tps   = [t for t in trades if t.action == "T_TP"]
    t_adds  = [t for t in trades if t.action == "T_ADD"]
    t_pnls  = [t.pnl_pct for t in t_exits if t.pnl_pct is not None]
    t_win   = sum(1 for p in t_pnls if p > 0) / len(t_pnls) * 100 if t_pnls else 0

    intraday_t = 0
    for b in t_buys:
        for x in t_exits:
            if (x.time > b.time and
                    pd.Timestamp(x.time).date() == pd.Timestamp(b.time).date()):
                intraday_t += 1
                break

    final_equity = trades[-1].equity if trades else initial
    total_ret    = (final_equity / initial - 1) * 100

    type_label = "蓝筹(vol≥{:.1f}x)".format(cfg.bluechip_vol_mult) \
        if cfg.stock_type == "bluechip" \
        else "高波动(vol≥{:.1f}x)".format(cfg.volatile_vol_mult)

    print(f"  股票类型:  {type_label}")
    print(f"  总收益:    {total_ret:+.1f}%  (${initial:.0f} → ${final_equity:.0f})")
    print()
    print(f"  ┌─ 核心仓（买入持有，不主动减仓）──────────────")
    print(f"  │  建仓次数: {len(core_buys)} 次  EOD强平: {len(core_eod)} 次")
    print(f"  └──────────────────────────────────────────────")
    print()
    print(f"  ┌─ T 仓 (20%) ─────────────────────────────────")
    print(f"  │  入场次数:  {len(t_buys)}")
    print(f"  │  加仓次数:  {len(t_adds)}")
    print(f"  │  出场次数:  {len(t_exits)}  (止盈 {len(t_tps)} | 止损 {len(t_stops)})")
    print(f"  │  胜率:      {t_win:.1f}%")
    print(f"  │  日内T笔数: {intraday_t}")
    if t_pnls:
        avg_win  = sum(p for p in t_pnls if p > 0) / max(1, sum(1 for p in t_pnls if p > 0))
        avg_loss = sum(p for p in t_pnls if p < 0) / max(1, sum(1 for p in t_pnls if p < 0))
        print(f"  │  均盈:      {avg_win:+.2%}  均亏: {avg_loss:+.2%}")
    print(f"  └──────────────────────────────────────────────")
    print()


# ── main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="核心仓+T仓双轨回测引擎"
    )
    parser.add_argument("--symbol",     default="NVDA")
    parser.add_argument("--tf",         default="1h")
    parser.add_argument("--since",      default=None)
    parser.add_argument("--until",      default=None)
    parser.add_argument("--stock-type", default="bluechip",
                        choices=["bluechip", "volatile"],
                        help="bluechip=蓝筹(vol≥1.2x) volatile=高波动(vol≥1.5x)")
    parser.add_argument("--log-limit",  type=int, default=80)
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

    cfg = StrategyConfig(stock_type=args.stock_type)
    trades, equity = run_backtest(df, cfg)

    print_trade_log(trades, limit=args.log_limit)
    print("  === 汇总 ===")
    print_summary(trades, cfg)


if __name__ == "__main__":
    main()
