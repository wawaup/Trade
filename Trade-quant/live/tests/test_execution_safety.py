import importlib.util
import os
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd


os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="trade-quant-mpl-"))

LIVE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = LIVE_DIR / "alpaca_trader.py"


def load_trader_module():
    spec = importlib.util.spec_from_file_location("alpaca_trader_safety_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ExecutionSafetyTests(unittest.TestCase):
    def test_market_order_defaults_to_opg_with_stable_client_order_id(self):
        trader = load_trader_module()

        order = trader.build_market_order(
            "AAPL",
            3,
            trader.OrderSide.BUY,
            "2026-06-22",
            "enter",
        )

        self.assertEqual(order.time_in_force, trader.TimeInForce.DAY)
        self.assertEqual(order.client_order_id, "tq-20260622-enter-aapl")

    def test_live_mode_requires_explicit_confirmation(self):
        trader = load_trader_module()

        with patch.object(trader, "PAPER", False), patch.dict(os.environ, {"LIVE_CONFIRM": ""}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "LIVE_CONFIRM=YES"):
                trader.ensure_live_confirmation()

        with patch.object(trader, "PAPER", False), patch.dict(os.environ, {"LIVE_CONFIRM": "YES"}, clear=False):
            trader.ensure_live_confirmation()

    def test_validate_panel_rejects_missing_benchmarks(self):
        trader = load_trader_module()
        idx = pd.bdate_range("2026-06-01", periods=10)
        close = pd.DataFrame({"AAPL": range(10), "QQQ": range(10)}, index=idx)

        with self.assertRaisesRegex(RuntimeError, "SPY"):
            trader.validate_panel(close)

    def test_validate_panel_rejects_stale_bar_when_today_expected(self):
        trader = load_trader_module()
        yesterday = trader.date.today() - trader.timedelta(days=1)
        idx = pd.bdate_range(end=yesterday, periods=200)
        cols = {f"SYM{i}": range(200) for i in range(trader.MIN_VALID_SYMBOLS)}
        cols["QQQ"] = range(200)
        cols["SPY"] = range(200)
        close = pd.DataFrame(cols, index=idx)

        with self.assertRaisesRegex(RuntimeError, "滞后"):
            trader.validate_panel(close, expect_today=True)

        # 不要求今日 bar 时，同样的数据应正常通过
        trader.validate_panel(close, expect_today=False)

    def test_duplicate_signal_date_blocks_real_orders(self):
        trader = load_trader_module()
        state = {"last_order_signal_date": "2026-06-22"}

        self.assertTrue(trader.is_duplicate_signal(state, "2026-06-22", dry_run=False, allow_duplicate=False))
        self.assertFalse(trader.is_duplicate_signal(state, "2026-06-22", dry_run=True, allow_duplicate=False))
        self.assertFalse(trader.is_duplicate_signal(state, "2026-06-22", dry_run=False, allow_duplicate=True))

    def test_audit_writer_records_run_signal_and_order_rows(self):
        trader = load_trader_module()
        with tempfile.TemporaryDirectory() as tmp:
            audit = trader.AuditWriter(Path(tmp))
            audit.append_run({
                "run_id": "run-1",
                "signal_date": "2026-06-22",
                "status": "ok",
                "equity": "100000.00",
                "target_symbols": "AAPL",
            })
            audit.append_signal_rows("run-1", "2026-06-22", pd.Series({"AAPL": 1.23}), {"AAPL": 200.0})
            audit.append_order({
                "run_id": "run-1",
                "signal_date": "2026-06-22",
                "action": "BUY",
                "symbol": "AAPL",
            })

            self.assertIn("run-1", (Path(tmp) / "paper_runs.csv").read_text())
            self.assertIn("100000.00", (Path(tmp) / "paper_runs.csv").read_text())
            self.assertIn("AAPL", (Path(tmp) / "signals.csv").read_text())
            self.assertIn("BUY", (Path(tmp) / "orders.csv").read_text())

    def test_audit_writer_records_empty_signal_snapshot(self):
        trader = load_trader_module()
        with tempfile.TemporaryDirectory() as tmp:
            audit = trader.AuditWriter(Path(tmp))
            audit.append_signal_rows("run-1", "2026-06-22", pd.Series(dtype=float), {})

            content = (Path(tmp) / "signals.csv").read_text()
            self.assertIn("NO_CANDIDATE", content)

    def test_existing_positions_receive_stop_orders(self):
        trader = load_trader_module()

        class Position:
            symbol = "AAPL"
            qty = "10"
            avg_entry_price = "100"

        class Client:
            def __init__(self):
                self.orders = []

            def submit_order(self, req):
                self.orders.append(req)
                return type("Order", (), {"id": "stop-1", "status": "accepted"})()

        client = Client()
        submitted = trader.ensure_stop_orders_for_positions(client, [Position()], "2026-06-22", dry_run=False)

        self.assertEqual(submitted, 1)
        self.assertEqual(client.orders[0].time_in_force, trader.TimeInForce.GTC)
        self.assertEqual(client.orders[0].stop_price, 75.0)

    def test_rebalance_raises_when_broker_rejects_buy_order(self):
        trader = load_trader_module()
        idx = pd.bdate_range("2026-06-01", periods=3)
        close = pd.DataFrame({"AMAT": [580.0, 582.0, 585.71]}, index=idx)

        class Client:
            def get_all_positions(self):
                return []

            def submit_order(self, req):
                raise RuntimeError('{"code":40310000,"message":"opg orders must be submitted after 7:00pm and before 9:28am"}')

        with tempfile.TemporaryDirectory() as tmp:
            audit = trader.AuditWriter(Path(tmp))
            with (
                patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0),
                self.assertRaisesRegex(RuntimeError, "opg orders"),
            ):
                trader.rebalance(
                    Client(),
                    ["AMAT"],
                    close,
                    5000.0,
                    5000.0,
                    dry_run=False,
                    signal_date="2026-06-23",
                    run_id="run-opg-reject",
                    audit=audit,
                    order_plan=[],
                )

    def test_rebalance_cancels_existing_stop_orders_before_selling(self):
        """回归用例：legacy rebalance()（--phase both 手动路径）在提交清仓/减仓卖单前
        也必须先撤销该标的已有的 GTC 止损单，否则会因 qty_available 被占满而被拒
        （HIGH-1 的同根因问题，此前只修了 execute_sell_phase，遗漏了 legacy 路径）。"""
        trader = load_trader_module()
        idx = pd.bdate_range("2026-06-01", periods=3)
        close = pd.DataFrame({
            "AMAT": [580.0, 582.0, 585.71],
            "NVDA": [48.0, 49.0, 50.0],
        }, index=idx)

        class Position:
            symbol = "AMAT"
            qty = "10"
            market_value = "5800"
            avg_entry_price = "500"

        class StopOrder:
            id = "stop-amat-1"
            client_order_id = "tq-stop-amat"
            symbol = "AMAT"

        class Client:
            def __init__(self):
                self.cancelled = []
                self.submitted = []

            def get_all_positions(self):
                return [Position()]

            def get_orders(self, req):
                return [StopOrder()]

            def cancel_order_by_id(self, order_id):
                self.cancelled.append(order_id)

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "order-1", "status": "accepted"})()

        client = Client()
        with tempfile.TemporaryDirectory() as tmp:
            audit = trader.AuditWriter(Path(tmp))
            with patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
                trader.rebalance(
                    client, ["NVDA"], close, 5000.0, 5000.0,
                    dry_run=False, signal_date="2026-06-23",
                    run_id="run-stop-cancel", audit=audit, order_plan=[],
                )

        self.assertEqual(client.cancelled, ["stop-amat-1"])
        sell_orders = [o for o in client.submitted if o.symbol == "AMAT"]
        self.assertEqual(len(sell_orders), 1)
        self.assertEqual(sell_orders[0].side, trader.OrderSide.SELL)

    def test_email_subject_separates_daily_report_and_emergency_alerts(self):
        trader = load_trader_module()

        self.assertIn("普通日报-运行成功", trader.build_email_subject("ok", "run-1", paper=True))
        self.assertIn("普通日报-非调仓日", trader.build_email_subject("skipped_rebalance_interval", "run-1", paper=True))
        self.assertIn("紧急报警-API连接失败", trader.build_email_subject("api_connection_failed", "run-1", paper=True))
        self.assertIn("紧急报警-行情数据异常", trader.build_email_subject("market_data_failed", "run-1", paper=True))
        self.assertIn("紧急报警-数据校验失败", trader.build_email_subject("data_validation_failed", "run-1", paper=True))
        self.assertIn("紧急报警-下单失败", trader.build_email_subject("order_submit_failed", "run-1", paper=True))
        self.assertIn("紧急报警-熔断触发", trader.build_email_subject("kill_switch_triggered", "run-1", paper=True))
        self.assertIn("紧急报警-服务失效", trader.build_email_subject("service_stale", "run-1", paper=True))
        self.assertIn("紧急报警-配置缺失", trader.build_email_subject("missing_api_key", "run-1", paper=True))
        self.assertIn("紧急报警-脚本异常", trader.build_email_subject("error", "run-1", paper=True))

    def test_emergency_status_policy(self):
        trader = load_trader_module()

        self.assertFalse(trader.is_emergency_status("ok"))
        self.assertFalse(trader.is_emergency_status("skipped_rebalance_interval"))
        self.assertTrue(trader.is_emergency_status("error"))
        self.assertTrue(trader.is_emergency_status("api_connection_failed"))
        self.assertTrue(trader.is_emergency_status("market_data_failed"))
        self.assertTrue(trader.is_emergency_status("data_validation_failed"))
        self.assertTrue(trader.is_emergency_status("order_submit_failed"))
        self.assertTrue(trader.is_emergency_status("kill_switch_locked"))
        self.assertTrue(trader.is_emergency_status("service_stale"))

    def test_email_includes_standard_deliverability_headers(self):
        trader = load_trader_module()

        class FakeSMTP:
            sent_message = None

            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def starttls(self, context):
                pass

            def login(self, username, password):
                pass

            def send_message(self, msg):
                FakeSMTP.sent_message = msg

        with (
            patch.object(trader, "EMAIL_ENABLED", True),
            patch.object(trader, "EMAIL_SMTP_HOST", "smtp.example.test"),
            patch.object(trader, "EMAIL_USERNAME", "sender@example.test"),
            patch.object(trader, "EMAIL_PASSWORD", "secret"),
            patch.object(trader, "EMAIL_FROM", "sender@example.test"),
            patch.object(trader, "EMAIL_TO", "receiver@example.test"),
            patch.object(trader.smtplib, "SMTP", FakeSMTP),
        ):
            trader.send_email("测试标题", "测试正文")

        self.assertIsNotNone(FakeSMTP.sent_message)
        self.assertTrue(FakeSMTP.sent_message["Date"])
        self.assertTrue(FakeSMTP.sent_message["Message-ID"])
        self.assertIn("@quant.system", FakeSMTP.sent_message["Message-ID"])

    def test_daily_email_body_uses_fixed_operational_template(self):
        trader = load_trader_module()

        class Position:
            symbol = "AAPL"
            qty = "10"
            market_value = "2500.50"
            avg_entry_price = "200.0"
            unrealized_pl = "500.5"

        body = trader.build_daily_email_body({
            "run_id": "run-1",
            "mode": "Paper",
            "dry_run": False,
            "signal_date": "2026-06-22",
            "equity": 100000.0,
            "buying_power": 400000.0,
            "high_watermark": 102000.0,
            "drawdown": -0.0196,
            "regime": "牛市",
            "qqq_close": 737.74,
            "qqq_ma50": 695.74,
            "candidates": pd.Series({"IONQ": 2.3456, "COIN": 1.8765}),
            "latest_prices": {"IONQ": 44.12, "COIN": 310.55},
            "target_syms": ["IONQ", "COIN"],
            "positions": [Position()],
            "order_plan": [{
                "action": "BUY",
                "symbol": "IONQ",
                "qty": 100,
                "time_in_force": "opg",
                "status": "planned",
                "message": "reference_price=44.1200",
            }],
            "fills_recorded": 1,
            "stop_orders_submitted": 1,
            "kill_switch": False,
            "attachments": ["paper_runs.csv", "signals.csv"],
            "log_tail": "最近日志内容",
        })

        expected_sections = [
            "账户概览",
            "市场状态",
            "今日候选股",
            "目标持仓",
            "当前持仓",
            "操作记录",
            "成交/滑点",
            "风控状态",
            "附件说明",
        ]
        self.assertEqual(expected_sections, [line[3:] for line in body.splitlines() if line.startswith("## ")][:9])
        self.assertIn("账户净值: $100,000.00", body)
        self.assertIn("可用资金: $400,000.00", body)
        self.assertIn("QQQ=737.74", body)
        self.assertIn("IONQ  score=2.3456  close=44.12", body)
        self.assertIn("AAPL qty=10 market_value=$2,500.50", body)
        self.assertIn("BUY IONQ qty=100 tif=opg status=planned", body)
        self.assertIn("fills_recorded: 1", body)
        self.assertIn("stop_orders_submitted: 1", body)
        self.assertIn("paper_runs.csv, signals.csv", body)

    def test_load_trading_universe_builds_missing_universe_file(self):
        trader = load_trader_module()
        with tempfile.TemporaryDirectory() as tmp:
            universe_path = Path(tmp) / "universe.json"
            built = {
                "symbols": ["AAPL", "NVDA"],
                "benchmarks": ["SPY", "QQQ"],
            }

            with (
                patch.object(trader, "UNIVERSE_PATH", universe_path),
                patch.object(trader, "build_universe_dict", return_value=built),
                patch.object(trader, "save_universe_dict") as save_universe,
            ):
                symbols, benchmarks = trader.load_trading_universe()

        self.assertEqual(symbols, ["AAPL", "NVDA"])
        self.assertEqual(benchmarks, ["SPY", "QQQ"])
        save_universe.assert_called_once_with(built)

    def test_load_trading_universe_prefers_existing_universe_file(self):
        trader = load_trader_module()
        with tempfile.TemporaryDirectory() as tmp:
            universe_path = Path(tmp) / "universe.json"
            universe_path.write_text(json.dumps({
                "symbols": ["MSFT"],
                "benchmarks": ["SPY", "QQQ"],
            }), encoding="utf-8")

            with (
                patch.object(trader, "UNIVERSE_PATH", universe_path),
                patch.object(trader, "build_universe_dict") as build_universe,
            ):
                symbols, benchmarks = trader.load_trading_universe()

        self.assertEqual(symbols, ["MSFT"])
        self.assertEqual(benchmarks, ["SPY", "QQQ"])
        build_universe.assert_not_called()

    def test_sell_phase_all_orders_failed_keeps_cycle_plan(self):
        trader = load_trader_module()

        class FakeQuotes(dict):
            pass

        class FakeDataClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_stock_latest_quote(self, req):
                return FakeQuotes()

        class Client:
            def submit_order(self, req):
                raise RuntimeError("broker rejected order")

        state = {
            trader.CYCLE_PLAN_KEY: {
                "plan_date": "2026-06-22",
                "signal_date": "2026-06-22",
                "close_all": [{"symbol": "AMAT", "qty": 3, "market_value": 500.0}],
                "trim": [],
                "buy": [{"symbol": "NVDA", "qty": 2, "price": 100.0, "is_new": True, "drift": 0.0}],
            }
        }

        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient):
            summary = trader.execute_sell_phase(Client(), state, "run-sell-fail", dry_run=False)

        self.assertTrue(summary["all_failed"])
        self.assertEqual(summary["orders"], [])
        self.assertIn(trader.CYCLE_PLAN_KEY, state)
        self.assertNotIn(trader.PENDING_SELL_KEY, state)

    def test_sell_phase_partial_success_advances_cycle(self):
        trader = load_trader_module()

        class FakeDataClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_stock_latest_quote(self, req):
                return {}

        class Client:
            def submit_order(self, req):
                return type("Order", (), {"id": "sell-1", "status": "accepted"})()

        state = {
            trader.CYCLE_PLAN_KEY: {
                "plan_date": "2026-06-22",
                "signal_date": "2026-06-22",
                "close_all": [{"symbol": "AMAT", "qty": 3, "market_value": 500.0}],
                "trim": [],
                "buy": [],
            }
        }

        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient):
            summary = trader.execute_sell_phase(Client(), state, "run-sell-ok", dry_run=False)

        self.assertFalse(summary["all_failed"])
        self.assertEqual(len(summary["orders"]), 1)
        self.assertNotIn(trader.CYCLE_PLAN_KEY, state)
        self.assertIn(trader.PENDING_SELL_KEY, state)

    def test_sell_phase_cancels_existing_stop_orders_before_selling(self):
        """回归用例：持仓已有 GTC 止损单时，sell 阶段必须先撤单，否则 qty 会被
        止损单占满导致卖单被券商以 insufficient qty available 拒绝（HIGH-1）。"""
        trader = load_trader_module()

        class FakeDataClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_stock_latest_quote(self, req):
                return {}

        class StopOrder:
            id = "stop-amat-1"
            client_order_id = "tq-stop-amat"
            symbol = "AMAT"

        class Client:
            def __init__(self):
                self.cancelled = []
                self.submitted = []

            def get_orders(self, req):
                return [StopOrder()]

            def cancel_order_by_id(self, order_id):
                self.cancelled.append(order_id)

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "sell-1", "status": "accepted"})()

        state = {
            trader.CYCLE_PLAN_KEY: {
                "plan_date": "2026-06-22",
                "signal_date": "2026-06-22",
                "close_all": [{"symbol": "AMAT", "qty": 3, "market_value": 500.0}],
                "trim": [],
                "buy": [],
            }
        }

        client = Client()
        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient):
            summary = trader.execute_sell_phase(client, state, "run-sell-stop", dry_run=False)

        self.assertEqual(client.cancelled, ["stop-amat-1"])
        self.assertEqual(len(client.submitted), 1)
        self.assertFalse(summary["all_failed"])

    def test_buy_phase_writes_retry_plan_for_unconfirmed_sell_fills(self):
        """回归用例：卖单未确认完全成交时，剩余未卖出数量必须写回 cycle_plan
        供下一轮 sell 阶段重试，而不是被静默丢弃导致目标仓位永久跑偏（MEDIUM-2）。"""
        trader = load_trader_module()

        class Order:
            def __init__(self, filled_qty, status="filled"):
                self.filled_qty = filled_qty
                self.status = status

        class Account:
            buying_power = "100000"

        class Client:
            def get_order_by_client_id(self, cid):
                if cid == "tq-2026-06-22-close-amat":
                    return Order(1)  # 只成交 1/3，剩余 2 股未卖出
                return Order(2)

            def get_account(self):
                return Account()

            def submit_order(self, req):
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

            def get_all_positions(self):
                return []

        state = {
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-06-23",
                "signal_date": "2026-06-22",
                "orders": [
                    {"symbol": "AMAT", "qty": 3, "client_order_id": "tq-2026-06-22-close-amat", "kind": "close"},
                ],
                "buy_carry": [
                    {"symbol": "AMAT", "qty": 2, "price": 100.0, "is_new": False, "drift": 0.0},
                    {"symbol": "NVDA", "qty": 2, "price": 50.0, "is_new": True, "drift": 0.0},
                ],
            }
        }

        summary = trader.execute_buy_phase(Client(), state, "run-buy-retry", dry_run=False)

        self.assertTrue(summary.get("retry_plan_written"))
        self.assertNotIn(trader.PENDING_SELL_KEY, state)
        self.assertIn(trader.CYCLE_PLAN_KEY, state)
        retry_plan = state[trader.CYCLE_PLAN_KEY]
        self.assertEqual(retry_plan["close_all"], [{"symbol": "AMAT", "qty": 2}])
        self.assertIsInstance(retry_plan["close_all"][0]["qty"], int)
        self.assertEqual(retry_plan["trim"], [])
        self.assertEqual([b["symbol"] for b in retry_plan["buy"]], ["AMAT"])
        self.assertFalse(retry_plan["buy"][0]["is_new"])

    def test_buy_phase_merges_retry_plan_with_preexisting_cycle_plan(self):
        """回归用例：sell 阶段可能已经因为某标的提交失败而写回了一份只含该标的的
        cycle_plan（如 BAD）；buy 阶段随后因另一标的（如 GOOD）卖单未确认成交也要
        写回重试计划时，绝不能无条件覆盖掉 BAD 的条目——否则 BAD 的清仓决策会被
        静默顶掉、永久丢失，且不会有任何报警（此前只在提交失败当天报过一次）。"""
        trader = load_trader_module()

        class Order:
            def __init__(self, filled_qty, status="filled"):
                self.filled_qty = filled_qty
                self.status = status

        class Account:
            buying_power = "100000"

        class Client:
            def get_order_by_client_id(self, cid):
                return Order(1)  # GOOD 只成交 1/3，剩余 2 股未卖出

            def get_account(self):
                return Account()

            def submit_order(self, req):
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

            def get_all_positions(self):
                return []

        state = {
            # sell 阶段此前因 BAD 提交失败写回的重试计划，尚未被任何后续 sell 阶段消费
            trader.CYCLE_PLAN_KEY: {
                "plan_date": "2026-07-02",
                "signal_date": "2026-06-22",
                "close_all": [{"symbol": "BAD", "qty": 2}],
                "trim": [],
                "buy": [],
            },
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-06-23",
                "signal_date": "2026-06-22",
                "orders": [
                    {"symbol": "GOOD", "qty": 3, "client_order_id": "tq-2026-06-22-close-good", "kind": "close"},
                ],
                "buy_carry": [
                    {"symbol": "GOOD", "qty": 2, "price": 100.0, "is_new": False, "drift": 0.0},
                ],
            },
        }

        summary = trader.execute_buy_phase(Client(), state, "run-buy-merge", dry_run=False)

        self.assertTrue(summary.get("retry_plan_written"))
        retry_plan = state[trader.CYCLE_PLAN_KEY]
        retry_symbols = {c["symbol"] for c in retry_plan["close_all"]}
        self.assertEqual(retry_symbols, {"BAD", "GOOD"})

    def test_sell_phase_retry_plan_uses_fresh_client_order_id_not_original(self):
        """回归用例：写回的重试 cycle_plan 再次进入 sell 阶段时，必须生成与原始
        卖单不同的 client_order_id，否则会被券商当作重复提交而实际从未真正下单，
        导致重试永远空转、周期死锁（HIGH-1 的具体触发路径）。"""
        trader = load_trader_module()

        original_cid = "tq-20260622-close-amat"

        class FakeDataClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_stock_latest_quote(self, req):
                return {}

        class Client:
            def __init__(self):
                self.submitted = []

            def submit_order(self, req):
                if req.client_order_id == original_cid:
                    raise RuntimeError("client order id already exists")
                self.submitted.append(req)
                return type("Order", (), {"id": "sell-retry-1", "status": "accepted"})()

        # 模拟 execute_buy_phase 写回的重试计划：signal_date 沿用原始信号日，
        # plan_date 是重试当天（与原始 plan_date/signal_date 不同）
        retry_plan = {
            "plan_date": "2026-06-24",
            "signal_date": "2026-06-22",
            "close_all": [{"symbol": "AMAT", "qty": 2}],
            "trim": [],
            "buy": [],
        }
        state = {trader.CYCLE_PLAN_KEY: retry_plan}

        client = Client()
        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient):
            summary = trader.execute_sell_phase(client, state, "run-sell-retry", dry_run=False)

        self.assertEqual(len(client.submitted), 1)
        self.assertNotEqual(client.submitted[0].client_order_id, original_cid)
        self.assertFalse(summary["all_failed"])

    def test_buy_phase_skips_symbols_with_unconfirmed_sell_fills(self):
        trader = load_trader_module()

        class Order:
            def __init__(self, filled_qty, status="filled"):
                self.filled_qty = filled_qty
                self.status = status

        class Account:
            buying_power = "100000"

        class Client:
            def __init__(self):
                self.submitted = []

            def get_order_by_client_id(self, cid):
                if cid == "tq-2026-06-22-close-amat":
                    return Order(1)  # 只成交 1/3，未完全成交
                return Order(2)

            def get_account(self):
                return Account()

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

        state = {
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-06-23",
                "signal_date": "2026-06-22",
                "orders": [
                    {"symbol": "AMAT", "qty": 3, "client_order_id": "tq-2026-06-22-close-amat", "kind": "close"},
                ],
                "buy_carry": [
                    {"symbol": "AMAT", "qty": 2, "price": 100.0, "is_new": False, "drift": 0.0},
                    {"symbol": "NVDA", "qty": 2, "price": 50.0, "is_new": True, "drift": 0.0},
                ],
            }
        }

        client = Client()
        summary = trader.execute_buy_phase(client, state, "run-buy-issue", dry_run=False)

        self.assertTrue(summary["had_fill_issues"])
        self.assertEqual(len(summary["fill_issues"]), 1)
        bought_syms = [o["symbol"] for o in summary["orders"]]
        self.assertNotIn("AMAT", bought_syms)
        self.assertIn("NVDA", bought_syms)

    def test_buy_phase_dispatch_marks_emergency_status_on_fill_issues(self):
        trader = load_trader_module()

        self.assertTrue(trader.is_emergency_status("buy_completed_with_issues"))
        self.assertIn("紧急报警", trader.build_email_subject("buy_completed_with_issues", "run-1", paper=True))

    def test_buy_phase_attaches_stop_orders_after_buy(self):
        trader = load_trader_module()

        class Position:
            symbol = "NVDA"
            qty = "2"
            avg_entry_price = "50"

        class Account:
            buying_power = "100000"

        class Client:
            def __init__(self):
                self.stop_orders = []

            def get_order_by_client_id(self, cid):
                return type("Order", (), {"filled_qty": 3, "status": "filled"})()

            def get_account(self):
                return Account()

            def get_orders(self, req):
                return []

            def submit_order(self, req):
                if getattr(req, "stop_price", None) is not None:
                    self.stop_orders.append(req)
                return type("Order", (), {"id": "o-1", "status": "accepted"})()

            def get_all_positions(self):
                return [Position()]

        state = {
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-06-23",
                "signal_date": "2026-06-22",
                "orders": [
                    {"symbol": "AMAT", "qty": 3, "client_order_id": "tq-2026-06-22-close-amat", "kind": "close"},
                ],
                "buy_carry": [
                    {"symbol": "NVDA", "qty": 2, "price": 50.0, "is_new": True, "drift": 0.0},
                ],
            }
        }

        client = Client()
        summary = trader.execute_buy_phase(client, state, "run-buy-stop", dry_run=False)

        self.assertEqual(summary["stop_orders_submitted"], 1)
        self.assertEqual(len(client.stop_orders), 1)

    def test_kill_switch_locked_force_closes_remaining_positions(self):
        trader = load_trader_module()

        class Position:
            symbol = "AMAT"

        class Client:
            def __init__(self):
                self.closed_all = False

            def get_all_positions(self):
                return [Position()]

            def close_all_positions(self, cancel_orders=True):
                self.closed_all = True

        client = Client()
        remaining = client.get_all_positions()
        self.assertTrue(remaining)
        client.close_all_positions(cancel_orders=True)
        self.assertTrue(client.closed_all)

    def test_kill_switch_after_hours_client_order_id_includes_date_and_handles_duplicate(self):
        trader = load_trader_module()

        class Position:
            symbol = "AMAT"
            qty = "3"
            current_price = "100"
            avg_entry_price = "100"

        class Client:
            def __init__(self):
                self.submitted_ids = []

            def cancel_orders(self):
                pass

            def submit_order(self, req):
                self.submitted_ids.append(req.client_order_id)
                if len(self.submitted_ids) == 1:
                    return type("Order", (), {"id": "ks-1"})()
                raise RuntimeError("order already exists with client_order_id")

        client = Client()
        today_slug = trader._slug_date(str(trader.date.today()))
        with patch.object(trader, "_is_after_hours", return_value=True), \
             patch.object(trader, "KILL_SWITCH_LIMIT_SLIPPAGE", 0.1):
            trader._kill_switch_liquidate(client, [Position(), Position()], dry_run=False)

        self.assertEqual(len(client.submitted_ids), 2)
        for cid in client.submitted_ids:
            self.assertTrue(cid.startswith(f"tq-ks-{today_slug}-amat"))

    def test_compute_rebalance_plan_skips_symbol_with_price_spike(self):
        trader = load_trader_module()
        idx = pd.bdate_range("2026-06-01", periods=3)
        close = pd.DataFrame({
            "AMAT": [100.0, 101.0, 102.0],
            "NVDA": [50.0, 51.0, 200.0],  # 相对前一日暴涨 >50%，疑似脏数据
        }, index=idx)

        class Client:
            def get_all_positions(self):
                return []

        with patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
            plan = trader.compute_rebalance_plan(
                Client(), ["AMAT", "NVDA"], close, equity=10000.0, buying_power=10000.0,
                signal_date="2026-06-03",
            )

        bought_syms = [b["symbol"] for b in plan["buy"]]
        self.assertIn("AMAT", bought_syms)
        self.assertNotIn("NVDA", bought_syms)

    def test_log_tail_redacts_sensitive_lines(self):
        trader = load_trader_module()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "trader.log"
            log_path.write_text(
                "普通日志行\n"
                "ALPACA_API_KEY=abc123 已加载\n"
                "另一行正常日志\n",
                encoding="utf-8",
            )
            with patch.object(trader, "LOG_FILE", log_path):
                tail = trader._read_log_tail(10)

        self.assertIn("普通日志行", tail)
        self.assertIn("另一行正常日志", tail)
        self.assertNotIn("abc123", tail)
        self.assertIn("已脱敏", tail)

    def test_get_upcoming_earnings_times_out_and_flags_degraded(self):
        """多数标的的 yf.Ticker(...).calendar 调用挂起时，应在超时时间内返回，
        且 degraded=True 用于提示财报避雷本次可能未完全生效，而不是无限期挂起主流程。"""
        trader = load_trader_module()
        import time as _time

        class SlowTicker:
            def __init__(self, sym):
                self.sym = sym

            @property
            def calendar(self):
                if self.sym in ("SLOW1", "SLOW2", "SLOW3"):
                    _time.sleep(5)  # 远大于测试用的超短超时
                    return None
                return {"Earnings Date": []}

        with patch.object(trader, "EARNINGS_LOOKUP_TIMEOUT_SEC", 0.2), \
             patch.object(trader.yf, "Ticker", side_effect=SlowTicker):
            start = _time.monotonic()
            blackout, degraded = trader.get_upcoming_earnings(
                ["SLOW1", "SLOW2", "SLOW3", "FAST1"], days_ahead=2
            )
            elapsed = _time.monotonic() - start

        self.assertTrue(degraded)
        self.assertEqual(blackout, set())
        # 不应无限期挂起：即便有 3 个标的各 sleep 5s，线程池并发执行 + 超时保护应远快于串行 15s
        self.assertLess(elapsed, 4.0)

    def test_get_upcoming_earnings_not_degraded_when_all_succeed(self):
        trader = load_trader_module()

        class FastTicker:
            def __init__(self, sym):
                self.sym = sym

            @property
            def calendar(self):
                return {"Earnings Date": []}

        with patch.object(trader.yf, "Ticker", side_effect=FastTicker):
            blackout, degraded = trader.get_upcoming_earnings(["A", "B", "C"], days_ahead=2)

        self.assertFalse(degraded)
        self.assertEqual(blackout, set())

    def test_plan_email_flags_earnings_degraded_status(self):
        trader = load_trader_module()
        plan = {
            "plan_date": "2026-07-02",
            "signal_date": "2026-07-02",
            "target_syms": ["AAPL"],
            "target_val": 1000.0,
            "close_all": [],
            "trim": [],
            "buy": [],
            "est_sell_value": 0.0,
            "est_buy_total": 0.0,
            "est_available": 0.0,
            "earnings_degraded": True,
        }
        status = "plan_saved" if not plan.get("earnings_degraded") else "plan_saved_earnings_degraded"
        self.assertEqual(status, "plan_saved_earnings_degraded")
        subject = trader.build_email_subject(status, "run123", True)
        self.assertIn("财报避雷未完全生效", subject)

    def test_sell_phase_writes_retry_cycle_plan_for_partially_failed_submissions(self):
        """回归用例：sell 阶段部分标的提交失败（非幂等错误）时不能被 all_failed 掩盖——
        all_failed 只在全部标的都失败时才为真，若有其它标的成功，失败标的必须写回一份
        新的 cycle_plan 供下一轮重试，否则会随成功标的推进而被静默清空、永久丢失。"""
        trader = load_trader_module()

        class FakeDataClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_stock_latest_quote(self, req):
                return {}

        class Client:
            def __init__(self):
                self.submitted = []

            def submit_order(self, req):
                if req.symbol == "BAD":
                    raise RuntimeError("insufficient shares to sell")
                self.submitted.append(req)
                return type("Order", (), {"id": "sell-1", "status": "accepted"})()

        plan = {
            "plan_date": "2026-07-01",
            "signal_date": "2026-07-01",
            "close_all": [{"symbol": "GOOD", "qty": 3}, {"symbol": "BAD", "qty": 2}],
            "trim": [],
            "buy": [{"symbol": "NVDA", "qty": 1, "price": 100.0, "is_new": True}],
        }
        state = {trader.CYCLE_PLAN_KEY: plan}
        client = Client()
        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient):
            summary = trader.execute_sell_phase(client, state, "run-sell-partial", dry_run=False)

        self.assertFalse(summary["all_failed"])
        self.assertEqual([f["symbol"] for f in summary["submit_failed"]], ["BAD"])

        pending = state[trader.PENDING_SELL_KEY]
        self.assertEqual([o["symbol"] for o in pending["orders"]], ["GOOD"])

        retry_plan = state[trader.CYCLE_PLAN_KEY]
        self.assertEqual([c["symbol"] for c in retry_plan["close_all"]], ["BAD"])
        self.assertEqual(retry_plan["close_all"][0]["qty"], 2)
        self.assertEqual(retry_plan["trim"], [])
        self.assertEqual(retry_plan["signal_date"], "2026-07-01")
        self.assertNotEqual(retry_plan["plan_date"], "2026-07-01")

    def test_should_escalate_to_error_covers_no_email_statuses(self):
        """回归用例：非交易日等免打扰状态若在其处理逻辑内部再抛异常（如 _save_state 写盘
        失败），必须仍能升级为 error 并触发邮件——否则一次真实故障会被 NO_EMAIL_STATUSES
        的静默逻辑连带吞掉，运维完全无感知。"""
        trader = load_trader_module()

        for status in trader.NO_EMAIL_STATUSES:
            self.assertTrue(trader.should_escalate_to_error(status))
        self.assertTrue(trader.should_escalate_to_error("started"))
        self.assertTrue(trader.should_escalate_to_error("ok"))
        self.assertFalse(trader.should_escalate_to_error("sell_submitted"))


if __name__ == "__main__":
    unittest.main()
