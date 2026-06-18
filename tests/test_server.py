import json
import unittest

from tradebot.server import build_config_response


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


if __name__ == "__main__":
    unittest.main()
