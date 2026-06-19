"""Parquet 因子表读写封装。"""
from pathlib import Path
import pandas as pd

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)


def factor_path(symbol: str, timeframe: str) -> Path:
    return DATA_DIR / f"{symbol.lower()}_{timeframe}_factors.parquet"


def save_factors(df: pd.DataFrame, symbol: str, timeframe: str) -> Path:
    path = factor_path(symbol, timeframe)
    df.to_parquet(path)
    return path


def load_factors(symbol: str, timeframe: str) -> pd.DataFrame:
    path = factor_path(symbol, timeframe)
    if not path.exists():
        raise FileNotFoundError(f"Factor file not found: {path}\n先运行 compute_factors.py")
    return pd.read_parquet(path)
