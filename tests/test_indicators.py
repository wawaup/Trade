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

        # Pre-market candles with a very different price level that would
        # skew VWAP badly if included.  With reset_utc_hour=13 they must be
        # excluded so that the active-session VWAP sits near 100, not 200+.
        pre_market = [
            full_candle(37 * hour - 120_000, 210, 212, 209, 211, 5000),
            full_candle(37 * hour - 60_000,  210, 213, 210, 212, 5000),
        ]
        active = [
            full_candle(37 * hour,            100, 101, 99,  100, 1000),
            full_candle(37 * hour + 60_000,   100, 101, 98,  98,  1000),
            full_candle(37 * hour + 120_000,  98,  103, 97,  102, 1000),
        ]

        config = StrategyConfig(session_reset_utc_hour=13, momentum_lookback=50)
        signal = generate_signal(daily, pre_market + active, position_quote=0, config=config)

        # If pre-market candles were included, VWAP ≈ 155 and reclaim would fail.
        # Correct behaviour: VWAP ≈ 100, reclaim succeeds → BUY.
        self.assertEqual(signal.action, "BUY")
        self.assertIn("reclaimed VWAP", signal.reason)


if __name__ == "__main__":
    unittest.main()
