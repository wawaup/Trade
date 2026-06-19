from pathlib import Path

from tradebot.data import fetch_klines_range, fetch_spot_klines, generate_synthetic_spcx, read_candles_csv

DATA_ROOT = Path(__file__).resolve().parents[1] / "data"


def resolve_data_file(path_value, data_root: Path = DATA_ROOT) -> Path:
    raw = Path(str(path_value or ""))
    root = data_root.resolve()
    candidate = raw if raw.is_absolute() else root / raw
    resolved = candidate.resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError("CSV paths must stay inside the Trade data directory")
    if not resolved.is_file():
        raise ValueError(f"CSV file not found in data directory: {raw}")
    return resolved


class SyntheticDataSource:
    name = "Synthetic"

    def get_default_candles(self, symbol="SPCX", **kwargs):
        seed = sum(ord(ch) for ch in str(symbol or "SPCX"))
        return generate_synthetic_spcx(seed=seed)


class CSVDataSource:
    name = "CSV"

    def get_default_candles(self, daily_path=None, intraday_path=None, **kwargs):
        if not daily_path or not intraday_path:
            raise ValueError("CSV source requires daily_path and intraday_path")
        return read_candles_csv(resolve_data_file(daily_path)), read_candles_csv(resolve_data_file(intraday_path))


class BinanceDataSource:
    name = "Binance"

    def get_default_candles(self, symbol="BTCUSDT", daily_limit=60, intraday_limit=500, **kwargs):
        return (
            fetch_spot_klines(symbol, "1d", daily_limit),
            fetch_spot_klines(symbol, "1m", intraday_limit),
        )


class BinanceHistoricalDataSource:
    """Fetch a full historical range of daily bars from Binance for walk-forward testing."""

    name = "BinanceHistorical"

    def get_daily_range(self, symbol: str, start_ms: int, end_ms: int) -> list:
        return fetch_klines_range(symbol, "1d", start_ms, end_ms)

    def get_default_candles(self, symbol="SPCXBUSDT", start_ms=None, end_ms=None, **kwargs):
        import time
        now_ms = int(time.time() * 1000)
        if end_ms is None:
            end_ms = now_ms
        if start_ms is None:
            start_ms = now_ms - 540 * 86_400_000  # ~18 months default
        daily = self.get_daily_range(symbol, start_ms, end_ms)
        intraday = fetch_spot_klines(symbol, "1m", 240)
        return daily, intraday


class DataSourceFactory:
    _ALIASES = {
        "synthetic": "Synthetic",
        "mock": "Synthetic",
        "demo": "Synthetic",
        "csv": "CSV",
        "file": "CSV",
        "binance": "Binance",
        "spot": "Binance",
        "binancehistorical": "BinanceHistorical",
        "historical": "BinanceHistorical",
        "history": "BinanceHistorical",
    }
    _SOURCES = {
        "Synthetic": SyntheticDataSource,
        "CSV": CSVDataSource,
        "Binance": BinanceDataSource,
        "BinanceHistorical": BinanceHistoricalDataSource,
    }

    @classmethod
    def normalize_source(cls, source):
        raw = str(source or "Synthetic").strip()
        if raw in cls._SOURCES:
            return raw
        key = raw.lower().replace("-", "_").replace(" ", "")
        if key in cls._ALIASES:
            return cls._ALIASES[key]
        raise ValueError(f"Unsupported data source: {source}")

    @classmethod
    def get_source(cls, source):
        normalized = cls.normalize_source(source)
        return cls._SOURCES[normalized]()

    @classmethod
    def list_sources(cls):
        return [
            {"id": "Synthetic", "label": "Synthetic demo data", "requiresNetwork": False},
            {"id": "CSV", "label": "Local CSV files", "requiresNetwork": False},
            {"id": "Binance", "label": "Binance public klines", "requiresNetwork": True},
            {"id": "BinanceHistorical", "label": "Binance historical range (walk-forward)", "requiresNetwork": True},
        ]
