import json
import unittest

from tradebot.server import (
    build_config_response,
    build_data_sources_response,
    build_paper_orders_response,
)


class ServerTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
