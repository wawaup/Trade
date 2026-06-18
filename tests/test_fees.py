import unittest

from tradebot.fees import BinanceStockFeeModel


class BinanceStockFeeModelTest(unittest.TestCase):
    def test_fixed_fee_below_threshold(self):
        fees = BinanceStockFeeModel()

        self.assertEqual(fees.estimate(50), 0.35)
        self.assertAlmostEqual(fees.round_trip_bps(50), 140.0)

    def test_percent_fee_at_or_above_threshold(self):
        fees = BinanceStockFeeModel()

        self.assertAlmostEqual(fees.estimate(350), 0.35)
        self.assertEqual(fees.estimate(1000), 1.0)
        self.assertAlmostEqual(fees.round_trip_bps(1000), 20.0)


if __name__ == "__main__":
    unittest.main()
