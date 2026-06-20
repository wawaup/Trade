from datetime import datetime, timezone
from pathlib import Path

from tradebot.data import fetch_klines_range, fetch_spot_klines, generate_synthetic_spcx, read_candles_csv

DATA_ROOT = Path(__file__).resolve().parents[1] / "data"

# ---------------------------------------------------------------------------
# Binance bStocks – symbol mapping and epoch
# ---------------------------------------------------------------------------
# bStocks are listed on Binance Spot (not Futures / Alpha).
# API symbol = no slash, suffix B + USDT. e.g. SPCX → SPCXBUSDT
BSTOCK_SYMBOL_MAP: dict[str, str] = {
    "SPCX":  "SPCXBUSDT",   # SpaceX   – opened 2026-06-12
    "TSLA":  "TSLABUSDT",   # Tesla    – opened 2026-06-11
    "NVDA":  "NVDABUSDT",   # NVIDIA   – opened 2026-06-11
    "MU":    "MUBUSDT",     # Micron   – opened 2026-06-11
    "CRCL":  "CRCLBUSDT",   # Circle   – opened 2026-06-11
    "CPOX":  "CRCLBUSDT",   # project alias for Circle bStocks
    "SNDK":  "SNDKBUSDT",   # SanDisk  – opened 2026-06-11
}

# bStocks have no history before 2026-06-10; use this as the earliest startTime
BSTOCK_EPOCH_MS: int = int(datetime(2026, 6, 10, tzinfo=timezone.utc).timestamp() * 1000)

# Map UI resolution strings → Binance interval parameter (case-sensitive on API)
_RESOLUTION_TO_INTERVAL: dict[str, str] = {
    "1m":    "1m",
    "3m":    "3m",
    "5m":    "5m",
    "15m":   "15m",
    "30m":   "30m",
    "1h":    "1h",
    "2h":    "2h",
    "4h":    "4h",
    "6h":    "6h",
    "8h":    "8h",
    "12h":   "12h",
    "1d":    "1d",
    "1day":  "1d",
    "daily": "1d",
    "1w":    "1w",
}


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


class BinanceSpotBStocksDataSource:
    """Fetch live bStocks K-lines from Binance Spot API.

    Automatically maps internal project symbols (SPCX, TSLA, …) to the
    correct Binance API symbols (SPCXBUSDT, TSLABUSDT, …).

    Resolution is honoured: pass resolution='15m' to get 15-minute bars,
    '1h' for hourly bars, etc.  The daily bars (returned as the first element
    of the tuple) are always fetched at '1d' granularity so the backtest
    trend layer works correctly.
    """

    name = "BinanceSpot"

    @staticmethod
    def resolve_symbol(symbol: str) -> str:
        """Return the Binance API symbol for a given UI/project symbol."""
        s = str(symbol or "SPCX").upper()
        return BSTOCK_SYMBOL_MAP.get(s, s)  # fall through unchanged if already an API symbol

    def get_default_candles(self, symbol: str = "SPCX", resolution: str = "1h", **kwargs):
        import time as _time

        api_symbol = self.resolve_symbol(symbol)
        now_ms = int(_time.time() * 1000)

        interval = _RESOLUTION_TO_INTERVAL.get(str(resolution).lower(), "1h")

        # Intraday bars at the requested resolution
        intraday_bars = fetch_klines_range(api_symbol, interval, BSTOCK_EPOCH_MS, now_ms)

        # Daily bars (needed for the strategy trend layer in backtesting)
        if interval == "1d":
            daily_bars = intraday_bars
        else:
            daily_bars = fetch_klines_range(api_symbol, "1d", BSTOCK_EPOCH_MS, now_ms)

        return daily_bars, intraday_bars


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
        # BinanceSpot / bStocks aliases
        "binancespot": "BinanceSpot",
        "bstocks": "BinanceSpot",
        "bstock": "BinanceSpot",
        "binancebstocks": "BinanceSpot",
        "binancehistorical": "BinanceHistorical",
        "historical": "BinanceHistorical",
        "history": "BinanceHistorical",
    }
    _SOURCES = {
        "Synthetic": SyntheticDataSource,
        "CSV": CSVDataSource,
        "Binance": BinanceDataSource,
        "BinanceSpot": BinanceSpotBStocksDataSource,
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
            {"id": "BinanceSpot", "label": "Binance bStocks 实时行情", "requiresNetwork": True},
            {"id": "Binance", "label": "Binance 加密货币现货 K 线", "requiresNetwork": True},
            {"id": "BinanceHistorical", "label": "Binance 历史区间（滚动回测）", "requiresNetwork": True},
        ]
