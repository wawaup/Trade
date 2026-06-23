import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="trade-quant-mpl-"))

LIVE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = LIVE_DIR / "alpaca_trader.py"
BUILD_UNIVERSE_PATH = LIVE_DIR.parent / "research" / "build_universe.py"


def load_trader_module():
    spec = importlib.util.spec_from_file_location("alpaca_trader_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_build_universe_module():
    spec = importlib.util.spec_from_file_location("build_universe_under_test", BUILD_UNIVERSE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def make_download_frame(symbols, index):
    fields = ["Close", "High", "Low", "Volume"]
    columns = pd.MultiIndex.from_product([fields, symbols])
    data = {}
    for field in fields:
        for i, symbol in enumerate(symbols, start=1):
            if field == "Volume":
                data[(field, symbol)] = [1_000_000 + i] * len(index)
            else:
                data[(field, symbol)] = [100.0 + i] * len(index)
    return pd.DataFrame(data, index=index, columns=columns)


class FakeBarSet:
    def __init__(self, df):
        self.df = df


def make_alpaca_barset(symbols, index):
    rows = []
    row_index = []
    for symbol in symbols:
        for ts in index:
            row_index.append((symbol, ts))
            rows.append({
                "open": 99.0,
                "high": 101.0,
                "low": 98.0,
                "close": 100.0,
                "volume": 1_000_000,
            })
    df = pd.DataFrame(
        rows,
        index=pd.MultiIndex.from_tuples(row_index, names=["symbol", "timestamp"]),
    )
    return FakeBarSet(df)


class FakeAlpacaDataClient:
    requests = []

    def __init__(self, *args, **kwargs):
        pass

    def get_stock_bars(self, req):
        requested = [req.symbol_or_symbols] if isinstance(req.symbol_or_symbols, str) else list(req.symbol_or_symbols)
        self.requests.append(requested)
        return make_alpaca_barset(requested, pd.bdate_range("2025-01-01", periods=180))


class FetchPanelTests(unittest.TestCase):
    def test_fetch_panel_uses_alpaca_as_primary_source_and_skips_delisted_aliases(self):
        trader = load_trader_module()
        FakeAlpacaDataClient.requests = []

        with (
            patch.object(trader.yf, "download", side_effect=AssertionError("live 默认不应调用 yfinance")),
            patch.object(trader, "StockHistoricalDataClient", FakeAlpacaDataClient),
        ):
            close, high, low, vol = trader.fetch_panel(["AAPL", "DELL", "IIVI"], 350)

        self.assertIn("DELL", close.columns)
        self.assertIn("AAPL", close.columns)
        self.assertIn("QQQ", close.columns)
        self.assertIn("SPY", close.columns)
        self.assertNotIn("IIVI", close.columns)
        self.assertEqual(FakeAlpacaDataClient.requests, [["AAPL", "DELL", "QQQ", "SPY"]])
        self.assertEqual(close["DELL"].count(), 180)
        self.assertEqual(high["DELL"].count(), 180)
        self.assertEqual(low["DELL"].count(), 180)
        self.assertEqual(vol["DELL"].count(), 180)

    def test_alpaca_feed_supports_only_iex_or_sip_and_defaults_to_sip(self):
        trader = load_trader_module()

        with patch.object(trader, "ALPACA_FEED", "sip"):
            self.assertEqual(trader._alpaca_feed(), trader.DataFeed.SIP)

        with patch.object(trader, "ALPACA_FEED", "iex"):
            self.assertEqual(trader._alpaca_feed(), trader.DataFeed.IEX)

        with patch.object(trader, "ALPACA_FEED", "delayed_sip"):
            self.assertEqual(trader._alpaca_feed(), trader.DataFeed.SIP)


class UniverseTests(unittest.TestCase):
    def test_build_universe_excludes_known_delisted_symbols(self):
        build_universe = load_build_universe_module()

        with patch.object(build_universe, "try_fetch_arkk", return_value=[]):
            universe = build_universe.build_universe()

        self.assertNotIn("IIVI", universe["symbols"])


if __name__ == "__main__":
    unittest.main()
