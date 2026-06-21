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
        daily = df[["open", "high", "low", "close"]].resample("1D").agg({
            "open": "first", "high": "max", "low": "min", "close": "last"
        }).dropna()
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

        # 日线入场信号：低点触及均线且收盘站上（用于 entry_signal="d_ma"）
        for n in [5, 10, 20]:
            ma = daily[f"d_ma{n}"]
            daily[f"d_touch{n}"] = (daily["low"] <= ma) & (daily["close"] > ma)
            daily[f"d_above{n}"] = daily["close"] > ma
        daily["d_touch"] = daily["d_touch5"] | daily["d_touch10"] | daily["d_touch20"]
        # 日线止损信号：收盘跌破日线MA5（用于 d_ma 模式的出场）
        daily["d_below_ma5"] = daily["close"] < daily["d_ma5"]

        # 近20日动量翻多：收盘站上20日前收盘价 + MA10 高于10日前MA10
        # 用于过滤单边下跌中的短暂反弹，只有真正动量回升时才允许探仓
        daily["d_trend_20d_up"] = (
            (daily["close"] > daily["close"].shift(20)) &
            (daily["d_ma10"] > daily["d_ma10"].shift(10))
        )

        sig_cols = ["d_close", "d_ma5", "d_ma10", "d_ma20",
                    "d_ma30", "d_ma60", "d_ma250", "d_trend_dn",
                    "d_touch", "d_touch5", "d_touch10", "d_touch20",
                    "d_above5", "d_above10", "d_above20", "d_below_ma5",
                    "d_trend_20d_up"]
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
        out["d_touch"]      = daily_sig["d_touch"].fillna(False).astype(bool)
        out["d_below_ma5"]     = daily_sig["d_below_ma5"].fillna(False).astype(bool)
        out["d_trend_20d_up"]  = daily_sig["d_trend_20d_up"].fillna(False).astype(bool)
        for n in [5, 10, 20]:
            out[f"d_touch{n}"] = daily_sig[f"d_touch{n}"].fillna(False).astype(bool)
            out[f"d_above{n}"] = daily_sig[f"d_above{n}"].fillna(False).astype(bool)
    except Exception:
        for col in ["d_close", "d_ma5", "d_ma10", "d_ma20",
                    "d_ma30", "d_ma60", "d_ma250"]:
            out[col] = np.nan
        out["d_trend_dn"]     = False
        out["d_ma20_col"]     = out["MA20"]
        out["d_ma50"]         = out["MA50"]
        out["d_touch"]        = False
        out["d_below_ma5"]    = False
        out["d_trend_20d_up"] = False
        for n in [5, 10, 20]:
            out[f"d_touch{n}"] = False
            out[f"d_above{n}"] = False

    return out


