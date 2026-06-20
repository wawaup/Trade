import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from tradebot.data import write_candles_csv
from tradebot.data_sources import DataSourceFactory
from tradebot.models import Candle
import tradebot.server as _server
from tradebot.server import (
    BACKTEST_RESULTS,
    BACKTEST_STORE,
    PAPER_ACCOUNT,
    PAPER_ORDER_LOG,
    api_request_requires_auth,
    authorization_token_valid,
    build_auth_login_response,
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
    def setUp(self):
        self._auth_env = {
            key: os.environ.get(key)
            for key in ("TRADE_ADMIN_USERNAME", "TRADE_ADMIN_PASSWORD", "TRADE_API_TOKEN")
        }
        os.environ["TRADE_ADMIN_USERNAME"] = "admin"
        os.environ["TRADE_ADMIN_PASSWORD"] = "secret"
        os.environ["TRADE_API_TOKEN"] = "unit-token"

    def tearDown(self):
        for key, value in self._auth_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        BACKTEST_RESULTS.clear()
        BACKTEST_STORE.clear()
        PAPER_ORDER_LOG.clear()
        PAPER_ACCOUNT.cash = 10_000.0
        PAPER_ACCOUNT.positions.clear()
        _server.PAPER_LAST_BUY_PRICE.clear()
        _server.PAPER_T_LAYERS.clear()
        _server.PAPER_POSITION_COST.clear()
        _server.PAPER_DAILY_LOSS = 0.0

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

    def test_auth_login_returns_env_token_for_valid_admin(self):
        status, body = build_auth_login_response(
            json.dumps({"username": "admin", "password": "secret"}).encode("utf-8")
        )

        self.assertEqual(status, 200)
        self.assertEqual(body["token"], "unit-token")
        self.assertEqual(body["username"], "admin")

    def test_auth_login_rejects_invalid_admin_password(self):
        status, body = build_auth_login_response(
            json.dumps({"username": "admin", "password": "wrong"}).encode("utf-8")
        )

        self.assertEqual(status, 401)
        self.assertIn("error", body)

    def test_sensitive_api_paths_require_bearer_token(self):
        self.assertTrue(api_request_requires_auth("POST", "/api/paper/orders"))
        self.assertTrue(api_request_requires_auth("POST", "/api/backtest/run"))
        self.assertTrue(api_request_requires_auth("GET", "/api/state/static"))
        self.assertTrue(api_request_requires_auth("GET", "/api/backtest/results"))
        self.assertFalse(api_request_requires_auth("POST", "/api/auth/login"))
        self.assertFalse(api_request_requires_auth("GET", "/api/health"))
        self.assertTrue(authorization_token_valid("Bearer unit-token"))
        self.assertFalse(authorization_token_valid(""))

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

    def test_klines_response_accepts_csv_paths(self):
        with tempfile.TemporaryDirectory(dir=_server.ROOT / "data") as tmpdir:
            daily_path = Path(tmpdir) / "daily.csv"
            intraday_path = Path(tmpdir) / "intraday.csv"
            write_candles_csv(daily_path, [server_candle(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)])
            write_candles_csv(
                intraday_path,
                [
                    server_candle(100, 130, 131, 128, 129, 1000),
                    server_candle(101, 129, 130, 127, 128, 1000),
                    server_candle(102, 128, 132, 127.5, 131.5, 2000),
                ],
            )

            status, body = build_klines_response(
                "NVDA",
                "1m",
                source="CSV",
                daily_path=str(daily_path),
                intraday_path=str(intraday_path),
            )

        self.assertEqual(status, 200)
        self.assertEqual(body["source"], "CSV")
        self.assertEqual(len(body["candles"]), 3)

    def test_klines_response_rejects_csv_paths_outside_data_dir(self):
        status, body = build_klines_response(
            "NVDA",
            "1m",
            source="CSV",
            daily_path="/etc/passwd",
            intraday_path="/etc/passwd",
        )

        self.assertEqual(status, 400)
        self.assertIn("data directory", body["error"])

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

    def test_build_paper_order_response_applies_risk_limits(self):
        payload = {
            "symbol": "NVDA",
            "side": "buy",
            "quoteAmount": 25_000,
            "price": 100,
            "orderType": "market",
        }

        status, body = build_paper_order_response(json.dumps(payload).encode("utf-8"))

        self.assertEqual(status, 200)
        self.assertEqual(body["order"]["status"], "rejected")
        self.assertIn("max order quote", body["order"]["reason"])
        self.assertEqual(PAPER_ACCOUNT.positions, {})

    def test_build_paper_order_response_rejects_wide_spread(self):
        payload = {
            "symbol": "NVDA",
            "side": "buy",
            "quoteAmount": 350,
            "price": 100,
            "orderType": "market",
            "spreadPct": 0.02,
        }

        status, body = build_paper_order_response(json.dumps(payload).encode("utf-8"))

        self.assertEqual(status, 200)
        self.assertEqual(body["order"]["status"], "rejected")
        self.assertIn("spread", body["order"]["reason"])
        self.assertEqual(PAPER_ACCOUNT.positions, {})

    def test_build_paper_order_response_rejects_stale_market_data(self):
        payload = {
            "symbol": "NVDA",
            "side": "buy",
            "quoteAmount": 350,
            "price": 100,
            "orderType": "market",
            "marketDataAgeSec": 120,
        }

        status, body = build_paper_order_response(json.dumps(payload).encode("utf-8"))

        self.assertEqual(status, 200)
        self.assertEqual(body["order"]["status"], "rejected")
        self.assertIn("stale", body["order"]["reason"])
        self.assertEqual(PAPER_ACCOUNT.positions, {})

    def test_build_paper_order_response_rejects_non_positive_sell_amount(self):
        build_paper_order_response(
            json.dumps(
                {
                    "symbol": "NVDA",
                    "side": "buy",
                    "quoteAmount": 1000,
                    "price": 100,
                    "orderType": "market",
                }
            ).encode("utf-8")
        )

        status, body = build_paper_order_response(
            json.dumps(
                {
                    "symbol": "NVDA",
                    "side": "sell",
                    "quoteAmount": -1,
                    "price": 120,
                    "orderType": "market",
                }
            ).encode("utf-8")
        )

        self.assertEqual(status, 400)
        self.assertIn("quoteAmount", body["error"])
        self.assertGreater(PAPER_ACCOUNT.positions["NVDA"], 0)

    def test_partial_paper_sell_reduces_layer_tracking_and_allows_next_layer(self):
        first_buy = {
            "symbol": "NVDA",
            "side": "buy",
            "quoteAmount": 1000,
            "price": 100,
            "orderType": "market",
        }
        partial_sell = {
            "symbol": "NVDA",
            "side": "sell",
            "quoteAmount": 500,
            "price": 100,
            "orderType": "market",
        }
        next_buy = {
            "symbol": "NVDA",
            "side": "buy",
            "quoteAmount": 350,
            "price": 99,
            "orderType": "market",
        }

        build_paper_order_response(json.dumps(first_buy).encode("utf-8"))
        sell_status, sell_body = build_paper_order_response(json.dumps(partial_sell).encode("utf-8"))

        self.assertEqual(sell_status, 200)
        self.assertEqual(sell_body["order"]["status"], "filled")
        self.assertNotIn("NVDA", _server.PAPER_T_LAYERS)
        self.assertNotIn("NVDA", _server.PAPER_LAST_BUY_PRICE)
        buy_status, buy_body = build_paper_order_response(json.dumps(next_buy).encode("utf-8"))
        self.assertEqual(buy_status, 200)
        self.assertEqual(buy_body["order"]["status"], "filled")

    def test_small_partial_paper_sell_keeps_layer_tracking(self):
        first_buy = {
            "symbol": "NVDA",
            "side": "buy",
            "quoteAmount": 1000,
            "price": 100,
            "orderType": "market",
        }
        small_sell = {
            "symbol": "NVDA",
            "side": "sell",
            "quoteAmount": 100,
            "price": 100,
            "orderType": "market",
        }

        build_paper_order_response(json.dumps(first_buy).encode("utf-8"))
        sell_status, sell_body = build_paper_order_response(json.dumps(small_sell).encode("utf-8"))

        self.assertEqual(sell_status, 200)
        self.assertEqual(sell_body["order"]["status"], "filled")
        self.assertEqual(_server.PAPER_T_LAYERS["NVDA"], 1)
        self.assertEqual(_server.PAPER_LAST_BUY_PRICE["NVDA"], 100)

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
        self.assertIn("assetDetails", result)
        self.assertIn("coreOnlyReturnPct", result["summary"])
        self.assertIn("strategyVsCoreOnlyAlpha", result["summary"])
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

    def test_backtest_detail_persists_every_asset_audit_trail(self):
        status, body = build_backtest_response(
            json.dumps({"symbols": ["NVDA", "TSLA"], "source": "Synthetic"}).encode("utf-8")
        )
        result_id = body["backtest"]["resultId"]

        detail_status, detail = build_backtest_result_detail_response(result_id)

        self.assertEqual(status, 200)
        self.assertEqual(detail_status, 200)
        details = detail["result"]["assetDetails"]
        self.assertEqual({row["symbol"] for row in details}, {"NVDA", "TSLA"})
        for row in details:
            self.assertIn("equityCurve", row)
            self.assertIn("trades", row)
            self.assertIn("orderIntents", row)
            self.assertIn("riskEvents", row)
            self.assertIn("executionAssumptions", row)

    def test_backtest_uses_per_symbol_quote_when_provided(self):
        status, body = build_backtest_response(
            json.dumps({"symbols": ["NVDA"], "source": "Synthetic", "perSymbolQuote": 600}).encode("utf-8")
        )
        self.assertEqual(status, 200)
        # The config snapshot should reflect the provided per-symbol quote
        config = body["backtest"]["configSnapshot"]
        self.assertAlmostEqual(config["startingQuote"], 600.0)

    def test_backtest_uses_total_quote_with_idle_buffer(self):
        # totalQuote=2000, idleBufferPct=0.1, 1 symbol → perAsset = 2000*0.9 = 1800
        status, body = build_backtest_response(
            json.dumps({
                "symbols": ["NVDA"],
                "source": "Synthetic",
                "totalQuote": 2000,
                "idleBufferPct": 0.1,
            }).encode("utf-8")
        )
        self.assertEqual(status, 200)
        self.assertAlmostEqual(body["backtest"]["configSnapshot"]["startingQuote"], 1800.0)

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

    def test_backtest_response_accepts_csv_paths(self):
        with tempfile.TemporaryDirectory(dir=_server.ROOT / "data") as tmpdir:
            daily_path = Path(tmpdir) / "daily.csv"
            intraday_path = Path(tmpdir) / "intraday.csv"
            write_candles_csv(daily_path, [server_candle(i, 100 + i, 101 + i, 99 + i, 100.5 + i) for i in range(30)])
            write_candles_csv(
                intraday_path,
                [
                    server_candle(100, 130, 131, 128, 129, 1000),
                    server_candle(101, 129, 130, 127, 128, 1000),
                    server_candle(102, 128, 132, 127.5, 131.5, 2000),
                    server_candle(103, 131.5, 132, 131, 131.8, 2000),
                ],
            )

            status, body = build_backtest_response(
                json.dumps(
                    {
                        "symbols": ["NVDA"],
                        "source": "CSV",
                        "dailyPath": str(daily_path),
                        "intradayPath": str(intraday_path),
                    }
                ).encode("utf-8")
            )

        self.assertEqual(status, 200)
        self.assertEqual(body["backtest"]["source"], "CSV")

    def test_backtest_result_detail_response_returns_404_for_missing_result(self):
        status, body = build_backtest_result_detail_response("missing")

        self.assertEqual(status, 404)
        self.assertIn("error", body)

    def test_backtest_store_append_is_thread_safe(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = _server.BacktestResultStore(Path(tmpdir) / "backtests.json")
            errors = []

            def append_result(index):
                try:
                    store.append({"resultId": f"bt-{index:04d}"})
                except Exception as exc:  # pragma: no cover - assertion reports details
                    errors.append(exc)

            threads = [threading.Thread(target=append_result, args=(idx,)) for idx in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            results = store.list_recent(limit=25)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 20)
        self.assertEqual({row["resultId"] for row in results}, {f"bt-{idx:04d}" for idx in range(20)})

    def test_walk_forward_rejects_non_positive_window_parameters(self):
        status, body = _server.validate_walk_forward_window(40, 20, 0)

        self.assertEqual(status, 400)
        self.assertIn("positive", body["error"])


if __name__ == "__main__":
    unittest.main()
