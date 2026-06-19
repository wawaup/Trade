import unittest

from tradebot.indicators import session_vwap
from tradebot.models import Candle


def candle(ts, close, volume=1000):
    return Candle(
        open_time=ts,
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        close_time=ts + 60_000 - 1,
        quote_volume=volume * close,
        trades=100,
    )


class IndicatorTests(unittest.TestCase):
    def test_session_vwap_uses_only_latest_trading_session(self):
        hour = 3_600_000
        previous_day = [
            candle(8 * hour, 200, 10),
            candle(9 * hour, 220, 10),
        ]
        current_day = [
            candle(32 * hour, 100, 10),
            candle(33 * hour, 102, 30),
        ]

        self.assertAlmostEqual(session_vwap(previous_day + current_day), 101.5)


if __name__ == "__main__":
    unittest.main()
