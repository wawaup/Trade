"""
从原始 OHLCV Parquet 计算量化因子，存为 factor Parquet。

因子列表：
  MA5, MA10, MA20          — 收盘价简单均线
  trend                    — up/neutral/broken
  ATR_14_pct               — Wilder ATR / 收盘价（14周期）
  VWAP_session             — 当日13:30 UTC起的 session VWAP
  KDJ_K, KDJ_D, KDJ_J     — KDJ(9,3,3)
  MACD_line, MACD_signal, MACD_hist  — MACD(12,26,9)
  vol_ratio                — 当根成交量 / 20周期均量
  buy_signal               — 策略入场信号(1=买)
  sell_signal              — 策略出场信号(1=卖)

用法:
    python compute_factors.py                       # 处理默认标的组
    python compute_factors.py --symbols NVDA TSLA SPY
    python compute_factors.py --symbols NVDA --tf 1d
"""
import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from factor_store import save_factors

DATA_DIR = Path(__file__).parent.parent / "data"
DEFAULT_SYMBOLS = ["NVDA", "TSLA", "SPY"]


def load_raw(symbol: str, timeframe: str) -> pd.DataFrame:
    path = DATA_DIR / f"{symbol.lower()}_{timeframe}_raw.parquet"
    if not path.exists():
        raise FileNotFoundError(f"先运行 fetch_ohlcv.py 下载数据: {path}")
    return pd.read_parquet(path)


def compute_atr_pct_wilder(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    # Wilder RMA: seed = SMA(period), then RMA
    atr = tr.copy()
    seed = tr.iloc[:period].mean()
    atr.iloc[:period] = np.nan
    val = seed
    for i in range(period, len(tr)):
        val = (val * (period - 1) + tr.iloc[i]) / period
        atr.iloc[i] = val
    return (atr / df["close"]).rename("ATR_14_pct")


def compute_kdj(df: pd.DataFrame, period: int = 9, smooth: int = 3) -> pd.DataFrame:
    alpha = 1.0 / smooth
    low_min = df["low"].rolling(period).min()
    high_max = df["high"].rolling(period).max()
    rsv = ((df["close"] - low_min) / (high_max - low_min).replace(0, np.nan) * 100).fillna(50)

    k_vals, d_vals = [], []
    k = d = 50.0
    warmup = period - 1
    for i, rsv_i in enumerate(rsv):
        if i >= warmup:
            k = (1 - alpha) * k + alpha * rsv_i
            d = (1 - alpha) * d + alpha * k
        k_vals.append(k if i >= warmup else np.nan)
        d_vals.append(d if i >= warmup else np.nan)

    k_s = pd.Series(k_vals, index=df.index)
    d_s = pd.Series(d_vals, index=df.index)
    return pd.DataFrame({"KDJ_K": k_s, "KDJ_D": d_s, "KDJ_J": 3 * k_s - 2 * d_s})


def compute_macd(series: pd.Series, fast=12, slow=26, signal=9) -> pd.DataFrame:
    ema_f = series.ewm(span=fast, adjust=False).mean()
    ema_s = series.ewm(span=slow, adjust=False).mean()
    line = ema_f - ema_s
    sig = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"MACD_line": line, "MACD_signal": sig, "MACD_hist": line - sig})


def compute_session_vwap(df: pd.DataFrame, session_hour_utc: int = 13) -> pd.Series:
    """Per-day VWAP reset at session_hour_utc (daily data: approximate as full-day VWAP)."""
    # For daily bars: VWAP = typical price weighted by volume, reset each day
    typical = (df["high"] + df["low"] + df["close"]) / 3
    # For daily data each bar IS the session; just return typical price as proxy VWAP
    return typical.rename("VWAP_session")


def add_signals(df: pd.DataFrame) -> pd.DataFrame:
    vwap_reclaim = (df["low"].shift(1) < df["VWAP_session"]) & (df["close"] > df["VWAP_session"])
    j_ok = df["KDJ_J"] < 70
    vol_spike = df["vol_ratio"] > 1.5
    uptrend = df["trend"] == "up"
    above_vwap = df["close"] > df["VWAP_session"]
    macd_pos = df["MACD_hist"] > 0

    df["buy_signal"] = (uptrend & above_vwap & vwap_reclaim & j_ok & vol_spike).astype(int)
    df["sell_signal"] = ((df["KDJ_J"] > 80) & (df["MACD_hist"] < 0)).astype(int)
    return df


def compute_all(symbol: str, timeframe: str) -> pd.DataFrame:
    raw = load_raw(symbol, timeframe)
    df = raw[["open", "high", "low", "close", "volume"]].copy()

    df["MA5"] = df["close"].rolling(5).mean()
    df["MA10"] = df["close"].rolling(10).mean()
    df["MA20"] = df["close"].rolling(20).mean()

    df["trend"] = "neutral"
    df.loc[(df["MA5"] > df["MA10"]) & (df["MA10"] > df["MA20"]), "trend"] = "up"
    df.loc[df["close"] < df["MA20"] * 0.99, "trend"] = "broken"

    df["ATR_14_pct"] = compute_atr_pct_wilder(df)
    df["VWAP_session"] = compute_session_vwap(df)
    df = df.join(compute_kdj(df))
    df = df.join(compute_macd(df["close"]))
    df["vol_ratio"] = df["volume"] / df["volume"].rolling(20).mean()

    df = add_signals(df)
    return df.dropna(subset=["MA20", "KDJ_K"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--tf", default="1d")
    args = parser.parse_args()

    for sym in args.symbols:
        print(f"计算因子: {sym} {args.tf} ...")
        try:
            df = compute_all(sym, args.tf)
            print(f"  行数: {len(df)}, 列: {len(df.columns)}")
            buy_cnt = int(df["buy_signal"].sum())
            sell_cnt = int(df["sell_signal"].sum())
            print(f"  买入信号: {buy_cnt} 次, 卖出信号: {sell_cnt} 次")
            path = save_factors(df, sym, args.tf)
            print(f"  保存至: {path.name}")
        except FileNotFoundError as e:
            print(f"  X {e}")
        except Exception as e:
            print(f"  X 错误: {e}")
            import traceback; traceback.print_exc()


if __name__ == "__main__":
    main()
