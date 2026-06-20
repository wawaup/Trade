import unittest

from tradebot.indicators import kdj, macd, session_vwap
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


class KDJTests(unittest.TestCase):
    def test_returns_none_when_fewer_than_period_candles(self):
        cs = [candle(i, 100) for i in range(8)]
        self.assertIsNone(kdj(cs, period=9))

    def test_j_above_80_when_price_consistently_near_high(self):
        # All closes at the top of their range → RSV near 100 → J should be well above 80.
        closes = [100 + i for i in range(20)]
        cs = [
            Candle(
                open_time=i, open=c, high=c, low=c - 4, close=c,
                volume=1000, close_time=i + 59999, quote_volume=c * 1000, trades=100,
            )
            for i, c in enumerate(closes)
        ]
        result = kdj(cs)
        self.assertIsNotNone(result)
        k, d, j = result
        self.assertGreater(j, 80)

    def test_j_below_20_when_price_consistently_near_low(self):
        # All closes at the bottom of their range → RSV near 0 → J should be well below 20.
        closes = [100 - i for i in range(20)]
        cs = [
            Candle(
                open_time=i, open=c, high=c + 4, low=c, close=c,
                volume=1000, close_time=i + 59999, quote_volume=c * 1000, trades=100,
            )
            for i, c in enumerate(closes)
        ]
        result = kdj(cs)
        self.assertIsNotNone(result)
        k, d, j = result
        self.assertLess(j, 20)

    def test_j_near_50_when_price_at_midrange(self):
        cs = [
            Candle(
                open_time=i, open=100, high=110, low=90, close=100,
                volume=1000, close_time=i + 59999, quote_volume=100_000, trades=100,
            )
            for i in range(20)
        ]
        result = kdj(cs)
        self.assertIsNotNone(result)
        _, _, j = result
        self.assertGreater(j, 30)
        self.assertLess(j, 70)


class MACDTests(unittest.TestCase):
    def _rising(self, n=60):
        return [candle(i, 100 + i * 0.5) for i in range(n)]

    def _falling(self, n=60):
        return [candle(i, 160 - i * 0.5) for i in range(n)]

    def test_returns_none_when_not_enough_candles(self):
        self.assertIsNone(macd([candle(i, 100) for i in range(34)]))

    def test_histogram_positive_in_sustained_uptrend(self):
        result = macd(self._rising())
        self.assertIsNotNone(result)
        _, _, hist = result
        self.assertGreater(hist, 0)

    def test_histogram_negative_in_sustained_downtrend(self):
        result = macd(self._falling())
        self.assertIsNotNone(result)
        _, _, hist = result
        self.assertLess(hist, 0)

    def test_macd_line_equals_fast_minus_slow_ema_direction(self):
        # In an uptrend fast EMA > slow EMA → macd_line > 0.
        result = macd(self._rising())
        macd_line, _, _ = result
        self.assertGreater(macd_line, 0)


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

    def test_session_vwap_reset_at_utc13_excludes_pre_market_candles(self):
        # Candles before UTC 13:00 are pre-market noise for US stocks.
        # With reset_utc_hour=13, they belong to the previous session and must be excluded.
        hour = 3_600_000
        pre_market = [
            candle(13 * hour, 500, 100),  # exactly UTC 13:00 day 0 = session boundary, NOT day 1
            candle(37 * hour - 1, 500, 100),  # 1 ms before UTC 13:00 day 1 — still previous session
        ]
        active_session = [
            candle(37 * hour, 100, 10),       # UTC 13:00 day 1 — session start
            candle(38 * hour, 102, 30),       # UTC 14:00 day 1
        ]

        result = session_vwap(pre_market + active_session, reset_utc_hour=13)
        self.assertAlmostEqual(result, 101.5)

    def test_session_vwap_with_utc13_reset_via_strategy_config(self):
        # Verify that generate_signal() honours session_reset_utc_hour from StrategyConfig.
        from tradebot.models import Candle as C
        from tradebot.strategy import StrategyConfig, generate_signal

        def full_candle(ts, open_, high, low, close, volume=1000):
            return C(
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

        hour = 3_600_000
        daily = [full_candle(i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(25)]

        # Pre-market candles at high volume but plausible prices (gap ≈1.6% < 4% threshold).
        # If included, their volume dominates VWAP and reclaim would fail (VWAP ≈ 118).
        # With reset_utc_hour=13, they are excluded → VWAP ≈ 100 → reclaim succeeds.
        pre_market = [
            full_candle(37 * hour - 120_000, 123, 124, 122, 123, 5000),
            full_candle(37 * hour - 60_000,  123, 125, 122, 124, 5000),
        ]
        active = [
            full_candle(37 * hour,            100, 101, 99,  100, 1000),
            full_candle(37 * hour + 60_000,   100, 101, 98,   98, 1000),
            full_candle(37 * hour + 120_000,   98, 103, 97,  102, 1000),
        ]

        config = StrategyConfig(session_reset_utc_hour=13, momentum_lookback=50)
        signal = generate_signal(daily, pre_market + active, position_quote=0, config=config)

        # If pre-market candles were included, their high volume pushes VWAP to ≈118 → no reclaim.
        # Correct behaviour: VWAP ≈ 100 (session-only), reclaim succeeds → BUY.
        self.assertEqual(signal.action, "BUY")
        self.assertIn("reclaimed VWAP", signal.reason)


if __name__ == "__main__":
    unittest.main()
