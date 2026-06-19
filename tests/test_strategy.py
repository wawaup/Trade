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

    def test_neutral_trend_can_buy_with_deeper_pullback_and_high_atr(self):
        daily = [
            candle(1, 100, 106, 94, 100),
            candle(2, 100, 108, 95, 105),
            candle(3, 105, 110, 99, 103),
            candle(4, 103, 111, 100, 107),
            candle(5, 107, 112, 101, 106),
            candle(6, 106, 113, 102, 109),
            candle(7, 109, 114, 103, 108),
            candle(8, 108, 115, 104, 111),
            candle(9, 111, 116, 105, 110),
            candle(10, 110, 117, 106, 113),
            candle(11, 113, 118, 107, 112),
            candle(12, 112, 119, 108, 115),
            candle(13, 115, 120, 109, 114),
            candle(14, 114, 121, 110, 117),
            candle(15, 117, 122, 111, 116),
            candle(16, 116, 123, 112, 119),
            candle(17, 119, 124, 113, 118),
            candle(18, 118, 125, 114, 121),
            candle(19, 121, 126, 115, 120),
            candle(20, 120, 127, 116, 123),
            candle(21, 123, 128, 117, 122),
            candle(22, 122, 129, 118, 125),
            candle(23, 125, 130, 119, 124),
            candle(24, 124, 131, 120, 126),
            candle(25, 126, 132, 121, 124),
        ]
        intraday = [
            candle(1, 124, 125, 122, 124, 1000),
            candle(2, 124, 124, 120, 121, 1200),
            candle(3, 121, 125, 120.5, 124.8, 2200),
        ]

        signal = generate_signal(daily, intraday, position_quote=0, config=StrategyConfig())

        self.assertEqual(signal.action, "BUY")
        self.assertIn("neutral", signal.reason)

    def test_strong_momentum_can_enter_half_size_above_vwap(self):
        daily = [candle(i, 100 + i, 104 + i, 98 + i, 101 + i) for i in range(25)]
        intraday = [
            candle(1, 120.0, 121.0, 119.5, 120.4, 1000),
            candle(2, 120.4, 121.2, 120.0, 120.8, 1000),
            candle(3, 120.8, 121.8, 120.6, 121.5, 2500),
            candle(4, 121.5, 122.4, 121.2, 122.0, 2600),
            candle(5, 122.0, 123.0, 121.8, 122.6, 3000),
        ]

        signal = generate_signal(
            daily,
            intraday,
            position_quote=0,
            config=StrategyConfig(
                momentum_lookback=1,
                momentum_vwap_premium_pct=0.002,
                momentum_volume_multiplier=1.4,
            ),
        )

        self.assertEqual(signal.action, "BUY")
        self.assertIn("momentum", signal.reason)
        self.assertEqual(signal.suggested_quote, 175.0)

    def test_dynamic_take_profit_decreases_with_layers(self):
        daily = [candle(i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(25)]
        intraday = [
            candle(1, 120, 121, 119, 120.0, 1000),
            candle(2, 120, 122, 119.5, 121.3, 1800),
        ]

        signal = generate_signal(
            daily,
            intraday,
            position_quote=1050,
            avg_entry_price=120,
            layers=3,
            config=StrategyConfig(),
        )

        self.assertEqual(signal.action, "SELL")
        self.assertIn("dynamic profit target", signal.reason)

    def test_rejects_extreme_one_minute_spike(self):
        daily = [candle(i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(25)]
        intraday = [
            candle(1, 120, 121, 118, 119, 1000),
            candle(2, 119, 120, 117, 118, 1200),
            candle(3, 118, 128, 117.5, 126.5, 2000),
        ]

        signal = generate_signal(daily, intraday, position_quote=0, config=StrategyConfig())

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("extreme", signal.reason)

    def test_signal_vwap_ignores_previous_session_candles(self):
        hour = 3_600_000
        daily = [candle(i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(25)]
        previous_session = [
            candle(8 * hour, 210, 212, 209, 211, 1000),
            candle(9 * hour, 211, 213, 210, 212, 1000),
        ]
        current_session = [
            candle(32 * hour, 100, 101, 99, 100, 1000),
            candle(32 * hour + 60_000, 100, 101, 98, 98, 1000),
            candle(32 * hour + 120_000, 98, 102.8, 97, 102.8, 1000),
        ]

        signal = generate_signal(
            daily,
            previous_session + current_session,
            position_quote=0,
            config=StrategyConfig(momentum_lookback=50),
        )

        self.assertEqual(signal.action, "BUY")
        self.assertIn("reclaimed VWAP", signal.reason)


if __name__ == "__main__":
    unittest.main()
