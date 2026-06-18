from pathlib import Path

from tradebot.data import fetch_spot_klines, generate_synthetic_spcx, read_candles_csv


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
        return read_candles_csv(Path(daily_path)), read_candles_csv(Path(intraday_path))


class BinanceDataSource:
    name = "Binance"

    def get_default_candles(self, symbol="BTCUSDT", daily_limit=60, intraday_limit=500, **kwargs):
        return (
            fetch_spot_klines(symbol, "1d", daily_limit),
            fetch_spot_klines(symbol, "1m", intraday_limit),
        )


class DataSourceFactory:
    _ALIASES = {
        "synthetic": "Synthetic",
        "mock": "Synthetic",
        "demo": "Synthetic",
        "csv": "CSV",
        "file": "CSV",
        "binance": "Binance",
        "spot": "Binance",
    }
    _SOURCES = {
        "Synthetic": SyntheticDataSource,
        "CSV": CSVDataSource,
        "Binance": BinanceDataSource,
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
        ]
