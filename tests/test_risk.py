import unittest

from tradebot.risk import coerce_fee_rate, trailing_exit_locks_net_profit


class RiskHelperTests(unittest.TestCase):
    def test_coerce_fee_rate_accepts_decimal_rate(self):
        self.assertEqual(coerce_fee_rate(0.001), 0.001)

    def test_coerce_fee_rate_defensively_interprets_percent_like_value(self):
        self.assertAlmostEqual(coerce_fee_rate(0.1), 0.001)

    def test_coerce_fee_rate_clamps_negative_to_zero(self):
        self.assertEqual(coerce_fee_rate(-0.5), 0.0)

    def test_long_trailing_exit_must_cover_round_trip_fee(self):
        self.assertFalse(
            trailing_exit_locks_net_profit(
                "long",
                entry_price=100.0,
                exit_price=100.10,
                fee_rate=0.001,
            )
        )
        self.assertTrue(
            trailing_exit_locks_net_profit(
                "long",
                entry_price=100.0,
                exit_price=100.30,
                fee_rate=0.001,
            )
        )

    def test_short_trailing_exit_must_cover_round_trip_fee(self):
        self.assertFalse(
            trailing_exit_locks_net_profit(
                "short",
                entry_price=100.0,
                exit_price=99.90,
                fee_rate=0.001,
            )
        )
        self.assertTrue(
            trailing_exit_locks_net_profit(
                "short",
                entry_price=100.0,
                exit_price=99.70,
                fee_rate=0.001,
            )
        )


if __name__ == "__main__":
    unittest.main()
