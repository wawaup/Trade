import unittest

from tradebot.data_sources import DataSourceFactory
from tradebot.models import Candle


class DataSourceFactoryTests(unittest.TestCase):
    def test_normalize_source_aliases(self):
        self.assertEqual(DataSourceFactory.normalize_source("synthetic"), "Synthetic")
        self.assertEqual(DataSourceFactory.normalize_source("csv"), "CSV")
        self.assertEqual(DataSourceFactory.normalize_source("binance"), "Binance")

    def test_unknown_source_raises_value_error(self):
        with self.assertRaises(ValueError):
            DataSourceFactory.normalize_source("mystery")

    def test_synthetic_source_returns_candles(self):
        source = DataSourceFactory.get_source("synthetic")
        daily, intraday = source.get_default_candles(symbol="NVDA")
        self.assertTrue(daily)
        self.assertTrue(intraday)
        self.assertIsInstance(daily[0], Candle)
        self.assertIsInstance(intraday[0], Candle)


if __name__ == "__main__":
    unittest.main()
