import unittest

from tradebot.models import Candle
from tradebot.strategy import StrategyConfig, core_position_signal, generate_signal, confidence_sized_quote


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
        # confidence=0.55 with floor=0.5 → scale≈0.61 → quote < full 175
        self.assertLess(signal.suggested_quote, 175.0)
        self.assertGreater(signal.suggested_quote, 80.0)

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

    def test_macd_negative_histogram_blocks_buy_signal(self):
        # Slow but decelerating uptrend: daily trend remains up, but MACD
        # histogram turns negative. The intraday bars satisfy the VWAP reclaim
        # setup, so disabling MACD must produce BUY.
        closes = [100 + i * 1.0 for i in range(35)]
        last = closes[-1]
        increments = [0.25, 0.22, 0.20, 0.18, 0.16, 0.14, 0.12, 0.10, 0.08, 0.06]
        for inc in increments:
            last += inc
            closes.append(last)
        daily = [candle(i, close - 0.2, close + 2, close - 2, close) for i, close in enumerate(closes)]

        intraday = [
            candle(1, 120, 121, 118, 119, 1000),
            candle(2, 119, 120, 117, 118, 1200),
            candle(3, 118, 121, 117.5, 120.6, 2000),
        ]

        signal_blocked = generate_signal(
            daily, intraday, position_quote=0,
            config=StrategyConfig(require_macd_histogram_positive=True),
        )
        signal_free = generate_signal(
            daily, intraday, position_quote=0,
            config=StrategyConfig(require_macd_histogram_positive=False),
        )

        self.assertEqual(signal_free.action, "BUY")
        self.assertEqual(signal_blocked.action, "HOLD")
        self.assertIn("MACD histogram negative", signal_blocked.reason)

    def test_macd_filter_disabled_allows_buy(self):
        # With require_macd_histogram_positive=False, MACD is not checked.
        daily = [candle(i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(25)]
        intraday = [
            candle(1, 120, 121, 118, 119, 1000),
            candle(2, 119, 120, 117, 118, 1200),
            candle(3, 118, 121, 117.5, 120.6, 2000),
        ]

        signal = generate_signal(
            daily, intraday, position_quote=0,
            config=StrategyConfig(require_macd_histogram_positive=False),
        )

        self.assertEqual(signal.action, "BUY")

    def test_kdj_overbought_blocks_buy_signal(self):
        # Uptrend + valid pullback reclaim, but intraday price stays near its high
        # the whole time → KDJ J > 80 → strategy should HOLD instead of BUY.
        daily = [candle(i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(25)]
        # All intraday closes at the top of their range so J will be well above 80.
        intraday = [
            Candle(
                open_time=i, open=120 + i, high=120 + i, low=118 + i, close=120 + i,
                volume=1000, close_time=i + 59999, quote_volume=(120 + i) * 1000, trades=100,
            )
            for i in range(20)
        ]

        signal = generate_signal(daily, intraday, position_quote=0, config=StrategyConfig())

        self.assertEqual(signal.action, "HOLD")
        self.assertIn("KDJ overbought", signal.reason)

    def test_kdj_overbought_does_not_block_sell_signal(self):
        # Even when J > 80, existing T position must still be able to hit take-profit.
        daily = [candle(i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(25)]
        intraday = [
            Candle(
                open_time=i, open=120 + i, high=120 + i, low=118 + i, close=120 + i,
                volume=1000, close_time=i + 59999, quote_volume=(120 + i) * 1000, trades=100,
            )
            for i in range(20)
        ]

        signal = generate_signal(
            daily, intraday,
            position_quote=400,
            avg_entry_price=110,  # pnl ≈ +8.5%, well above take_profit_pct
            config=StrategyConfig(take_profit_pct=0.015),
        )

        self.assertEqual(signal.action, "SELL")
        self.assertIn("profit target", signal.reason)

    def test_confidence_scales_suggested_quote_below_base(self):
        # Uptrend pullback with low ATR → confidence ≈ 0.55, floor=0.5 → quote < base 350.
        daily = [
            candle(i, 100 + i, 101 + i, 99 + i, 100 + i)  # tiny range → low ATR
            for i in range(25)
        ]
        intraday = [
            candle(1, 120, 121, 118, 119, 1000),
            candle(2, 119, 120, 117, 118, 1200),
            candle(3, 118, 121, 117.5, 120.6, 2000),
        ]

        signal = generate_signal(daily, intraday, position_quote=0, config=StrategyConfig())

        self.assertEqual(signal.action, "BUY")
        self.assertLess(signal.suggested_quote, 350.0)
        self.assertGreater(signal.suggested_quote, 100.0)

    def test_confidence_size_floor_1_gives_full_quote(self):
        # floor=1.0 disables scaling: suggested_quote always equals base.
        daily = [candle(i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(25)]
        intraday = [
            candle(1, 120, 121, 118, 119, 1000),
            candle(2, 119, 120, 117, 118, 1200),
            candle(3, 118, 121, 117.5, 120.6, 2000),
        ]

        signal = generate_signal(
            daily, intraday, position_quote=0,
            config=StrategyConfig(confidence_size_floor=1.0),
        )

        self.assertEqual(signal.action, "BUY")
        self.assertAlmostEqual(signal.suggested_quote, 350.0)

    def test_stop_loss_uses_atr_multiplier_when_atr_available(self):
        # Daily candles have TR=3 per bar, close≈125 → ATR% ≈ 2.4%.
        # atr_stop_loss_multiplier=1.5 → effective stop ≈ 3.6%.
        # Entry at 100, close at 96.0 → pnl = -4%, beyond 3.6% stop → SELL.
        daily = [
            candle(i, 100 + i, 102 + i, 99 + i, 101 + i)
            for i in range(25)
        ]
        intraday = [
            candle(1, 100, 101, 99, 100, 1000),
            candle(2, 100, 101, 99, 96.0, 1200),
        ]

        signal = generate_signal(
            daily,
            intraday,
            position_quote=400,
            avg_entry_price=100,
            config=StrategyConfig(atr_stop_loss_multiplier=1.5),
        )

        self.assertEqual(signal.action, "SELL")
        self.assertIn("stop loss", signal.reason)
        self.assertIn("stop=", signal.reason)

    def test_stop_loss_falls_back_to_fixed_when_daily_atr_is_zero(self):
        # All daily candles have identical open/high/low/close → ATR = 0.
        # Fall back to stop_loss_pct=0.03; entry 100, close 96.5 → pnl -3.5% → SELL.
        daily = [candle(i, 100, 100, 100, 100) for i in range(25)]
        intraday = [
            candle(1, 100, 101, 99, 100, 1000),
            candle(2, 100, 101, 99, 96.5, 1200),
        ]

        signal = generate_signal(
            daily,
            intraday,
            position_quote=400,
            avg_entry_price=100,
            config=StrategyConfig(stop_loss_pct=0.03, atr_stop_loss_multiplier=1.5),
        )

        self.assertEqual(signal.action, "SELL")
        self.assertIn("stop loss", signal.reason)

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
        # Previous session: prices close to daily[-1].close (≈125, gap ≈3% < 4% threshold)
        # but with high volume so they would skew VWAP badly if included.
        previous_session = [
            candle(8 * hour, 121, 123, 120, 122, 10_000),
            candle(9 * hour, 122, 124, 121, 123, 10_000),
        ]
        # Current session: pull back well below VWAP, then reclaim (1m move kept <5%).
        current_session = [
            candle(32 * hour,            99, 100, 98,   99,   1000),
            candle(32 * hour + 60_000,   99, 100, 97,   98,   1000),
            candle(32 * hour + 120_000,  98, 102, 97,  101.5, 1000),
        ]

        signal = generate_signal(
            daily,
            previous_session + current_session,
            position_quote=0,
            config=StrategyConfig(momentum_lookback=50),
        )

        self.assertEqual(signal.action, "BUY")
        self.assertIn("reclaimed VWAP", signal.reason)


class CorePositionSignalTest(unittest.TestCase):
    def _uptrend_daily(self, n=25):
        return [candle(i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(n)]

    def _downtrend_daily(self, n=25):
        # Sustained decline: MA5 will cross below MA10 quickly
        return [candle(i, 120 - i * 0.8, 121 - i * 0.8, 118 - i * 0.8, 120 - i * 0.8) for i in range(n)]

    def test_hold_at_full_allocation_in_uptrend(self):
        signal = core_position_signal(self._uptrend_daily(), StrategyConfig())
        self.assertEqual(signal.action, "HOLD")
        self.assertAlmostEqual(signal.target_allocation_pct, 1.0)

    def test_reduce_25pct_on_ma5_dead_cross(self):
        # Build: 20 up days then 6 sharp down days → MA5 < MA10 but close still near MA20
        daily = [candle(i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(20)]
        # 6 down bars that drag MA5 below MA10 but close stays above MA20
        for j in range(6):
            p = 120 - j * 0.5  # gentle decline, stays above MA20 ≈ 111
            daily.append(candle(20 + j, p, p + 1, p - 1, p))

        signal = core_position_signal(daily, StrategyConfig())
        self.assertEqual(signal.action, "REDUCE")
        self.assertAlmostEqual(signal.target_allocation_pct, 0.75)

    def test_reduce_to_0_on_3_consecutive_closes_below_ma20(self):
        # Start uptrend then crash hard: last 3 closes well below MA20
        daily = [candle(i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(22)]
        # Crash: close well below MA20 for 3 bars; MA20 ≈ 111
        for j in range(3):
            daily.append(candle(22 + j, 80, 81, 79, 80))

        signal = core_position_signal(daily, StrategyConfig())
        self.assertEqual(signal.action, "REDUCE")
        self.assertAlmostEqual(signal.target_allocation_pct, 0.0)

    def test_exit_all_on_catastrophic_single_day_drop(self):
        # Uptrend then one-day crash of 10%
        daily = [candle(i, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(24)]
        last_close = daily[-1].close
        daily.append(candle(24, last_close, last_close, last_close * 0.88, last_close * 0.88))

        signal = core_position_signal(daily, StrategyConfig(core_catastrophic_pct=0.08))
        self.assertEqual(signal.action, "EXIT_ALL")
        self.assertAlmostEqual(signal.target_allocation_pct, 0.0)
        self.assertIn("catastrophic", signal.reason)

    def test_insufficient_daily_candles_returns_hold(self):
        daily = [candle(i, 100, 101, 99, 100) for i in range(10)]
        signal = core_position_signal(daily, StrategyConfig())
        self.assertEqual(signal.action, "HOLD")
        self.assertAlmostEqual(signal.target_allocation_pct, 1.0)


class OvernightGapTest(unittest.TestCase):
    # Use realistic timestamps: daily bars close before intraday starts.
    # Each daily bar spans 60_000 ms; last daily close_time = 24 * 60_000 + 59_999.
    # Intraday starts at 2_000_000 (well after daily closes).
    _INTRADAY_START = 2_000_000

    def _uptrend_daily(self, n=25):
        return [candle(i * 60_000, 100 + i, 102 + i, 99 + i, 101 + i) for i in range(n)]

    def test_overnight_gap_up_blocks_t_buy(self):
        daily = self._uptrend_daily()
        last_close = daily[-1].close
        t0 = self._INTRADAY_START
        gapped_open = last_close * 1.06  # 6% gap-up
        intraday = [
            candle(t0,           gapped_open, gapped_open + 1, gapped_open - 1, gapped_open, 1000),
            candle(t0 + 60_000,  gapped_open, gapped_open + 2, gapped_open - 1, gapped_open + 1.5, 1200),
        ]
        signal = generate_signal(daily, intraday, position_quote=0, config=StrategyConfig())
        self.assertEqual(signal.action, "HOLD")
        self.assertIn("overnight gap", signal.reason)

    def test_overnight_gap_down_blocks_t_buy(self):
        daily = self._uptrend_daily()
        last_close = daily[-1].close
        t0 = self._INTRADAY_START
        gapped_open = last_close * 0.93  # 7% gap-down
        intraday = [
            candle(t0,           gapped_open, gapped_open + 1, gapped_open - 2, gapped_open - 1, 1000),
            candle(t0 + 60_000,  gapped_open - 1, gapped_open, gapped_open - 2, gapped_open - 0.5, 1200),
        ]
        signal = generate_signal(daily, intraday, position_quote=0, config=StrategyConfig())
        self.assertEqual(signal.action, "HOLD")
        self.assertIn("overnight gap", signal.reason)

    def test_small_gap_does_not_block_buy(self):
        daily = self._uptrend_daily()
        last_close = daily[-1].close
        t0 = self._INTRADAY_START
        # 1% gap — well below the 4% threshold
        gapped_open = last_close * 1.01
        intraday = [
            candle(t0,            gapped_open, gapped_open + 1, gapped_open - 2, gapped_open - 1.5, 1000),
            candle(t0 + 60_000,   gapped_open - 1.5, gapped_open, gapped_open - 2, gapped_open - 0.5, 1200),
            candle(t0 + 120_000,  gapped_open - 0.5, gapped_open + 2, gapped_open - 1, gapped_open + 1.8, 2000),
        ]
        signal = generate_signal(daily, intraday, position_quote=0, config=StrategyConfig(momentum_lookback=50))
        self.assertNotIn("overnight gap", signal.reason)


if __name__ == "__main__":
    unittest.main()
