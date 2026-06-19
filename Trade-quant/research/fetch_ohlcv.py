"""
从 Yahoo Finance 下载美股/ETF OHLCV 历史数据，存为 Parquet。

用法:
    python fetch_ohlcv.py                              # 下载默认标的组
    python fetch_ohlcv.py --symbols NVDA TSLA SPY      # 自定义标的
    python fetch_ohlcv.py --symbols NVDA --tf 1h       # 1小时粒度
    python fetch_ohlcv.py --since 2022-01-01           # 自定义起始日期

支持的 interval: 1m/2m/5m/15m/30m/60m/90m/1h/1d/5d/1wk/1mo/3mo
注意：yfinance 对分钟级数据有 60天/730天历史限制。
"""
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yfinance as yf

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_SYMBOLS = ["NVDA", "TSLA", "AAPL", "SPY", "QQQ"]
DEFAULT_SINCE = "2022-01-01"


def fetch_symbol(symbol: str, interval: str, since: str, until: str) -> pd.DataFrame:
    ticker = yf.Ticker(symbol)
    df = ticker.history(
        start=since,
        end=until,
        interval=interval,
        auto_adjust=True,
        actions=False,
    )
    if df.empty:
        raise ValueError(f"No data returned for {symbol}")

    # 统一列名为小写
    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]]

    # 统一时区为 UTC
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")

    df.index.name = "datetime"
    return df.sort_index()


def save(df: pd.DataFrame, symbol: str, interval: str) -> Path:
    path = DATA_DIR / f"{symbol.lower()}_{interval}_raw.parquet"
    df.to_parquet(path)
    return path


def main():
    parser = argparse.ArgumentParser(description="Yahoo Finance OHLCV downloader")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--tf", default="1d", help="interval: 1d/1h/15m etc.")
    parser.add_argument("--since", default=DEFAULT_SINCE, help="YYYY-MM-DD")
    parser.add_argument("--until", default=None, help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    until = args.until or datetime.today().strftime("%Y-%m-%d")

    for sym in args.symbols:
        print(f"下载 {sym} {args.tf} from {args.since} to {until} ...")
        try:
            df = fetch_symbol(sym, args.tf, args.since, until)
            path = save(df, sym, args.tf)
            print(f"  ✓ {len(df)} 行  {df.index[0].date()} ~ {df.index[-1].date()}  → {path.name}")
        except Exception as e:
            print(f"  ✗ {sym} 失败: {e}")


if __name__ == "__main__":
    main()
