import json
import unittest

from tradebot.server import (
    BACKTEST_RESULTS,
    PAPER_ACCOUNT,
    PAPER_ORDER_LOG,
    build_backtest_response,
    build_backtest_results_response,
    build_config_response,
    build_data_sources_response,
    build_paper_orders_response,
    build_paper_order_response,
)


class ServerTest(unittest.TestCase):
    def tearDown(self):
        BACKTEST_RESULTS.clear()
        PAPER_ORDER_LOG.clear()
        PAPER_ACCOUNT.cash = 10_000.0
        PAPER_ACCOUNT.positions.clear()

    def test_build_config_response_accepts_allocation_payload(self):
        payload = {
            "totalAccountQuote": 10_000,
            "symbols": [
                {"symbol": "NVDA", "totalPct": 0.3, "tPct": 0.25},
                {"symbol": "TSLA", "totalPct": 0.2, "tPct": 0.3},
            ],
        }

        status, body = build_config_response(json.dumps(payload).encode("utf-8"))

        self.assertEqual(status, 200)
        self.assertEqual(body["allocation"]["totalAccountQuote"], 10_000)
        self.assertEqual(body["allocationRows"][0]["tBudget"], 750)

    def test_build_data_sources_response(self):
        status, body = build_data_sources_response()
        self.assertEqual(status, 200)
        self.assertIn("sources", body)
        self.assertTrue(any(row["id"] == "Synthetic" for row in body["sources"]))

    def test_build_paper_orders_response(self):
        status, body = build_paper_orders_response()
        self.assertEqual(status, 200)
        self.assertIn("orders", body)
        self.assertIn("account", body)
        self.assertTrue(body["account"]["paperOnly"])

    def test_build_paper_order_response_records_order_and_fill(self):
        payload = {
            "symbol": "NVDA",
            "side": "buy",
            "quoteAmount": 350,
            "price": 100,
            "orderType": "market",
        }

        status, body = build_paper_order_response(json.dumps(payload).encode("utf-8"))

        self.assertEqual(status, 200)
        self.assertEqual(body["order"]["symbol"], "NVDA")
        self.assertEqual(body["order"]["side"], "buy")
        self.assertEqual(body["order"]["status"], "filled")
        self.assertEqual(len(PAPER_ORDER_LOG), 1)
        self.assertGreater(PAPER_ACCOUNT.positions["NVDA"], 0)

    def test_backtest_run_records_result_history(self):
        BACKTEST_RESULTS.clear()

        status, body = build_backtest_response(json.dumps({"symbols": ["NVDA"]}).encode("utf-8"))
        history_status, history = build_backtest_results_response()

        self.assertEqual(status, 200)
        self.assertIn("backtest", body)
        self.assertEqual(history_status, 200)
        self.assertEqual(len(BACKTEST_RESULTS), 1)
        self.assertEqual(len(history["results"]), 1)
        result = history["results"][0]
        self.assertIn("resultId", result)
        self.assertIn("createdAt", result)
        self.assertIn("summary", result)
        self.assertIn("engineVersion", result)
        self.assertEqual(result["summary"], body["backtest"]["summary"])


if __name__ == "__main__":
    unittest.main()
