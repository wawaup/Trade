import unittest

from tradebot.execution import (
    OrderIntent,
    PaperAccount,
    PaperExecutionAdapter,
    RiskLimits,
    order_intent_from_signal,
)


class ExecutionModelTests(unittest.TestCase):
    def test_order_intent_from_open_signal(self):
        intent = order_intent_from_signal(
            signal="OPEN_T",
            symbol="NVDA",
            quote_amount=350.0,
            strategy_id="t-vwap",
        )
        self.assertEqual(intent.symbol, "NVDA")
        self.assertEqual(intent.side, "buy")
        self.assertEqual(intent.source_signal, "OPEN_T")
        self.assertTrue(intent.paper_only)

    def test_order_intent_from_close_signal_is_reduce_only_sell(self):
        intent = order_intent_from_signal(
            signal="CLOSE_T",
            symbol="NVDA",
            quote_amount=0.0,
            quantity=2.0,
            strategy_id="t-vwap",
        )
        self.assertEqual(intent.side, "sell")
        self.assertTrue(intent.reduce_only)

    def test_paper_buy_updates_cash_and_position(self):
        account = PaperAccount(cash=1000.0)
        adapter = PaperExecutionAdapter(account)
        intent = OrderIntent(symbol="NVDA", side="buy", quote_amount=500.0, source_signal="OPEN_T")
        fill = adapter.execute(intent, price=100.0, fee=1.0)
        self.assertEqual(fill.status, "filled")
        self.assertAlmostEqual(account.cash, 500.0)
        self.assertAlmostEqual(account.positions["NVDA"], 4.99)

    def test_paper_rejects_insufficient_cash(self):
        account = PaperAccount(cash=100.0)
        adapter = PaperExecutionAdapter(account)
        intent = OrderIntent(symbol="NVDA", side="buy", quote_amount=500.0, source_signal="OPEN_T")
        fill = adapter.execute(intent, price=100.0, fee=1.0)
        self.assertEqual(fill.status, "rejected")
        self.assertIn("insufficient cash", fill.reason)

    def test_paper_rejects_t_bucket_cap(self):
        account = PaperAccount(cash=1000.0)
        adapter = PaperExecutionAdapter(account, RiskLimits(max_t_position_quote=300.0))
        intent = OrderIntent(symbol="NVDA", side="buy", quote_amount=500.0, source_signal="OPEN_T")
        fill = adapter.execute(intent, price=100.0, fee=1.0)
        self.assertEqual(fill.status, "rejected")
        self.assertIn("T bucket cap", fill.reason)

    def test_paper_rejects_stale_market_data(self):
        account = PaperAccount(cash=1000.0)
        adapter = PaperExecutionAdapter(account, RiskLimits(market_data_max_age_sec=10.0))
        intent = OrderIntent(symbol="NVDA", side="buy", quote_amount=100.0, source_signal="OPEN_T")

        fill = adapter.execute(intent, price=100.0, market_data_age_sec=30.0)

        self.assertEqual(fill.status, "rejected")
        self.assertIn("stale market data", fill.reason)

    def test_paper_rejects_spread_above_limit(self):
        account = PaperAccount(cash=1000.0)
        adapter = PaperExecutionAdapter(account, RiskLimits(max_spread_pct=0.005))
        intent = OrderIntent(symbol="NVDA", side="buy", quote_amount=100.0, source_signal="OPEN_T")

        fill = adapter.execute(intent, price=100.0, spread_pct=0.01)

        self.assertEqual(fill.status, "rejected")
        self.assertIn("spread", fill.reason)

    def test_paper_rejects_daily_loss_cap_breach(self):
        account = PaperAccount(cash=1000.0)
        adapter = PaperExecutionAdapter(account, RiskLimits(daily_loss_limit_quote=50.0))
        intent = OrderIntent(symbol="NVDA", side="buy", quote_amount=100.0, source_signal="OPEN_T")

        fill = adapter.execute(intent, price=100.0, daily_loss_quote=75.0)

        self.assertEqual(fill.status, "rejected")
        self.assertIn("daily loss", fill.reason)


if __name__ == "__main__":
    unittest.main()
