import json
import unittest

from tradebot.data_sources import DataSourceFactory
from tradebot.models import Candle
from tradebot.server import (
    BACKTEST_RESULTS,
    BACKTEST_STORE,
    PAPER_ACCOUNT,
    PAPER_ORDER_LOG,
    build_backtest_result_detail_response,
    build_backtest_response,
    build_backtest_results_response,
    build_config_response,
    build_data_sources_response,
    build_klines_response,
    build_paper_orders_response,
    build_paper_order_response,
    build_paper_reset_response,
)


def server_candle(ts, open_, high, low, close, volume=1000):
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


class ServerTest(unittest.TestCase):
    def tearDown(self):
        BACKTEST_RESULTS.clear()
        BACKTEST_STORE.clear()
        PAPER_ORDER_LOG.clear()
        PAPER_ACCOUNT.cash = 10_000.0
        PAPER_ACCOUNT.positions.clear()

    def test_build_config_response_accepts_allocation_payload(self):
        payload = {
            "totalAccountQuote": 10_000,
            "symbols": [
                {"symbol": "NVDA", "totalPct": 0.3, "tPct": 0.25},
                {"symbol": "TSLA", "totalPct": 0.2, "tPct": 0.1},
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

    def test_klines_response_routes_through_named_data_source(self):
        status, body = build_klines_response("NVDA", "1m", source="Synthetic")

        self.assertEqual(status, 200)
        self.assertEqual(body["symbol"], "NVDA")
        self.assertEqual(body["resolution"], "1m")
        self.assertEqual(body["source"], "Synthetic")
        self.assertEqual(body["dataStatus"], "demo")
        self.assertGreater(len(body["candles"]), 0)
        self.assertEqual(len(body["candles"][0]), 5)
        self.assertGreater(len(body["vwap"]), 0)

    def test_klines_response_rejects_unknown_data_source(self):
        status, body = build_klines_response("NVDA", "1m", source="mystery")

        self.assertEqual(status, 400)
        self.assertIn("error", body)

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

    def test_build_paper_order_response_uses_synthetic_price_when_missing(self):
        payload = {
            "symbol": "NVDA",
            "side": "buy",
            "quoteAmount": 350,
            "orderType": "market",
        }

        status, body = build_paper_order_response(json.dumps(payload).encode("utf-8"))

        self.assertEqual(status, 200)
        self.assertEqual(body["order"]["status"], "filled")
        self.assertGreater(body["order"]["price"], 0)

    def test_build_paper_reset_response_clears_account_and_orders(self):
        build_paper_order_response(
            json.dumps(
                {
                    "symbol": "NVDA",
                    "side": "buy",
                    "quoteAmount": 350,
                    "price": 100,
                    "orderType": "market",
                }
            ).encode("utf-8")
        )

        status, body = build_paper_reset_response(json.dumps({"cash": 2500}).encode("utf-8"))

        self.assertEqual(status, 200)
        self.assertEqual(body["account"]["cash"], 2500)
        self.assertEqual(body["account"]["positions"], {})
        self.assertEqual(PAPER_ORDER_LOG, [])

    def test_backtest_run_records_result_history(self):
        BACKTEST_RESULTS.clear()

        status, body = build_backtest_response(
            json.dumps({"symbols": ["NVDA"], "source": "Synthetic"}).encode("utf-8")
        )
        history_status, history = build_backtest_results_response()

        self.assertEqual(status, 200)
        self.assertIn("backtest", body)
        self.assertEqual(body["backtest"]["source"], "Synthetic")
        self.assertEqual(history_status, 200)
        self.assertEqual(len(BACKTEST_RESULTS), 1)
        self.assertEqual(len(history["results"]), 1)
        result = history["results"][0]
        self.assertIn("resultId", result)
        self.assertIn("createdAt", result)
        self.assertIn("summary", result)
        self.assertIn("engineVersion", result)
        self.assertIn("configSnapshot", result)
        self.assertIn("executionAssumptions", result)
        self.assertIn("assets", result)
        self.assertIn("equityCurve", result)
        self.assertIn("trades", result)
        self.assertIn("orderIntents", result)
        self.assertIn("riskEvents", result)
        self.assertEqual(result["summary"], body["backtest"]["summary"])

    def test_backtest_result_detail_response_returns_full_persisted_result(self):
        status, body = build_backtest_response(
            json.dumps({"symbols": ["NVDA"], "source": "Synthetic"}).encode("utf-8")
        )
        result_id = body["backtest"]["resultId"]

        detail_status, detail = build_backtest_result_detail_response(result_id)

        self.assertEqual(status, 200)
        self.assertEqual(detail_status, 200)
        self.assertEqual(detail["result"]["resultId"], result_id)
        self.assertEqual(detail["result"]["symbols"], ["NVDA"])
        self.assertEqual(detail["result"]["source"], "Synthetic")
        self.assertIn("configSnapshot", detail["result"])
        self.assertIn("executionAssumptions", detail["result"])
        self.assertGreater(len(detail["result"]["equityCurve"]), 0)
        self.assertIsInstance(detail["result"]["trades"], list)

    def test_backtest_response_rejects_unknown_data_source(self):
        status, body = build_backtest_response(
            json.dumps({"symbols": ["NVDA"], "source": "mystery"}).encode("utf-8")
        )

        self.assertEqual(status, 400)
        self.assertIn("error", body)

    def test_backtest_response_loads_candles_from_selected_data_source(self):
        class UnitDataSource:
            calls = []

            def get_default_candles(self, symbol="NVDA", **kwargs):
                self.calls.append(symbol)
                daily = [server_candle(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)]
                intraday = [
                    server_candle(100, 130, 131, 128, 129, 1000),
                    server_candle(101, 129, 130, 127, 128, 1000),
                    server_candle(102, 128, 132, 127.5, 131.5, 2000),
                ]
                return daily, intraday

        original = DataSourceFactory._SOURCES.get("Unit")
        DataSourceFactory._SOURCES["Unit"] = UnitDataSource
        try:
            status, body = build_backtest_response(
                json.dumps({"symbols": ["NVDA"], "source": "Unit"}).encode("utf-8")
            )
        finally:
            if original is None:
                DataSourceFactory._SOURCES.pop("Unit", None)
            else:
                DataSourceFactory._SOURCES["Unit"] = original

        self.assertEqual(status, 200)
        self.assertEqual(body["backtest"]["source"], "Unit")
        self.assertEqual(UnitDataSource.calls, ["NVDA"])
        self.assertEqual(body["backtest"]["assets"][0]["symbol"], "NVDA")

    def test_backtest_result_detail_response_returns_404_for_missing_result(self):
        status, body = build_backtest_result_detail_response("missing")

        self.assertEqual(status, 404)
        self.assertIn("error", body)


if __name__ == "__main__":
    unittest.main()