def compute_market_regime(spy_df: pd.DataFrame) -> pd.Series:
    """
    从 SPY 日线数据计算市场 regime：周收盘 > 20周均线 → True（大盘多头，允许个股建仓）。
    移位1周防前视偏差。
    """
    weekly = spy_df[["close"]].resample("W").agg({"close": "last"}).dropna()
    weekly["ma20"] = weekly["close"].rolling(20, min_periods=10).mean()
    weekly["regime"] = weekly["close"] > weekly["ma20"]
    shifted = weekly[["regime"]].copy()
    shifted.index = shifted.index + pd.Timedelta(weeks=1)
    return shifted["regime"]


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
        weekly["w_bull"]            = weekly["w_ma5"] > weekly["w_ma10"]
        weekly["w_close_above_ma5"] = weekly["close"] > weekly["w_ma5"]
        weekly["w_ma5_rising"]      = weekly["w_ma5"] > weekly["w_ma5"].shift(1)
        weekly_sig = weekly[["w_trend_dn", "w_bull", "w_close_above_ma5", "w_ma5_rising"]].copy()
        weekly_sig.index = weekly_sig.index + pd.Timedelta(weeks=1)
        out["w_trend_dn"]        = weekly_sig["w_trend_dn"].reindex(out.index, method="ffill").fillna(False).astype(bool)
        out["w_bull"]            = weekly_sig["w_bull"].reindex(out.index, method="ffill").fillna(False).astype(bool)
        out["w_close_above_ma5"] = weekly_sig["w_close_above_ma5"].reindex(out.index, method="ffill").fillna(False).astype(bool)
        out["w_ma5_rising"]      = weekly_sig["w_ma5_rising"].reindex(out.index, method="ffill").fillna(False).astype(bool)
    except Exception:
        out["w_trend_dn"]        = False
        out["w_bull"]            = False
        out["w_close_above_ma5"] = False
        out["w_ma5_rising"]      = False

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

    入场逻辑（OR 关系，满足其中一根均线即可）：
      信号根：2H K线低点触及 MA5/MA10/MA20 其中之一，且收盘收回该均线之上
      确认根：紧接的下一根2H K线收盘继续站在 MA5/MA10/MA20 任一之上
    """
    try:
        df_2h = df[["open", "high", "low", "close", "volume"]].resample("2h").agg({
            "open": "first", "high": "max", "low": "min",
            "close": "last", "volume": "sum"
        }).dropna()

        df_2h["h2_ma5"]  = df_2h["close"].rolling(5,  min_periods=4).mean()
        df_2h["h2_ma10"] = df_2h["close"].rolling(10, min_periods=8).mean()
        df_2h["h2_ma20"] = df_2h["close"].rolling(20, min_periods=15).mean()
        # 各均线独立的触及+收回信号（low 触及该均线，且 close 收盘站上该均线）
        for n in [5, 10, 20]:
            ma = df_2h[f"h2_ma{n}"]
            df_2h[f"h2_touch{n}"] = (df_2h["low"] <= ma) & (df_2h["close"] > ma)
            df_2h[f"h2_above{n}"] = df_2h["close"] > ma

        # 综合 touch：三根均线任一满足即可
        df_2h["h2_touch"] = (
            df_2h["h2_touch5"] | df_2h["h2_touch10"] | df_2h["h2_touch20"]
        )

        # T仓出场：收盘跌破2H MA5
        df_2h["h2_below_ma5"] = df_2h["close"] < df_2h["h2_ma5"].fillna(0)

        sig_cols = [
            "h2_ma5", "h2_ma10", "h2_ma20",
            "h2_touch", "h2_touch5", "h2_touch10", "h2_touch20",
            "h2_above5", "h2_above10", "h2_above20",
            "h2_below_ma5",
        ]
        # 移位1根2H K线：使用上一根已完成的2H K线信号
        sig = df_2h[sig_cols].shift(1)
        sig_ff = sig.reindex(out.index, method="ffill")

        out["h2_ma5"]       = sig_ff["h2_ma5"].fillna(out["MA5"])
        out["h2_ma10"]      = sig_ff["h2_ma10"].fillna(out["MA10"])
        out["h2_ma20"]      = sig_ff["h2_ma20"].fillna(out["MA20"])
        out["h2_touch"]     = sig_ff["h2_touch"].fillna(False).astype(bool)
        for n in [5, 10, 20]:
            out[f"h2_touch{n}"] = sig_ff[f"h2_touch{n}"].fillna(False).astype(bool)
            out[f"h2_above{n}"] = sig_ff[f"h2_above{n}"].fillna(False).astype(bool)
        out["h2_below_ma5"] = sig_ff["h2_below_ma5"].fillna(False).astype(bool)
    except Exception:
        out["h2_touch"]     = False
        out["h2_below_ma5"] = False
        out["h2_ma5"]       = out["MA5"]
        out["h2_ma10"]      = out["MA10"]
        out["h2_ma20"]      = out["MA20"]
        for n in [5, 10, 20]:
            out[f"h2_touch{n}"] = False
            out[f"h2_above{n}"] = False

    return out


def auto_classify(df: pd.DataFrame) -> str:
    """
    根据 IS 窗口数据动态分类股票特征，避免静态标签在行情变化时失效。

    分类规则（基于 1h 数据）：
      ATR_pct < 0.015           → small_vol   （低波动蓝筹，保守建仓）
      ATR_pct ≥ 0.015 且趋势强  → large_vol   （高波动趋势，金字塔追涨）
      ATR_pct ≥ 0.015 且无趋势  → volatile_vol（高波动震荡，保守建仓+不加仓）
    """
    valid = df.dropna(subset=["ATR_pct", "close"])
    if len(valid) < 20:
        return "small_vol"
    atr_pct = float(valid["ATR_pct"].mean())
    if atr_pct < 0.010:
        return "small_vol"
    # 趋势强度：收盘价与时间序列的 Pearson 相关系数（绝对值）
    n = len(valid)
    closes = valid["close"].values.astype(float)
    t_idx  = np.arange(n, dtype=float)
    t_mean, c_mean = t_idx.mean(), closes.mean()
    cov = ((t_idx - t_mean) * (closes - c_mean)).mean()
    t_std = t_idx.std()
    c_std = closes.std()
    if t_std < 1e-9 or c_std < 1e-9:
        return "volatile_vol"
    trend_corr = abs(cov / (t_std * c_std))
    return "large_vol" if trend_corr >= 0.5 else "volatile_vol"


def compute_grid(df_is: pd.DataFrame) -> tuple:
    """
    从 IS 窗口计算 volatile_vol 网格上下轨（布林带思路）。
    用 IS 期日线收盘的均值 ± 2σ 作为上下轨，代表震荡正常波动范围。
    返回 (grid_upper, grid_lower)，若数据不足则返回 (0.0, 0.0)。
    """
    daily_close = df_is["close"].resample("1D").last().dropna()
    if len(daily_close) < 20:
        return 0.0, 0.0
    mu    = float(daily_close.mean())
    sigma = float(daily_close.std())
    # 买入区：μ-2σ 附近，卖出目标：μ（均值回归即走，更容易触发）
    return mu, mu - 2 * sigma


def compute_indicators(df: pd.DataFrame,
                       market_regime: Optional[pd.Series] = None) -> pd.DataFrame:
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
    # 市场 regime（SPY 周收盘 > MA20 → 大盘多头）
    if market_regime is not None:
        out["mkt_regime"] = market_regime.reindex(out.index, method="ffill").fillna(True).astype(bool)
    else:
        out["mkt_regime"] = True
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
    core_pct_val:         float = 0.70    # 核心仓统一 70%（三种类型一致）
    t_pct:                float = 0.20
    # 核心仓入场
    core_entry_h2bars:    int   = 2
    # 日线多头过滤强度：3=MA5>10>20  4=+30  5=+60  6=+250
    d_trend_min_mas:      int   = 4
    # T仓出场：连续几根2H收盘跌破MA5（2H模式用）
    t_exit_h2_break:      int   = 2
    # 日线T仓参数（d_ma 模式用）
    t_d_tp:               float = 0.04   # 止盈目标 4%
    t_d_days:             int   = 3      # 最大持仓天数
    # small_vol 专属止损（large_vol 不启用：超级趋势股中途震仓会被踢出主升浪）
    # 1) 浮盈归零保护：曾盈利超 core_profit_lock，后跌回建仓成本 → 清仓
    # 2) 早期亏损止损：建仓后 core_early_loss_window 根1H bar 内亏损≥core_early_loss → 清仓
    #    窗口后不再触发，避免牛市正常回调被踢出
    core_profit_lock:          float = 0.03   # 触发浮盈归零保护所需最低历史盈利
    core_early_loss:           float = 0.03   # 早期止损阈值（3%）
    core_early_loss_window:    int   = 20     # 早期止损有效窗口（根1H bar，≈3个交易日）
    # ── 变体实验参数 ──────────────────────────────────────────────────────────
    # T仓加仓间距
    t_add_mode:       str   = "fixed"   # "fixed"=固定0~1%浮盈窗口 | "atr"=0~N×ATR
    t_add_atr_mult:   float = 0.5       # ATR模式下：加仓窗口上限 = N×ATR
    # 核心仓金字塔建仓
    core_mode:        str   = "oneshot"  # "oneshot"=一次建满 | "pyramid"=三层递减 | "auto"=large_vol→pyramid, small_vol→oneshot
    core_l1_frac:     float = 0.55       # L1占core_pct的比例（最大，首次入场）
    core_l2_frac:     float = 0.30       # L2占core_pct的比例
    core_l3_frac:     float = 0.15       # L3占core_pct的比例（三者之和应≈1.0）
    core_pyramid_atr: float = 1.0        # 相邻层价格间距 = N×ATR
    probe_pct:        float = 0.30       # 探仓仓位比例（未满足完整趋势时）
    # volatile_vol 专属网格参数（由 IS 窗口日线高低点均值决定）
    grid_upper:       float = 0.0        # 网格上檐（IS日线高点均值）
    grid_lower:       float = 0.0        # 网格下檐（IS日线低点均值）
    grid_band:        float = 0.05       # 靠近上/下檐触发范围（5%）
    grid_cooldown_bars: int = 20         # 止损后冷却期（1H bar数，约20小时）
    # 入场信号模式
    # "h2_ma"     : 2H MA触及+双根确认（当前策略）
    # "kdj_macd"  : KDJ超卖金叉 + MACD柱收敛/金叉（反转捕捉）
    # "swing_low" : 摆动低点：下影长+量能异常，随后连续收涨
    # "daily_ma"  : 日线MA支撑反弹（日线MA20/MA50触及收回）
    entry_signal: str = "h2_ma"

    @property
    def effective_core_mode(self) -> str:
        """auto 模式：large_vol 用金字塔，small_vol 保持一次建满"""
        if self.core_mode == "auto":
            return "pyramid" if self.stock_type == "large_vol" else "oneshot"
        return self.core_mode

    @property
    def core_pct(self) -> float:
        return self.core_pct_val   # 三种类型统一 70%

    @property
    def vol_mult(self) -> float:
        if self.stock_type == "small_vol":
            return self.small_vol_vol_mult
        return self.large_vol_vol_mult   # large_vol 和 volatile_vol 均用高量能门槛


# ── 持仓状态 ───────────────────────────────────────────────────────────────

@dataclass
class Position:
    size:             float = 0.0
    entry_price:      float = 0.0
    stop_pct:         float = 0.0
    layers:           int   = 0
    prev_layer_price: float = 0.0   # 金字塔：上一层入场价（跌破→止损）
    pyramid_target:   float = 0.0   # 金字塔：下一层触发价

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

    # 档位2：宽松，只需收盘价站上 MA5 和 MA10
    if min_mas <= 2:
        if any(pd.isna(v) for v in [dc, ma5, ma10]):
            return False
        return bool(dc > ma5 and dc > ma10)

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
    core_h2_ma5        = False  # K线1 触发时，是否踩了 MA5
    core_h2_ma10       = False  # K线1 触发时，是否踩了 MA10
    core_h2_ma20       = False  # K线1 触发时，是否踩了 MA20
    core_hold_bars     = 0    # 建仓后累计1H bar数，用于早期亏损止损窗口
    core_entry_bar     = -1   # 建仓时的 bar index，防止同 bar 即止损
    portfolio_peak     = 0.0  # 核心仓持仓以来最高总资产（整体收益率追踪）
    core_entry_equity  = 0.0  # 建仓时总资产
    prev_t             = None

    t_pos           = Position()
    t_h2_below_cnt  = 0   # 连续2H收盘在 h2_ma5 下方的计数（T出场信号）
    t_grid_cooldown = 0   # volatile_vol止损后冷却期（剩余bar数）
    t_day_count     = 0   # 日线T仓持仓天数（d_ma 模式用）
    # kdj_macd 模式状态
    kdj_os_cnt      = 0   # 距上次 J<25 的计数（0=无；1~15=近期超卖）
    # swing_low 模式状态
    swing_cnt       = 0   # 底部确认后连续收涨计数
    swing_ref_low   = 0.0 # 摆动低点价格
    # d_ma 模式状态（日线 2 天确认）
    d_sig_count     = 0   # 0=无信号 1=日线K1触及 2=日线K2确认(可建仓)
    d_sig_ma5       = False
    d_sig_ma10      = False
    d_sig_ma20      = False
    cash            = cfg.initial_capital
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
        is_new_h2  = (prev_t is None) or (t.floor("2h") != prev_t.floor("2h"))
        is_new_day = (prev_t is None) or (t.floor("1D") != prev_t.floor("1D"))
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
        h2_touch5    = bool(bar.get("h2_touch5",    False))
        h2_touch10   = bool(bar.get("h2_touch10",   False))
        h2_touch20   = bool(bar.get("h2_touch20",   False))
        h2_above5    = bool(bar.get("h2_above5",    False))
        h2_above10   = bool(bar.get("h2_above10",   False))
        h2_above20   = bool(bar.get("h2_above20",   False))
        h2_below_ma5 = bool(bar.get("h2_below_ma5", False))

        # 2H MA 数值（用于 volatile_vol 买卖条件）
        h2_ma5_val   = bar.get("h2_ma5",  np.nan)
        h2_ma10_val  = bar.get("h2_ma10", np.nan)
        h2_ma5_prev  = prev.get("h2_ma5", np.nan)
        ma5_gt_ma10  = (not pd.isna(h2_ma5_val) and not pd.isna(h2_ma10_val)
                        and h2_ma5_val > h2_ma10_val)
        h2_ma5_fall  = (not pd.isna(h2_ma5_val) and not pd.isna(h2_ma5_prev)
                        and h2_ma5_val < h2_ma5_prev)

        # 冷却期倒计
        if t_grid_cooldown > 0:
            t_grid_cooldown -= 1

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
        d_trend_dn        = bool(bar.get("d_trend_dn",        False))
        w_close_above_ma5 = bool(bar.get("w_close_above_ma5", False))
        w_trend_dn        = bool(bar.get("w_trend_dn",        False))
        mkt_regime        = bool(bar.get("mkt_regime",        True))
        # 入场门槛：大盘多头(SPY周线>MA20) + 个股周收盘站上MA5 + 周线未确认空头
        w_entry_gate      = mkt_regime and w_close_above_ma5 and not w_trend_dn
        any_trend_dn = d_trend_dn
        t_trend_dn   = d_trend_dn

        # 日线触及/止损信号（已 forward-fill 到1H index，shift+1天防前视）
        d_touch      = bool(bar.get("d_touch",     False))
        d_touch5     = bool(bar.get("d_touch5",    False))
        d_touch10    = bool(bar.get("d_touch10",   False))
        d_touch20    = bool(bar.get("d_touch20",   False))
        d_above5     = bool(bar.get("d_above5",    False))
        d_above10    = bool(bar.get("d_above10",   False))
        d_above20    = bool(bar.get("d_above20",   False))
        d_below_ma5  = bool(bar.get("d_below_ma5", False))

        # ══ 核心仓：2H双根信号建仓，建后不动 ════════════════════════════════
        # ══ 核心仓入场（按 entry_signal 分支）══════════════════════════════════
        # d_ma 模式：空仓时正常建仓；仓位不足70%时允许信号再触发加仓
        _full_value = cfg.initial_capital * cfg.core_pct
        _cur_value  = core_pos.size * close if core_pos.is_open else 0.0
        _allow_add  = (cfg.entry_signal == "d_ma" and core_pos.is_open
                       and _cur_value < _full_value * 0.70
                       and w_entry_gate)  # 周线门槛满足才允许加仓
        if not core_pos.is_open or _allow_add:
            entry_triggered = False
            entry_reason    = ""

            # ── h2_ma：2H MA触及+双根确认（当前基准策略）──────────────────
            if cfg.entry_signal == "h2_ma":
                if not any_trend_dn:
                    if is_new_h2:
                        if h2_touch:
                            core_h2_count = 1
                            core_h2_ma5  = h2_touch5
                            core_h2_ma10 = h2_touch10
                            core_h2_ma20 = h2_touch20
                        elif core_h2_count == 1:
                            same_ma_ok = (
                                (core_h2_ma5  and h2_above5)  or
                                (core_h2_ma10 and h2_above10) or
                                (core_h2_ma20 and h2_above20)
                            )
                            if same_ma_ok:
                                core_h2_count = 2
                            else:
                                core_h2_count = 0
                                core_h2_ma5 = core_h2_ma10 = core_h2_ma20 = False
                        else:
                            core_h2_count = 0
                            core_h2_ma5 = core_h2_ma10 = core_h2_ma20 = False
                    if core_h2_count >= cfg.core_entry_h2bars and above_vwap:
                        entry_triggered = True
                        entry_reason    = "2H双根同线确认+VWAP"
                        core_h2_count   = 0
                        core_h2_ma5 = core_h2_ma10 = core_h2_ma20 = False
                else:
                    core_h2_count = 0

            # ── kdj_macd：KDJ超卖金叉 + MACD柱收敛/金叉（碗底反转）──────
            elif cfg.entry_signal == "kdj_macd":
                # 追踪近期超卖
                if j < 25:
                    kdj_os_cnt = 1
                elif 0 < kdj_os_cnt < 20:
                    kdj_os_cnt += 1
                else:
                    kdj_os_cnt = 0

                kdj_cross    = (k > d_kdj) and (prev["KDJ_K"] <= prev["KDJ_D"])
                macd_improve = macd_h > prev["MACD_hist"]           # 柱子在收窄
                macd_xup     = (macd_h > 0) and (prev["MACD_hist"] <= 0)  # 金叉

                # 软趋势过滤：只在价格跌破日线MA20 8%以上时禁止（不用严格d_trend_dn）
                d_ma20_v  = bar.get("d_ma20", np.nan)
                soft_block = (not pd.isna(d_ma20_v)) and (close < d_ma20_v * 0.92)

                if (0 < kdj_os_cnt <= 15 and kdj_cross
                        and (macd_improve or macd_xup)
                        and close > vwap * 0.97          # VWAP 允许 3% 以内偏差
                        and not soft_block):
                    entry_triggered = True
                    entry_reason    = (f"KDJ超卖金叉 J_cnt={kdj_os_cnt}"
                                       f" {'MACD金叉' if macd_xup else 'MACD收敛'}")

            # ── swing_low：摆动低点（下影长+量能，随后连续收涨）───────────
            elif cfg.entry_signal == "swing_low":
                lower_wick = min(float(bar["open"]), close) - float(bar["low"])
                body_size  = abs(float(bar["open"]) - close)
                wick_big   = (lower_wick > max(body_size * 1.5, atr * 0.3)
                              if not pd.isna(atr) else lower_wick > body_size * 1.5)

                if wick_big and vol_spike:
                    # 发现摆动低点特征：重置计数，记录参考价
                    swing_cnt     = 1
                    swing_ref_low = close
                elif swing_cnt > 0:
                    if close > prev["close"] and close > swing_ref_low:
                        swing_cnt += 1
                    else:
                        swing_cnt     = 0
                        swing_ref_low = 0.0

                # 底部后连续 3 根收涨 → 确认反转，且不是严重空头
                d_ma20_v  = bar.get("d_ma20", np.nan)
                soft_block = (not pd.isna(d_ma20_v)) and (close < d_ma20_v * 0.90)
                if (swing_cnt >= 3 and close > vwap * 0.97 and not soft_block):
                    entry_triggered = True
                    entry_reason    = f"摆动低点反弹 cnt={swing_cnt}"
                    swing_cnt       = 0
                    swing_ref_low   = 0.0

            # ── d_ma：日线MA触及+次日确认，周线门槛（MA5>MA10且收盘站上MA5）才监测 ─
            elif cfg.entry_signal == "d_ma" and cfg.stock_type != "volatile_vol":
                if not w_entry_gate:
                    # 周线门槛未满足：重置信号，不监测入场
                    d_sig_count = 0
                    d_sig_ma5 = d_sig_ma10 = d_sig_ma20 = False
                else:
                    # 周线行情启动：正常监测日线 K1/K2 入场信号
                    if is_new_day:
                        if d_touch:
                            # 日线 K1：低点触及均线且收盘站上 → 记录触发的MA
                            d_sig_count = 1
                            d_sig_ma5   = d_touch5
                            d_sig_ma10  = d_touch10
                            d_sig_ma20  = d_touch20
                        elif d_sig_count == 1:
                            # 日线 K2：同MA确认收盘仍站上 → 信号成立
                            same_ma_ok = (
                                (d_sig_ma5  and d_above5)  or
                                (d_sig_ma10 and d_above10) or
                                (d_sig_ma20 and d_above20)
                            )
                            if same_ma_ok:
                                d_sig_count = 2
                            else:
                                d_sig_count = 0
                                d_sig_ma5 = d_sig_ma10 = d_sig_ma20 = False
                        else:
                            d_sig_count = 0
                            d_sig_ma5 = d_sig_ma10 = d_sig_ma20 = False

                    # 日线 K2 确认后直接建满仓
                    if d_sig_count == 2:
                        entry_triggered = True
                        which = "MA5" if d_sig_ma5 else ("MA10" if d_sig_ma10 else "MA20")
                        entry_reason    = f"周线启动+日线{which}触及确认"
                        d_sig_count = 0
                        d_sig_ma5 = d_sig_ma10 = d_sig_ma20 = False

            # ── volatile_vol 网格入场：价格处于下檐附近区间才买 ────────────
            elif cfg.entry_signal == "d_ma" and cfg.stock_type == "volatile_vol":
                _grid_buy_lo = cfg.grid_lower * (1 - cfg.grid_band)   # 跌穿则不买
                _grid_buy_hi = cfg.grid_lower * (1 + cfg.grid_band)   # 上限
                if (cfg.grid_lower > 0
                        and _grid_buy_lo <= close <= _grid_buy_hi):
                    entry_triggered = True
                    entry_reason = (f"网格下檐买入 lower={cfg.grid_lower:.2f}"
                                    f" price={close:.2f}")

            # ── daily_ma（旧版，保留兼容）: 已废弃，用 d_ma 代替 ───────────
            elif cfg.entry_signal == "daily_ma":
                d_ma20_v = bar.get("d_ma20", np.nan)
                d_ma60_v = bar.get("d_ma60", np.nan)
                touch_ma20 = (not pd.isna(d_ma20_v)
                              and float(bar["low"]) <= d_ma20_v * 1.015
                              and close > d_ma20_v)
                touch_ma60 = (not pd.isna(d_ma60_v)
                              and float(bar["low"]) <= d_ma60_v * 1.015
                              and close > d_ma60_v)
                if (touch_ma20 or touch_ma60) and vol_r > 1.0:
                    entry_triggered = True
                    entry_reason    = f"日线MA{'20' if touch_ma20 else '60'}支撑反弹 vol={vol_r:.1f}x"

            # ── 执行建仓 / 加仓 ──────────────────────────────────────────────
            if entry_triggered:
                if _allow_add:
                    # 加仓：补满剩余容量的50%，更新均价
                    add_value = (_full_value - _cur_value) * 0.5
                    add_size  = add_value / close
                    if cash >= add_value and add_size > 0:
                        prev_cost         = core_pos.entry_price * core_pos.size
                        core_pos.size    += add_size
                        core_pos.entry_price = (prev_cost + close * add_size) / core_pos.size
                        cash             -= add_value
                        core_entry_bar    = i
                        trades.append(Trade(
                            time=t, action="CORE_ADD", price=close, size=add_size,
                            reason=f"加仓至{core_pos.size*close/_full_value*100:.0f}% {entry_reason}",
                            equity=cash + core_pos.size * close + t_pos.size * close,
                        ))
                else:
                    # 首次建仓
                    if cfg.entry_signal == "d_ma":
                        buy_value = _full_value
                        nxt_tgt   = 0.0
                    elif cfg.effective_core_mode == "pyramid":
                        buy_value = _full_value * cfg.core_l1_frac
                        nxt_tgt   = close * (1 + cfg.core_pyramid_atr * max(atr, 0.01))
                    else:
                        buy_value = _full_value
                        nxt_tgt   = 0.0
                    buy_size = buy_value / close if buy_value > 0 else 0.0
                    if buy_value > 0 and cash >= buy_value:
                        core_pos  = Position(size=buy_size, entry_price=close, stop_pct=0.0,
                                             layers=1, prev_layer_price=0.0, pyramid_target=nxt_tgt)
                        cash             -= buy_value
                        core_hold_bars    = 0
                        core_entry_bar    = i
                        core_entry_equity = cash + core_pos.size * close + t_pos.size * close
                        portfolio_peak    = core_entry_equity
                        trades.append(Trade(
                            time=t, action="CORE_BUY", price=close, size=buy_size,
                            reason=entry_reason,
                            equity=cash + core_pos.size * close,
                        ))

        # ══ 金字塔核心仓：追加后续层（d_ma 模式不加仓）══════════════════════
        if (core_pos.is_open and cfg.entry_signal != "d_ma"
                and cfg.effective_core_mode == "pyramid"
                and core_pos.layers < 3 and close >= core_pos.pyramid_target
                and not any_trend_dn):
            add_frac  = cfg.core_l2_frac if core_pos.layers == 1 else cfg.core_l3_frac
            next_tgt  = (close * (1 + cfg.core_pyramid_atr * max(atr, 0.01))
                         if core_pos.layers == 1 else float("inf"))
            add_value = cfg.initial_capital * cfg.core_pct * add_frac
            add_size  = add_value / close
            if cash >= add_value:
                prev_entry            = core_pos.entry_price
                total_cost            = core_pos.entry_price * core_pos.size + close * add_size
                core_pos.size        += add_size
                core_pos.entry_price  = total_cost / core_pos.size
                core_pos.layers      += 1
                core_pos.prev_layer_price = prev_entry
                core_pos.pyramid_target   = next_tgt
                cash                 -= add_value
                trades.append(Trade(
                    time=t, action="CORE_ADD", price=close, size=add_size,
                    reason=f"金字塔L{core_pos.layers} 间距{cfg.core_pyramid_atr:.1f}×ATR",
                    layers=core_pos.layers,
                    equity=cash + core_pos.size * close + t_pos.size * close,
                ))

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
                d_sig_count       = 0
                d_sig_ma5 = d_sig_ma10 = d_sig_ma20 = False
                core_hold_bars    = 0
                portfolio_peak    = 0.0
                core_entry_equity = 0.0
                if t_pos.is_open:
                    t_pnl = (close - t_pos.entry_price) / t_pos.entry_price
                    cash += t_pos.size * close * (1 - cfg.commission_pct)
                    trades.append(Trade(
                        time=t, action="T_STOP", price=close, size=t_pos.size,
                        reason=f"核心仓止损同步平T pnl={t_pnl:.2%}",
                        pnl_pct=t_pnl, layers=t_pos.layers,
                        equity=cash,
                    ))
                    t_pos = Position(); t_day_count = 0

        # ══ 金字塔减仓：收盘跌破MA5 → 剥离一层（软止损）══════════════════
        # d_ma 模式用日线条件（is_new_day + d_below_ma5），其余用 2H 条件
        _use_daily_stop = (cfg.entry_signal == "d_ma")
        _stop_trigger   = (
            (is_new_day and d_below_ma5) if _use_daily_stop
            else (is_new_h2 and h2_below_ma5)
        )
        _stop_label = "日线跌破MA5" if _use_daily_stop else "2H跌破MA5"
        if (core_pos.is_open and cfg.entry_signal != "d_ma"
                and cfg.effective_core_mode == "pyramid"
                and _stop_trigger and i > core_entry_bar):
            pnl = (close - core_pos.entry_price) / core_pos.entry_price
            if core_pos.layers > 1:
                sell_size     = core_pos.size / core_pos.layers   # 剥离最近一层
                gross         = sell_size * close
                net           = gross * (1 - cfg.commission_pct)
                cash         += net
                core_pos.size -= sell_size
                core_pos.layers -= 1
                trades.append(Trade(
                    time=t, action="CORE_CUT", price=close, size=sell_size,
                    reason=(f"{_stop_label}减仓 剩余{core_pos.layers}层"
                            f" pnl={pnl:.2%}"),
                    pnl_pct=pnl, layers=core_pos.layers,
                    equity=cash + core_pos.size * close + t_pos.size * close,
                ))
            else:
                # 只剩1层 → 全部清仓
                gross = core_pos.size * close
                net   = gross * (1 - cfg.commission_pct)
                cash += net
                trades.append(Trade(
                    time=t, action="CORE_STOP", price=close, size=core_pos.size,
                    reason=f"{_stop_label}止损(最终层) pnl={pnl:.2%}",
                    pnl_pct=pnl, equity=cash + t_pos.size * close,
                ))
                core_pos          = Position()
                core_h2_count     = 0
                d_sig_count       = 0
                d_sig_ma5 = d_sig_ma10 = d_sig_ma20 = False
                core_hold_bars    = 0
                portfolio_peak    = 0.0
                core_entry_equity = 0.0
                if t_pos.is_open:
                    t_pnl = (close - t_pos.entry_price) / t_pos.entry_price
                    cash += t_pos.size * close * (1 - cfg.commission_pct)
                    trades.append(Trade(
                        time=t, action="T_STOP", price=close, size=t_pos.size,
                        reason=f"核心仓止损同步平T pnl={t_pnl:.2%}",
                        pnl_pct=t_pnl, layers=t_pos.layers,
                        equity=cash,
                    ))
                    t_pos = Position(); t_day_count = 0

        # ══ 金字塔止损：跌破上一层入场价（d_ma 模式不适用）══════════════════
        if (core_pos.is_open and cfg.entry_signal != "d_ma"
                and cfg.effective_core_mode == "pyramid"
                and core_pos.layers >= 2 and close < core_pos.prev_layer_price):
            pnl   = (close - core_pos.entry_price) / core_pos.entry_price
            cash += core_pos.size * close
            trades.append(Trade(
                time=t, action="CORE_STOP", price=close, size=core_pos.size,
                reason=(f"金字塔止损 跌破L{core_pos.layers - 1}"
                        f"价{core_pos.prev_layer_price:.2f} pnl={pnl:.2%}"),
                pnl_pct=pnl, equity=cash + t_pos.size * close,
            ))
            core_pos          = Position()
            core_h2_count     = 0
            core_hold_bars    = 0
            portfolio_peak    = 0.0
            core_entry_equity = 0.0
            if t_pos.is_open:
                t_pnl = (close - t_pos.entry_price) / t_pos.entry_price
                cash += t_pos.size * close * (1 - cfg.commission_pct)
                trades.append(Trade(
                    time=t, action="T_STOP", price=close, size=t_pos.size,
                    reason=f"核心仓止损同步平T pnl={t_pnl:.2%}",
                    pnl_pct=t_pnl, layers=t_pos.layers, equity=cash,
                ))
                t_pos = Position(); t_day_count = 0

        # ══ volatile_vol 网格：靠近上檐止盈，跌穿下檐止损 ══════════════════
        if (core_pos.is_open and cfg.stock_type == "volatile_vol"
                and cfg.entry_signal == "d_ma" and cfg.grid_upper > 0
                and i > core_entry_bar):   # 防同 bar 建仓后即触发出场
            pnl = (close - core_pos.entry_price) / core_pos.entry_price
            _grid_exit_reason = None
            if close >= cfg.grid_upper * (1 - cfg.grid_band):
                _grid_exit_reason = f"网格上檐止盈 upper={cfg.grid_upper:.2f} pnl={pnl:.2%}"
            elif close < cfg.grid_lower * (1 - cfg.grid_band):
                _grid_exit_reason = f"网格跌穿下檐止损 lower={cfg.grid_lower:.2f} pnl={pnl:.2%}"
            if _grid_exit_reason:
                net   = core_pos.size * close * (1 - cfg.commission_pct)
                cash += net
                trades.append(Trade(
                    time=t, action="CORE_STOP", price=close, size=core_pos.size,
                    reason=_grid_exit_reason, pnl_pct=pnl,
                    equity=cash + t_pos.size * close,
                ))
                core_pos          = Position()
                core_h2_count     = 0
                d_sig_count       = 0
                d_sig_ma5 = d_sig_ma10 = d_sig_ma20 = False
                core_hold_bars    = 0
                portfolio_peak    = 0.0
                core_entry_equity = 0.0

        # ══ T仓管理（核心仓不存在时跳过）══════════════════════════════════
        if not core_pos.is_open:
            continue

        # ── 日线T仓（d_ma 模式）── 暂停，专注核心仓策略优化 ─────────────────
        if cfg.entry_signal == "d_ma":
            # T仓逻辑暂时注释，待核心仓策略稳定后启用
            # if t_pos.is_open:
            #     t_pnl = (close - t_pos.entry_price) / t_pos.entry_price
            #     if is_new_day:
            #         t_day_count += 1
            #     exit_reason = ""
            #     exit_action = "T_STOP"
            #     if is_new_day and d_below_ma5:
            #         exit_reason = f"日线跌破MA5 pnl={t_pnl:.2%}"
            #     elif t_pnl >= cfg.t_d_tp:
            #         exit_reason = f"T止盈{cfg.t_d_tp:.0%} pnl={t_pnl:.2%}"
            #         exit_action = "T_TP"
            #     elif is_new_day and t_day_count >= cfg.t_d_days:
            #         exit_action = "T_TP" if t_pnl >= 0 else "T_STOP"
            #         exit_reason = f"持仓{t_day_count}日出场 pnl={t_pnl:.2%}"
            #     if exit_reason:
            #         gross = t_pos.size * close
            #         cash += gross * (1 - cfg.commission_pct)
            #         trades.append(Trade(
            #             time=t, action=exit_action, price=close, size=t_pos.size,
            #             reason=exit_reason, pnl_pct=t_pnl, layers=t_pos.layers,
            #             equity=cash + core_pos.size * close,
            #         ))
            #         t_pos = Position(); t_day_count = 0
            # else:
            #     _core_pnl = (close - core_pos.entry_price) / core_pos.entry_price
            #     _dma5_v   = bar.get("d_ma5",  np.nan)
            #     _dma10_v  = bar.get("d_ma10", np.nan)
            #     _d_bull   = (not pd.isna(_dma5_v) and not pd.isna(_dma10_v) and _dma5_v > _dma10_v)
            #     _core_safe = _core_pnl >= -0.02
            #     if is_new_day and d_touch5 and not t_trend_dn and _d_bull and w_bull and _core_safe:
            continue  # T仓暂停，跳过入场

        # ── 2H 动量T仓（非 d_ma 模式）────────────────────────────────────
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
                if cfg.stock_type == "volatile_vol":
                    t_grid_cooldown = cfg.grid_cooldown_bars
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
                if cfg.stock_type == "volatile_vol":
                    t_grid_cooldown = cfg.grid_cooldown_bars
                continue

            # T仓止盈（动量模式）
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

            # T仓加仓（顺势 + 量能 + 浮盈 0~N×ATR 或固定0~1%）
            t_add_upper = (cfg.t_add_atr_mult * atr
                           if cfg.t_add_mode == "atr" and atr > 0 else 0.01)
            if (t_pos.layers < cfg.max_t_layers and trend == "up"
                    and vol_spike and 0 < pnl < t_add_upper and atr_ok):
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

        # ── 2H T仓入场（空T仓）────────────────────────────────────────────
        if t_trend_dn:
            continue

        buy_value = cfg.initial_capital * cfg.t_pct
        comm      = buy_value * cfg.commission_pct
        buy_size  = buy_value / close
        if cash < buy_value + comm:
            continue

        if cfg.stock_type == "volatile_vol":
            continue   # 震荡股暂不做T，跳过动量T逻辑

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

        stop_pct = atr * cfg.atr_sl_mult if not pd.isna(atr) else 0.035
        t_pos     = Position(size=buy_size, entry_price=close,
                             stop_pct=stop_pct, layers=1)
        cash     -= buy_value + comm
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
