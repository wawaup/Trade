import unittest

from tradebot.market_quality import MarketQuality, market_quality_allows_trade


class MarketQualityTest(unittest.TestCase):
    def test_rejects_wide_spread(self):
        quality = MarketQuality(best_bid=100, best_ask=100.7)

        allowed, reason = market_quality_allows_trade(quality, max_spread_pct=0.005)

        self.assertFalse(allowed)
        self.assertIn("spread", reason)

    def test_allows_tight_spread(self):
        quality = MarketQuality(best_bid=100, best_ask=100.2)

        allowed, reason = market_quality_allows_trade(quality, max_spread_pct=0.005)

        self.assertTrue(allowed)
        self.assertIn("ok", reason)


if __name__ == "__main__":
    unittest.main()
