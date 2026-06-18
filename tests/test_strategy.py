import unittest

from tradebot.models import Candle
from tradebot.strategy import StrategyConfig, generate_signal


def candle(ts, open_, high, low, close, volume=1000):
    return Candle(
        open_time=ts,
        open=open_,
        high=high,
        low=low,
        close=close,
        volume=volume,
        close_time=ts + 60_000 - 1,
        quote_volume=volume * close,
        trades=100,
    )


class StrategyTest(unittest.TestCase):
    def test_buy_when_uptrend_and_intraday_pullback_reclaims_vwap(self):
        daily = [
            candle(i, 100 + i, 102 + i, 99 + i, 101 + i)
            for i in range(25)
        ]
        intraday = [
            candle(1, 120, 121, 118, 119, 1000),
            candle(2, 119, 120, 117, 118, 1200),
            candle(3, 118, 121, 117.5, 120.6, 2000),
        ]

        signal = generate_signal(daily, intraday, position_quote=0, config=StrategyConfig())

        self.assertEqual(signal.action, "BUY")
        self.assertIn("uptrend", signal.reason)
        self.assertGreaterEqual(signal.confidence, 0.6)

    def test_sell_t_position_after_profit_target(self):
        daily = [
            candle(i, 100 + i, 102 + i, 99 + i, 101 + i)
            for i in range(25)
        ]
        intraday = [
            candle(1, 120, 121, 119, 120.0, 1000),
            candle(2, 120, 123, 119.5, 122.8, 1800),
        ]

        signal = generate_signal(
            daily,
            intraday,
            position_quote=400,
            avg_entry_price=120,
            config=StrategyConfig(take_profit_pct=0.015),
        )

        self.assertEqual(signal.action, "SELL")
        self.assertIn("profit target", signal.reason)

    def test_holds_when_daily_trend_breaks(self):
        daily = [
            candle(i, 120 - i, 121 - i, 118 - i, 119 - i)
            for i in range(25)
        ]
        intraday = [
            candle(1, 95, 96, 93, 94, 1000),
            candle(2, 94, 95, 92, 93, 1200),
            candle(3, 93, 96, 92.5, 95.5, 2000),
        ]

        signal = generate_signal(daily, intraday, position_quote=0, config=StrategyConfig())

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("daily trend", signal.reason)


if __name__ == "__main__":
    unittest.main()
