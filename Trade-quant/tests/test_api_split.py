import json
import unittest

from tradebot.server import build_backtest_response, build_klines_response, build_live_state, build_static_state


class ApiSplitTest(unittest.TestCase):
    def test_static_state_excludes_live_and_contains_glossary(self):
        state = build_static_state()

        self.assertIn("backtest", state)
        self.assertIn("glossary", state)
        self.assertIn("allocation", state)
        self.assertNotIn("live", state)

    def test_live_state_excludes_backtest_and_contains_positions(self):
        state = build_live_state()

        self.assertIn("live", state)
        self.assertNotIn("backtest", state)
        self.assertGreater(len(state["live"]["positions"]), 0)

    def test_klines_response_returns_lightweight_arrays(self):
        status, body = build_klines_response("NVDA", "1m")

        self.assertEqual(status, 200)
        self.assertEqual(body["symbol"], "NVDA")
        self.assertIn("candles", body)
        self.assertIn("vwap", body)
        self.assertIsInstance(body["candles"][0], list)
        self.assertEqual(len(body["candles"][0]), 5)

    def test_backtest_run_filters_symbols(self):
        payload = {"symbols": ["NVDA", "TSLA"], "slippageBps": 30, "spreadBps": 20}

        status, body = build_backtest_response(json.dumps(payload).encode("utf-8"))

        self.assertEqual(status, 200)
        self.assertIn("summary", body["backtest"])
        self.assertIn("totalReturnPct", body["backtest"]["summary"])
        self.assertEqual({row["symbol"] for row in body["backtest"]["assets"]}, {"NVDA", "TSLA"})

    def test_default_allocation_is_valid_for_client_side_guardrails(self):
        state = build_static_state()

        self.assertLessEqual(sum(row["totalPct"] for row in state["allocation"]["symbols"]), 1)
        for row in state["allocation"]["symbols"]:
            self.assertLessEqual(row["tPct"], row["totalPct"])


if __name__ == "__main__":
    unittest.main()
