"""
从 Yahoo Finance 下载美股/ETF OHLCV 历史数据，存为 Parquet。

用法:
    python fetch_ohlcv.py                              # 下载默认标的组
    python fetch_ohlcv.py --symbols NVDA TSLA SPY      # 自定义标的
    python fetch_ohlcv.py --symbols NVDA --tf 1h       # 1小时粒度
    python fetch_ohlcv.py --since 2022-01-01           # 自定义起始日期
    python fetch_ohlcv.py --universe                   # 下载 universe.json 全部股票

支持的 interval: 1m/2m/5m/15m/30m/60m/90m/1h/1d/5d/1wk/1mo/3mo
注意：yfinance 对分钟级数据有 60天/730天历史限制。
"""
import argparse
import json
from datetime import datetime
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


def load_universe_symbols() -> list[str]:
    universe_path = DATA_DIR / "universe.json"
    if not universe_path.exists():
        raise FileNotFoundError("找不到 universe.json，请先运行 build_universe.py")
    with open(universe_path, encoding="utf-8") as f:
        u = json.load(f)
    return u["symbols"] + u["benchmarks"]


def main():
    parser = argparse.ArgumentParser(description="Yahoo Finance OHLCV downloader")
    parser.add_argument("--symbols",   nargs="+", default=None)
    parser.add_argument("--universe",  action="store_true", help="下载 universe.json 全部股票+基准")
    parser.add_argument("--tf",        default="1d", help="interval: 1d/1h/15m etc.")
    parser.add_argument("--since",     default=DEFAULT_SINCE, help="YYYY-MM-DD")
    parser.add_argument("--until",     default=None, help="YYYY-MM-DD (default: today)")
    args = parser.parse_args()

    if args.universe:
        symbols = load_universe_symbols()
        print(f"从 universe.json 加载 {len(symbols)} 个标的")
    else:
        symbols = args.symbols or DEFAULT_SYMBOLS

    until = args.until or datetime.today().strftime("%Y-%m-%d")
    ok, fail = 0, []

    for i, sym in enumerate(symbols, 1):
        prefix = f"[{i:3d}/{len(symbols)}]" if len(symbols) > 5 else ""
        print(f"{prefix} 下载 {sym} {args.tf} ...")
        try:
            df = fetch_symbol(sym, args.tf, args.since, until)
            path = save(df, sym, args.tf)
            print(f"  ✓ {len(df)} 行  {df.index[0].date()} ~ {df.index[-1].date()}  → {path.name}")
            ok += 1
        except Exception as e:
            print(f"  ✗ {sym} 失败: {e}")
            fail.append(sym)

    if len(symbols) > 5:
        print(f"\n完成: 成功 {ok} 只，失败 {len(fail)} 只")
        if fail:
            print(f"失败列表: {fail}")


if __name__ == "__main__":
    main()
