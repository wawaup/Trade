import unittest

from tradebot.backtest import BacktestConfig, run_backtest
from tradebot.data import generate_synthetic_spcx
from tradebot.models import Candle
from tradebot.strategy import StrategyConfig


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


class BacktestTest(unittest.TestCase):
    def test_backtest_trades_only_t_bucket_and_keeps_core_budget_separate(self):
        daily = [candle(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)]
        intraday = [
            candle(100, 130, 131, 128, 129, 1000),
            candle(101, 129, 130, 127, 128, 1000),
            candle(102, 128, 132, 127.5, 131.5, 2000),
            candle(103, 131.5, 134, 131, 133.5, 2200),
        ]

        result = run_backtest(
            daily,
            intraday,
            BacktestConfig(starting_quote=3500, core_allocation_pct=0.70),
            StrategyConfig(take_profit_pct=0.012),
        )

        self.assertGreaterEqual(len(result.trades), 1)
        self.assertLessEqual(result.max_t_position_quote, 3500 * 0.30 + 1)
        self.assertGreater(result.ending_equity, 0)

    def test_backtest_respects_buy_grid_spacing(self):
        daily = [candle(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)]
        intraday = [
            candle(100, 130, 131, 128, 129, 1000),
            candle(101, 129, 130, 127, 128, 1000),
            candle(102, 128, 132, 127.5, 131.5, 2000),
            candle(103, 131.5, 132.5, 131, 132.0, 2000),
            candle(104, 132.0, 133, 131.8, 132.4, 2000),
        ]

        result = run_backtest(
            daily,
            intraday,
            BacktestConfig(starting_quote=3500, core_allocation_pct=0.70),
            StrategyConfig(take_profit_pct=0.05),
        )

        buys = [trade for trade in result.trades if trade.side == "BUY"]
        self.assertEqual(len(buys), 1)

    def test_backtest_result_exposes_platform_contract_fields(self):
        daily, intraday = generate_synthetic_spcx()
        result = run_backtest(daily, intraday, BacktestConfig(), StrategyConfig())
        self.assertTrue(result.result_id.startswith("bt_"))
        self.assertEqual(result.engine_version, "tradebot-backtest-v2")
        self.assertIn("fillTiming", result.execution_assumptions)
        self.assertIn("slippageBps", result.config_snapshot)
        self.assertIsInstance(result.order_intents, list)
        self.assertIsInstance(result.risk_events, list)
        self.assertIsInstance(result.core_only_return_pct, float)
        self.assertIsInstance(result.strategy_vs_core_only_alpha, float)

    def test_backtest_records_order_intent_for_buy_signal(self):
        daily = [candle(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)]
        intraday = [
            candle(100, 130, 131, 128, 129, 1000),
            candle(101, 129, 130, 127, 128, 1000),
            candle(102, 128, 132, 127.5, 131.5, 2000),
            candle(103, 131.5, 132, 131, 131.8, 2000),
        ]

        result = run_backtest(
            daily,
            intraday,
            BacktestConfig(starting_quote=3500, core_allocation_pct=0.70),
            StrategyConfig(take_profit_pct=0.05),
        )

        self.assertGreaterEqual(len(result.order_intents), 1)
        intent = result.order_intents[0]
        self.assertEqual(intent["sourceSignal"], "OPEN_T")
        self.assertEqual(intent["side"], "buy")
        # quoteAmount is confidence-scaled; just verify it's a positive amount ≤ base 350
        self.assertGreater(intent["quoteAmount"], 0)
        self.assertLessEqual(intent["quoteAmount"], 350.0)
        self.assertEqual(intent["strategyId"], "t-vwap")
        self.assertTrue(intent["paperOnly"])

    def test_backtest_records_risk_event_when_spread_rejects_signal(self):
        daily = [candle(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)]
        intraday = [
            candle(100, 130, 131, 128, 129, 1000),
            candle(101, 129, 130, 127, 128, 1000),
            candle(102, 128, 132, 127.5, 131.5, 2000),
            candle(103, 131.5, 132, 131, 131.8, 2000),
        ]

        result = run_backtest(
            daily,
            intraday,
            BacktestConfig(
                starting_quote=3500,
                core_allocation_pct=0.70,
                synthetic_spread_bps=100,
                max_spread_pct=0.001,
            ),
            StrategyConfig(take_profit_pct=0.05),
        )

        self.assertGreaterEqual(len(result.risk_events), 1)
        self.assertEqual(result.risk_events[0]["type"], "spread_rejected")
        self.assertIn("spread", result.risk_events[0]["reason"])

    def test_backtest_fills_buy_signal_on_next_bar_open(self):
        daily = [candle(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)]
        intraday = [
            candle(100, 130, 131, 128, 129, 1000),
            candle(101, 129, 130, 127, 128, 1000),
            candle(102, 128, 132, 127.5, 131.5, 2000),
            candle(103, 140, 141, 139, 140.5, 2000),
        ]

        result = run_backtest(
            daily,
            intraday,
            BacktestConfig(starting_quote=3500, core_allocation_pct=0.70, slippage_bps=50),
            StrategyConfig(take_profit_pct=0.05),
        )

        buys = [trade for trade in result.trades if trade.side == "BUY"]
        self.assertGreaterEqual(len(buys), 1)
        self.assertEqual(result.execution_assumptions["fillTiming"], "next_bar_open")
        self.assertAlmostEqual(buys[0].price, 140 * 1.005)

    def test_backtest_rejects_buy_when_max_layers_reached(self):
        daily = [candle(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)]
        intraday = [
            candle(100, 130, 131, 128, 129, 1000),
            candle(101, 129, 130, 127, 128, 1000),
            candle(102, 128, 132, 127.5, 131.5, 2000),
            candle(103, 131.5, 132, 131, 131.8, 2000),
        ]

        result = run_backtest(
            daily,
            intraday,
            BacktestConfig(starting_quote=3500, core_allocation_pct=0.70),
            StrategyConfig(max_layers=0, take_profit_pct=0.05),
        )

        self.assertEqual([trade for trade in result.trades if trade.side == "BUY"], [])
        self.assertTrue(any(event["type"] == "max_layers_rejected" for event in result.risk_events))


if __name__ == "__main__":
    unittest.main()
