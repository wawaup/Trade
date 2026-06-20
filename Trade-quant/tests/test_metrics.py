import unittest

from tradebot.metrics import calculate_metrics


class MetricsTest(unittest.TestCase):
    def test_calculates_basic_return_drawdown_and_win_rate(self):
        metrics = calculate_metrics(
            starting_equity=1000,
            equity_curve=[1000, 1030, 1010, 1060],
            trade_pnls=[20, -10, 30],
        )

        self.assertAlmostEqual(metrics.total_return_pct, 0.06)
        self.assertAlmostEqual(metrics.max_drawdown_pct, -20 / 1030)
        self.assertAlmostEqual(metrics.win_rate, 2 / 3)
        self.assertGreater(metrics.profit_factor, 1)


if __name__ == "__main__":
    unittest.main()
