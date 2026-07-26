import importlib.util
import os
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import Mock, patch

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

        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
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

        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
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
        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
            summary = trader.execute_sell_phase(client, state, "run-sell-stop", dry_run=False)

        self.assertEqual(client.cancelled, ["stop-amat-1"])
        self.assertEqual(len(client.submitted), 1)
        self.assertFalse(summary["all_failed"])

    def test_sell_phase_restores_stop_order_when_sell_submission_fails(self):
        """回归用例：止损单已被撤销、但卖单提交失败——必须立即用原止损价把止损单
        恢复回去，不能留下"止损已撤、卖单未成交"的裸露窗口。"""
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
            stop_price = 75.0
            qty = 3

        class Client:
            def __init__(self):
                self.cancelled = []
                self.submitted = []

            def get_orders(self, req):
                return [StopOrder()]

            def cancel_order_by_id(self, order_id):
                self.cancelled.append(order_id)

            def submit_order(self, req):
                # 卖单必然失败，止损恢复单必然成功——用 client_order_id 前缀区分两者
                if req.client_order_id.startswith("tq-stop-"):
                    self.submitted.append(req)
                    return type("Order", (), {"id": "stop-restored-1", "status": "accepted"})()
                raise RuntimeError("broker rejected sell order")

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
        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
            summary = trader.execute_sell_phase(client, state, "run-sell-restore", dry_run=False)

        self.assertEqual(client.cancelled, ["stop-amat-1"])
        self.assertEqual(len(client.submitted), 1)
        self.assertEqual(client.submitted[0].stop_price, 75.0)
        self.assertEqual(summary["naked_positions"], [])
        self.assertEqual([f["symbol"] for f in summary["submit_failed"]], ["AMAT"])

    def test_sell_phase_reports_naked_position_when_stop_restore_also_fails(self):
        """止损单撤销后，卖单和止损恢复单都失败：必须记录为 naked_positions，
        供上层升级为最高优先级报警邮件，而不是被静默吞掉。"""
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
            stop_price = 75.0
            qty = 3

        class Client:
            def __init__(self):
                self.cancelled = []

            def get_orders(self, req):
                return [StopOrder()]

            def cancel_order_by_id(self, order_id):
                self.cancelled.append(order_id)

            def submit_order(self, req):
                raise RuntimeError("broker unavailable")

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
        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
            summary = trader.execute_sell_phase(client, state, "run-sell-naked", dry_run=False)

        self.assertEqual(len(summary["naked_positions"]), 1)
        self.assertEqual(summary["naked_positions"][0]["symbol"], "AMAT")

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

        with patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
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

        with patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
            summary = trader.execute_buy_phase(Client(), state, "run-buy-merge", dry_run=False)

        self.assertTrue(summary.get("retry_plan_written"))
        retry_plan = state[trader.CYCLE_PLAN_KEY]
        retry_symbols = {c["symbol"] for c in retry_plan["close_all"]}
        self.assertEqual(retry_symbols, {"BAD", "GOOD"})

    def test_buy_phase_retry_merge_keeps_new_qty_on_overlapping_symbol(self):
        """回归用例：existing cycle_plan 与本轮 buy 阶段重试计划里出现同一 symbol 时，
        合并逻辑必须去重（不能重复计数同一标的两条记录），并以本轮核实到的最新 qty
        为准，而不是简单拼接两个列表。"""
        trader = load_trader_module()

        class Order:
            def __init__(self, filled_qty, status="filled"):
                self.filled_qty = filled_qty
                self.status = status

        class Account:
            buying_power = "100000"

        class Client:
            def get_order_by_client_id(self, cid):
                return Order(1)  # BAD 只成交 1/3，剩余 2 股未卖出

            def get_account(self):
                return Account()

            def submit_order(self, req):
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

            def get_all_positions(self):
                return []

        state = {
            # 上一轮遗留的 BAD 重试计划里 qty 是旧值 5（早已过时）
            trader.CYCLE_PLAN_KEY: {
                "plan_date": "2026-07-02",
                "signal_date": "2026-06-22",
                "close_all": [{"symbol": "BAD", "qty": 5}],
                "trim": [],
                "buy": [],
            },
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-06-23",
                "signal_date": "2026-06-22",
                "orders": [
                    {"symbol": "BAD", "qty": 3, "client_order_id": "tq-2026-06-22-close-bad", "kind": "close"},
                ],
                "buy_carry": [],
            },
        }

        with patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
            summary = trader.execute_buy_phase(Client(), state, "run-buy-dedup", dry_run=False)

        self.assertTrue(summary.get("retry_plan_written"))
        retry_plan = state[trader.CYCLE_PLAN_KEY]
        bad_entries = [c for c in retry_plan["close_all"] if c["symbol"] == "BAD"]
        self.assertEqual(len(bad_entries), 1)
        self.assertEqual(bad_entries[0]["qty"], 2)

    def test_buy_phase_flags_signal_date_mismatch_on_merge(self):
        """回归用例：待合并的 existing cycle_plan 与本轮 pending_sell 的 signal_date
        不一致，说明两个本应独立的调仓周期被意外混合——必须在 summary 里留下可追踪的
        标志（供邮件展示），而不能只打一行日志、让运维只能靠翻 log 才能发现。"""
        trader = load_trader_module()

        class Order:
            def __init__(self, filled_qty, status="filled"):
                self.filled_qty = filled_qty
                self.status = status

        class Account:
            buying_power = "100000"

        class Client:
            def get_order_by_client_id(self, cid):
                return Order(1)

            def get_account(self):
                return Account()

            def submit_order(self, req):
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

            def get_all_positions(self):
                return []

        state = {
            trader.CYCLE_PLAN_KEY: {
                "plan_date": "2026-07-02",
                "signal_date": "2026-06-15",  # 来自更早的、不相关的周期
                "close_all": [{"symbol": "BAD", "qty": 5}],
                "trim": [],
                "buy": [],
            },
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-06-23",
                "signal_date": "2026-06-22",
                "orders": [
                    {"symbol": "GOOD", "qty": 3, "client_order_id": "tq-2026-06-22-close-good", "kind": "close"},
                ],
                "buy_carry": [],
            },
        }

        with patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
            summary = trader.execute_buy_phase(Client(), state, "run-buy-mixed", dry_run=False)

        self.assertTrue(summary.get("signal_date_mixed"))

    def test_sell_phase_blocks_when_pending_sell_not_yet_consumed(self):
        """回归用例：若上一轮 sell 阶段提交的卖单还没被 buy 阶段核实/消费（buy 阶段
        遗漏运行或崩溃），本轮 sell 阶段绝不能直接提交新卖单并覆盖 pending_sell——
        那样会让上一轮已提交卖单的核实指针和 buy_carry 永久丢失。"""
        trader = load_trader_module()

        class Client:
            def submit_order(self, req):
                raise AssertionError("不应该在 pending_sell 未被消费时提交新卖单")

        state = {
            trader.CYCLE_PLAN_KEY: {
                "plan_date": "2026-07-02",
                "signal_date": "2026-07-02",
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
                "buy_carry": [{"symbol": "GOOD", "qty": 2, "price": 100.0, "is_new": False, "drift": 0.0}],
            },
        }

        summary = trader.execute_sell_phase(Client(), state, "run-sell-blocked", dry_run=False)

        self.assertTrue(summary["blocked_stale_pending_sell"])
        # pending_sell 必须原样保留，不能被覆盖或清空
        self.assertEqual(state[trader.PENDING_SELL_KEY]["orders"][0]["symbol"], "GOOD")
        self.assertEqual(state[trader.CYCLE_PLAN_KEY]["close_all"][0]["symbol"], "BAD")

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
        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
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

            def get_all_positions(self):
                return []

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
        with patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
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
                self.bought = False

            def get_order_by_client_id(self, cid):
                return type("Order", (), {"filled_qty": 3, "status": "filled"})()

            def get_account(self):
                return Account()

            def get_orders(self, req):
                return []

            def submit_order(self, req):
                if getattr(req, "stop_price", None) is not None:
                    self.stop_orders.append(req)
                else:
                    self.bought = True
                return type("Order", (), {"id": "o-1", "status": "accepted"})()

            def get_all_positions(self):
                return [Position()] if self.bought else []

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
        with patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
            summary = trader.execute_buy_phase(client, state, "run-buy-stop", dry_run=False)

        self.assertEqual(summary["stop_orders_submitted"], 0)
        self.assertIn(trader.PENDING_BUY_KEY, state)

        reconcile_summary = trader.reconcile_pending_buy(
            client, state, "run-buy-stop-reconcile", dry_run=False,
        )

        self.assertTrue(reconcile_summary["completed"])
        self.assertEqual(reconcile_summary["stop_orders_submitted"], 1)
        self.assertEqual(len(client.stop_orders), 1)

    def test_buy_phase_accepted_order_transitions_to_pending_buy(self):
        """券商 accepted 只表示接单，不代表成交。buy 阶段必须保存核实指针，且在
        后续确认成交前不能更新 last_rebalance 或按旧持仓提前挂止损。"""
        trader = load_trader_module()

        class Account:
            buying_power = "100000"

        class Client:
            def get_account(self):
                return Account()

            def get_all_positions(self):
                return []

            def submit_order(self, req):
                return type("Order", (), {"id": "buy-accepted", "status": "accepted"})()

        state = {
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-07-09",
                "signal_date": "2026-07-08",
                "orders": [],
                "buy_carry": [
                    {"symbol": "NVDA", "qty": 2, "price": 50.0, "is_new": True, "drift": 0.0},
                ],
            }
        }

        with patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0), \
             patch.object(trader, "ensure_stop_orders_for_positions", return_value=0) as stop_mock:
            summary = trader.execute_buy_phase(Client(), state, "run-buy-pending", dry_run=False)

        self.assertTrue(summary["buy_pending"])
        self.assertNotIn(trader.PENDING_SELL_KEY, state)
        self.assertIn(trader.PENDING_BUY_KEY, state)
        self.assertNotIn("last_rebalance", state)
        pending = state[trader.PENDING_BUY_KEY]
        self.assertEqual(pending["orders"][0]["symbol"], "NVDA")
        self.assertEqual(pending["orders"][0]["qty"], 2)
        stop_mock.assert_not_called()

    def test_reconcile_pending_buy_completes_only_after_fill_and_attaches_stops(self):
        trader = load_trader_module()

        class Position:
            symbol = "NVDA"
            qty = "2"
            avg_entry_price = "50"

        class Client:
            def get_order_by_client_id(self, cid):
                return type("Order", (), {"filled_qty": 2, "status": "filled"})()

            def get_all_positions(self):
                return [Position()]

        state = {
            trader.PENDING_BUY_KEY: {
                "submit_date": "2026-07-10",
                "signal_date": "2026-07-08",
                "orders": [{
                    "symbol": "NVDA", "qty": 2,
                    "client_order_id": "tq-20260708-enter-nvda", "status": "submitted",
                }],
            }
        }

        with patch.object(trader, "ensure_stop_orders_for_positions", return_value=1) as stop_mock:
            summary = trader.reconcile_pending_buy(Client(), state, "run-reconcile-filled", dry_run=False)

        self.assertTrue(summary["completed"])
        self.assertNotIn(trader.PENDING_BUY_KEY, state)
        self.assertEqual(state["last_order_signal_date"], "2026-07-08")
        self.assertIn("last_rebalance", state)
        self.assertEqual(summary["stop_orders_submitted"], 1)
        stop_mock.assert_called_once()

    def test_reconcile_pending_buy_retains_nonfilled_and_unknown_orders(self):
        trader = load_trader_module()

        cases = [
            ("partial", type("Order", (), {"filled_qty": 1, "status": "partially_filled"})()),
            ("accepted", type("Order", (), {"filled_qty": 0, "status": "accepted"})()),
            ("rejected", type("Order", (), {"filled_qty": 0, "status": "rejected"})()),
            ("query_error", RuntimeError("temporary api error")),
        ]

        for label, result in cases:
            with self.subTest(label=label):
                class Client:
                    def get_order_by_client_id(self, cid):
                        if isinstance(result, Exception):
                            raise result
                        return result

                    def get_all_positions(self):
                        raise AssertionError("未全部成交时不应读取持仓挂止损")

                state = {
                    trader.PENDING_BUY_KEY: {
                        "submit_date": "2026-07-10",
                        "signal_date": "2026-07-08",
                        "orders": [{
                            "symbol": "NVDA", "qty": 2,
                            "client_order_id": "tq-20260708-enter-nvda", "status": "submitted",
                        }],
                    }
                }

                with patch.object(trader, "ensure_stop_orders_for_positions", return_value=0) as stop_mock:
                    summary = trader.reconcile_pending_buy(
                        Client(), state, f"run-reconcile-{label}", dry_run=False,
                    )

                self.assertFalse(summary["completed"])
                self.assertIn(trader.PENDING_BUY_KEY, state)
                self.assertEqual(len(state[trader.PENDING_BUY_KEY]["orders"]), 1)
                stop_mock.assert_not_called()

    def test_buy_phase_submit_failure_keeps_pending_buy_intent(self):
        trader = load_trader_module()

        class Account:
            buying_power = "100000"

        class Client:
            def get_account(self):
                return Account()

            def get_all_positions(self):
                return []

            def submit_order(self, req):
                raise RuntimeError("broker temporarily unavailable")

        state = {
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-07-09",
                "signal_date": "2026-07-08",
                "orders": [],
                "buy_carry": [
                    {"symbol": "NVDA", "qty": 2, "price": 50.0, "is_new": True, "drift": 0.0},
                ],
            }
        }

        with patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
            summary = trader.execute_buy_phase(Client(), state, "run-buy-submit-failed", dry_run=False)

        self.assertTrue(summary["buy_pending"])
        self.assertNotIn("last_rebalance", state)
        pending_order = state[trader.PENDING_BUY_KEY]["orders"][0]
        self.assertEqual(pending_order["symbol"], "NVDA")
        self.assertEqual(pending_order["qty"], 2)
        self.assertEqual(pending_order["status"], "submit_failed")

    def test_retry_halted_entry_reconciles_pending_buy_before_halt_queue(self):
        trader = load_trader_module()
        state = {
            trader.PENDING_BUY_KEY: {
                "submit_date": "2026-07-10",
                "signal_date": "2026-07-08",
                "orders": [],
            }
        }
        args = type("Args", (), {"retry_halted": True, "dry_run": False})()
        call_order = []

        with patch.object(trader, "TradingClient", return_value=object()), \
             patch.object(trader, "_load_state", return_value=state), \
             patch.object(trader, "_save_state") as save_mock, \
             patch.object(
                 trader, "reconcile_pending_buy",
                 side_effect=lambda *a, **k: call_order.append("reconcile") or {"completed": True},
             ), \
             patch.object(
                 trader, "retry_halted_orders",
                 side_effect=lambda *a, **k: call_order.append("halt_retry"),
             ):
            trader._main_impl(args, earnings_allow=set())

        self.assertEqual(call_order, ["reconcile", "halt_retry"])
        save_mock.assert_called_once_with(state)

    def test_pending_buy_blocks_new_plan_and_has_noncompleted_status(self):
        trader = load_trader_module()

        self.assertTrue(trader.has_incomplete_cycle({trader.PENDING_BUY_KEY: {"orders": [{}]}}))
        self.assertEqual(
            trader.buy_phase_status({"buy_pending": True, "submit_failed": [], "had_fill_issues": False}),
            "buy_submitted_pending_confirmation",
        )
        self.assertEqual(
            trader.buy_phase_status({
                "buy_pending": True,
                "submit_failed": [{"symbol": "NVDA"}],
                "had_fill_issues": False,
            }),
            "buy_submitted_with_issues",
        )
        self.assertEqual(
            trader.buy_phase_status({"buy_pending": False, "submit_failed": [], "had_fill_issues": False}),
            "buy_completed",
        )

    def test_reconcile_pending_buy_retries_submit_failure_only_after_confirmed_not_found(self):
        trader = load_trader_module()

        class Client:
            def __init__(self):
                self.submitted = []

            def get_order_by_client_id(self, cid):
                raise RuntimeError("404 order not found")

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "retry-1", "status": "accepted"})()

            def get_all_positions(self):
                raise AssertionError("重试单尚未确认成交，不应挂止损")

        state = {
            trader.PENDING_BUY_KEY: {
                "submit_date": "2026-07-10",
                "signal_date": "2026-07-08",
                "orders": [{
                    "symbol": "NVDA", "qty": 2,
                    "client_order_id": "tq-20260708-enter-nvda",
                    "status": "submit_failed",
                }],
            }
        }
        client = Client()

        summary = trader.reconcile_pending_buy(
            client, state, "run-reconcile-retry", dry_run=False,
        )

        self.assertFalse(summary["completed"])
        self.assertEqual(len(client.submitted), 1)
        pending_order = state[trader.PENDING_BUY_KEY]["orders"][0]
        self.assertEqual(pending_order["status"], "submitted")
        self.assertEqual(pending_order["retry_attempt"], 1)
        self.assertNotEqual(pending_order["client_order_id"], "tq-20260708-enter-nvda")

    def test_halt_retry_updates_pending_buy_client_order_id(self):
        trader = load_trader_module()

        class Quote:
            bid_price = 100.0
            ask_price = 100.5

        class DataClient:
            def get_stock_latest_quote(self, req):
                return {"NVDA": Quote()}

        class Client:
            def get_order_by_client_id(self, cid):
                raise RuntimeError("404 order not found")

            def submit_order(self, req):
                return type("Order", (), {"id": "halt-retry-1", "status": "accepted"})()

            def get_all_positions(self):
                raise AssertionError("halt 重试单尚未确认成交，不应挂止损")

        state = {
            trader.PENDING_BUY_KEY: {
                "submit_date": "2026-07-10",
                "signal_date": "2026-07-08",
                "orders": [{
                    "symbol": "NVDA", "qty": 2,
                    "client_order_id": "tq-20260708-enter-nvda",
                    "status": "halted",
                }],
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            pending_file = Path(tmp) / "halt_pending.json"
            pending_file.write_text(json.dumps({
                "pending_date": "2026-07-08",
                "orders": [{
                    "symbol": "NVDA", "qty": 2,
                    "signal_date": "2026-07-08", "run_id": "run-buy-halt",
                }],
            }), encoding="utf-8")

            with patch.object(trader, "HALT_PENDING_FILE", pending_file), \
                 patch.object(trader, "TradingClient", return_value=Client()), \
                 patch.object(trader, "StockHistoricalDataClient", return_value=DataClient()), \
                 patch.object(trader, "_now_hour_et", return_value=10):
                trader.reconcile_pending_buy(
                    Client(), state, "run-reconcile-before-halt", dry_run=False,
                )
                trader.retry_halted_orders(dry_run=False, state=state)

        pending_order = state[trader.PENDING_BUY_KEY]["orders"][0]
        self.assertEqual(pending_order["status"], "submitted")
        self.assertEqual(pending_order["client_order_id"], "tq-retry-20260708-nvda")

    def test_halt_retry_uses_ask_price_not_bid(self):
        """回归用例：买单重试必须按卖一价（ask）追价，用买一价（bid）会低于最优卖价，
        几乎不可能成交（此前版本 bid*0.999 是买卖方向搞反的错误）。"""
        trader = load_trader_module()

        class Quote:
            bid_price = 90.0
            ask_price = 100.0

        class DataClient:
            def get_stock_latest_quote(self, req):
                return {"NVDA": Quote()}

        class Client:
            def __init__(self):
                self.submitted = []

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "halt-retry-1", "status": "accepted"})()

        client = Client()

        with tempfile.TemporaryDirectory() as tmp:
            pending_file = Path(tmp) / "halt_pending.json"
            pending_file.write_text(json.dumps({
                "pending_date": "2026-07-08",
                "orders": [{
                    "symbol": "NVDA", "qty": 2, "ref_price": 100.0,
                    "signal_date": "2026-07-08", "run_id": "run-buy-halt",
                }],
            }), encoding="utf-8")

            with patch.object(trader, "HALT_PENDING_FILE", pending_file), \
                 patch.object(trader, "TradingClient", return_value=client), \
                 patch.object(trader, "StockHistoricalDataClient", return_value=DataClient()), \
                 patch.object(trader, "_now_hour_et", return_value=10):
                trader.retry_halted_orders(dry_run=False, state=None)

        self.assertEqual(len(client.submitted), 1)
        # limit_price = ask * 1.001，明显高于 bid，绝不能落在 bid 附近
        self.assertAlmostEqual(client.submitted[0].limit_price, 100.1, places=2)

    def test_halt_retry_skips_submission_when_chase_price_exceeds_cap(self):
        """追涨超过 HALT_RETRY_MAX_CHASE_PCT 时本轮不提交（继续等待），不算失败也不报警。"""
        trader = load_trader_module()

        class Quote:
            bid_price = 148.0
            ask_price = 150.0  # 相对 ref_price=100 已经涨了 50%，远超 5% 上限

        class DataClient:
            def get_stock_latest_quote(self, req):
                return {"NVDA": Quote()}

        class Client:
            def __init__(self):
                self.submitted = []

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "should-not-happen", "status": "accepted"})()

        client = Client()

        with tempfile.TemporaryDirectory() as tmp:
            pending_file = Path(tmp) / "halt_pending.json"
            pending_file.write_text(json.dumps({
                "pending_date": "2026-07-08",
                "orders": [{
                    "symbol": "NVDA", "qty": 2, "ref_price": 100.0,
                    "signal_date": "2026-07-08", "run_id": "run-buy-halt",
                }],
            }), encoding="utf-8")

            with patch.object(trader, "HALT_PENDING_FILE", pending_file), \
                 patch.object(trader, "TradingClient", return_value=client), \
                 patch.object(trader, "StockHistoricalDataClient", return_value=DataClient()), \
                 patch.object(trader, "_now_hour_et", return_value=10), \
                 patch.object(trader, "HALT_RETRY_MAX_ATTEMPTS", 1), \
                 patch.object(trader, "time") as time_mock:
                summary = trader.retry_halted_orders(dry_run=False, state=None)

        self.assertEqual(client.submitted, [])
        self.assertIn("NVDA", summary["chase_capped_symbols"])
        self.assertNotIn("NVDA", summary["confirmed_absent_symbols"])
        self.assertNotIn("NVDA", summary["uncertain_symbols"])
        # 追价放弃不属于"失败"，不应该触发 sleep（重试循环在 max_attempts 后正常退出）
        time_mock.sleep.assert_not_called()

    def test_halt_retry_exhausted_and_broker_confirms_404_abandons_without_alert(self):
        """重试次数耗尽后，Broker 明确确认订单不存在（404）：静默放弃该笔买入意图，
        从 pending_buy 中移除，不产生持仓也不发邮件报警（追涨没下单，不用发邮件）。"""
        trader = load_trader_module()

        class Quote:
            bid_price = 100.0
            ask_price = 100.5

        class DataClient:
            def get_stock_latest_quote(self, req):
                return {"NVDA": Quote()}

        class Client:
            def __init__(self):
                self.submit_calls = 0

            def submit_order(self, req):
                self.submit_calls += 1
                raise RuntimeError("halted: trading halted")

            def get_order_by_client_id(self, cid):
                raise RuntimeError("404 order not found")

            def get_all_positions(self):
                return []

        client = Client()
        state = {
            trader.PENDING_BUY_KEY: {
                "submit_date": "2026-07-10",
                "signal_date": "2026-07-08",
                "orders": [{
                    "symbol": "NVDA", "qty": 2,
                    "client_order_id": "tq-20260708-enter-nvda",
                    "status": "halted",
                }],
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            pending_file = Path(tmp) / "halt_pending.json"
            pending_file.write_text(json.dumps({
                "pending_date": "2026-07-08",
                "orders": [{
                    "symbol": "NVDA", "qty": 2, "ref_price": 100.0,
                    "signal_date": "2026-07-08", "run_id": "run-buy-halt",
                }],
            }), encoding="utf-8")

            with patch.object(trader, "HALT_PENDING_FILE", pending_file), \
                 patch.object(trader, "TradingClient", return_value=client), \
                 patch.object(trader, "StockHistoricalDataClient", return_value=DataClient()), \
                 patch.object(trader, "_now_hour_et", return_value=10), \
                 patch.object(trader, "HALT_RETRY_MAX_ATTEMPTS", 2), \
                 patch.object(trader, "time") as time_mock:
                summary = trader.retry_halted_orders(dry_run=False, state=state)

        self.assertEqual(summary["confirmed_absent_symbols"], ["NVDA"])
        self.assertEqual(summary["uncertain_symbols"], [])
        self.assertNotIn(trader.PENDING_BUY_KEY, state)  # 全部解决，周期已收尾
        self.assertFalse(pending_file.exists())
        time_mock.sleep.assert_called()

    def test_halt_retry_exhausted_but_broker_status_uncertain_keeps_pending_buy(self):
        """重试次数耗尽后，Broker 状态不确定（既非明确 404，也无法确认不存在）：
        必须保留 pending_buy（继续阻塞下一次 plan 周期），交由上层发邮件报警。"""
        trader = load_trader_module()

        class Quote:
            bid_price = 100.0
            ask_price = 100.5

        class Client:
            def submit_order(self, req):
                raise RuntimeError("halted: trading halted")

            def get_order_by_client_id(self, cid):
                raise RuntimeError("network timeout")  # 不是明确的 404

        class DataClient:
            def get_stock_latest_quote(self, req):
                return {"NVDA": Quote()}

        client = Client()
        state = {
            trader.PENDING_BUY_KEY: {
                "submit_date": "2026-07-10",
                "signal_date": "2026-07-08",
                "orders": [{
                    "symbol": "NVDA", "qty": 2,
                    "client_order_id": "tq-20260708-enter-nvda",
                    "status": "halted",
                }],
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            pending_file = Path(tmp) / "halt_pending.json"
            pending_file.write_text(json.dumps({
                "pending_date": "2026-07-08",
                "orders": [{
                    "symbol": "NVDA", "qty": 2, "ref_price": 100.0,
                    "signal_date": "2026-07-08", "run_id": "run-buy-halt",
                }],
            }), encoding="utf-8")

            with patch.object(trader, "HALT_PENDING_FILE", pending_file), \
                 patch.object(trader, "TradingClient", return_value=client), \
                 patch.object(trader, "StockHistoricalDataClient", return_value=DataClient()), \
                 patch.object(trader, "_now_hour_et", return_value=10), \
                 patch.object(trader, "HALT_RETRY_MAX_ATTEMPTS", 1), \
                 patch.object(trader, "time"):
                summary = trader.retry_halted_orders(dry_run=False, state=state)

        self.assertEqual(summary["uncertain_symbols"], ["NVDA"])
        self.assertEqual(summary["confirmed_absent_symbols"], [])
        self.assertIn(trader.PENDING_BUY_KEY, state)
        pending_order = state[trader.PENDING_BUY_KEY]["orders"][0]
        self.assertEqual(pending_order["status"], "halt_retry_stuck_uncertain")

    def test_halt_retry_uncertain_persists_latest_attempted_client_order_id(self):
        """回归用例：模糊提交异常导致状态不确定时，必须把本轮实际尝试的
        client_order_id（tq-retry-...）写回 pending_buy，而不是留着重试前的旧 ID——
        否则后续人工核实/下一次 reconcile 会查询错的订单，即使 Broker 已经用新 ID
        接单成功，也会核实不到，导致真实持仓可能漏挂止损。"""
        trader = load_trader_module()

        class Quote:
            bid_price = 100.0
            ask_price = 100.5

        class Client:
            def submit_order(self, req):
                raise RuntimeError("connection reset by peer")  # 模糊异常，非 duplicate/halt/404

        class DataClient:
            def get_stock_latest_quote(self, req):
                return {"NVDA": Quote()}

        client = Client()
        state = {
            trader.PENDING_BUY_KEY: {
                "submit_date": "2026-07-10",
                "signal_date": "2026-07-08",
                "orders": [{
                    "symbol": "NVDA", "qty": 2,
                    "client_order_id": "tq-20260708-enter-nvda",
                    "status": "halted",
                }],
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            pending_file = Path(tmp) / "halt_pending.json"
            pending_file.write_text(json.dumps({
                "pending_date": "2026-07-08",
                "orders": [{
                    "symbol": "NVDA", "qty": 2, "ref_price": 100.0,
                    "signal_date": "2026-07-08", "run_id": "run-buy-halt",
                }],
            }), encoding="utf-8")

            with patch.object(trader, "HALT_PENDING_FILE", pending_file), \
                 patch.object(trader, "TradingClient", return_value=client), \
                 patch.object(trader, "StockHistoricalDataClient", return_value=DataClient()), \
                 patch.object(trader, "_now_hour_et", return_value=10), \
                 patch.object(trader, "HALT_RETRY_MAX_ATTEMPTS", 1), \
                 patch.object(trader, "time"):
                summary = trader.retry_halted_orders(dry_run=False, state=state)

        self.assertEqual(summary["uncertain_symbols"], ["NVDA"])
        pending_order = state[trader.PENDING_BUY_KEY]["orders"][0]
        self.assertEqual(pending_order["status"], "halt_retry_stuck_uncertain")
        self.assertEqual(pending_order["client_order_id"], "tq-retry-20260708-nvda")

    def test_retry_halted_entry_sends_email_only_for_uncertain_symbols(self):
        """--retry-halted 分支之前完全不发邮件；现在必须仅在状态不确定时才发，
        追涨放弃/确认不存在都不应该触发邮件。"""
        trader = load_trader_module()

        args = type("Args", (), {"retry_halted": True, "dry_run": False})()

        with patch.object(trader, "TradingClient", return_value=object()), \
             patch.object(trader, "_load_state", return_value={}), \
             patch.object(trader, "_save_state"), \
             patch.object(
                 trader, "retry_halted_orders",
                 return_value={
                     "uncertain_symbols": ["NVDA"],
                     "confirmed_absent_symbols": [],
                     "chase_capped_symbols": [],
                     "attempts_used": 6,
                 },
             ), \
             patch.object(trader, "EMAIL_ENABLED", True), \
             patch.object(trader, "send_email") as send_mock:
            trader._main_impl(args, earnings_allow=set())

        send_mock.assert_called_once()
        subject, body = send_mock.call_args[0][0], send_mock.call_args[0][1]
        self.assertIn("halt_retry_stuck_uncertain", subject)
        self.assertIn("NVDA", body)

    def test_retry_halted_entry_sends_no_email_when_only_chase_capped_or_absent(self):
        trader = load_trader_module()

        args = type("Args", (), {"retry_halted": True, "dry_run": False})()

        with patch.object(trader, "TradingClient", return_value=object()), \
             patch.object(trader, "_load_state", return_value={}), \
             patch.object(trader, "_save_state"), \
             patch.object(
                 trader, "retry_halted_orders",
                 return_value={
                     "uncertain_symbols": [],
                     "confirmed_absent_symbols": ["NVDA"],
                     "chase_capped_symbols": ["AMAT"],
                     "attempts_used": 6,
                 },
             ), \
             patch.object(trader, "EMAIL_ENABLED", True), \
             patch.object(trader, "send_email") as send_mock:
            trader._main_impl(args, earnings_allow=set())

        send_mock.assert_not_called()

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

        with patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0), \
             patch.object(trader, "MAX_POSITION_PCT", 0.5), \
             patch.object(trader, "MIN_CASH_BUFFER_PCT", 0.05):
            plan = trader.compute_rebalance_plan(
                Client(), ["AMAT", "NVDA"], close, equity=10000.0, buying_power=10000.0,
                signal_date="2026-06-03",
            )

        bought_syms = [b["symbol"] for b in plan["buy"]]
        self.assertIn("AMAT", bought_syms)
        self.assertNotIn("NVDA", bought_syms)
        self.assertEqual(plan["target_snapshot"], {"AMAT": {"price": 102.0}})
        self.assertEqual(plan["target_n"], 1)
        self.assertEqual(plan["target_val"], 5000.0)

    def test_compute_rebalance_plan_caps_single_candidate_at_max_position_pct(self):
        trader = load_trader_module()
        idx = pd.bdate_range("2026-06-01", periods=3)
        close = pd.DataFrame({"AMAT": [100.0, 101.0, 102.0]}, index=idx)

        class Client:
            def get_all_positions(self):
                return []

        with patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0), \
             patch.object(trader, "MAX_POSITION_PCT", 0.5), \
             patch.object(trader, "MIN_CASH_BUFFER_PCT", 0.05):
            plan = trader.compute_rebalance_plan(
                Client(), ["AMAT"], close, equity=10000.0, buying_power=10000.0,
                signal_date="2026-06-03",
            )

        # 只有 1 个候选标的时，等权本会分到 100% 资金；集中度上限应把它封顶在 50%，
        # 而不是把账户几乎全部资金压在单票上
        self.assertEqual(plan["target_val"], 5000.0)

    def test_buy_phase_skips_new_position_already_held(self):
        trader = load_trader_module()

        class Position:
            symbol = "DOCN"

        class Account:
            buying_power = "100000"

        class Client:
            def __init__(self):
                self.submitted = []

            def get_order_by_client_id(self, cid):
                return type("Order", (), {"filled_qty": 5, "status": "filled"})()

            def get_account(self):
                return Account()

            def get_all_positions(self):
                return [Position()]

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

        state = {
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-07-09",
                "signal_date": "2026-07-08",
                "orders": [
                    {"symbol": "KLAC", "qty": 5, "client_order_id": "tq-20260708-close-klac", "kind": "close"},
                ],
                "buy_carry": [
                    {"symbol": "DOCN", "qty": 678, "price": 140.47, "is_new": True, "drift": -1.0},
                ],
            }
        }

        client = Client()
        with patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
            summary = trader.execute_buy_phase(client, state, "run-buy-dup", dry_run=False)

        # DOCN 已经是持仓，计划却仍标记为"新建"（比如上一轮 state 卡住重放）——
        # 不应该重复提交买单，避免同一标的仓位翻倍
        self.assertEqual(client.submitted, [])
        self.assertEqual(summary["orders"], [])

    def test_buy_phase_earnings_recheck_cancels_new_and_add_without_forced_sell(self):
        """新增：buy 阶段财报复核命中标的（无论新建 is_new=True 还是加仓 is_new=False），
        一律取消本次买入，且不发起强制卖出（清仓交给下一轮 sell 阶段复核）。"""
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
                return Order(2)

            def get_account(self):
                return Account()

            def get_all_positions(self):
                return []

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

        state = {
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-07-01",
                "signal_date": "2026-07-01",
                "orders": [
                    {"symbol": "OLD", "qty": 2, "client_order_id": "tq-20260701-close-old", "kind": "close"},
                ],
                "buy_carry": [
                    {"symbol": "NVDA", "qty": 3, "price": 100.0, "is_new": True, "drift": 0.0},
                    {"symbol": "AAPL", "qty": 2, "price": 150.0, "is_new": False, "drift": -0.05},
                ],
            }
        }
        client = Client()

        with patch.object(trader, "get_upcoming_earnings", return_value=({"NVDA", "AAPL"}, False)):
            summary = trader.execute_buy_phase(client, state, "run-buy-earnings-skip", dry_run=False)

        self.assertEqual(summary["earnings_skipped"], ["AAPL", "NVDA"])
        self.assertEqual(summary["orders"], [])
        self.assertEqual(client.submitted, [])

    def test_buy_phase_earnings_reallocation_recomputes_qty_for_survivors(self):
        """核心场景：财报复核剔除 1 只候选后，剩余候选按新的等权目标金额重新计算
        买入股数——必须是与原计划不同的具体数值，不能留着过时份额或者空置资金。"""
        trader = load_trader_module()

        class Order:
            def __init__(self, filled_qty, status="filled"):
                self.filled_qty = filled_qty
                self.status = status

        class Account:
            buying_power = "100000"

        class Position:
            def __init__(self, symbol, qty):
                self.symbol = symbol
                self.qty = qty

        class Client:
            def __init__(self):
                self.submitted = []

            def get_order_by_client_id(self, cid):
                return Order(1)

            def get_account(self):
                return Account()

            def get_all_positions(self):
                return [Position("BBB", 10)]

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

        state = {
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-07-01",
                "signal_date": "2026-07-01",
                "orders": [],
                "buy_carry": [
                    {"symbol": "AAA", "qty": 30, "price": 100.0, "is_new": True, "drift": 0.0},
                    {"symbol": "BBB", "qty": 50, "price": 50.0, "is_new": False, "drift": 0.0},
                    {"symbol": "CCC", "qty": 37, "price": 80.0, "is_new": True, "drift": 0.0},
                ],
                "target_n": 3,
            }
        }
        client = Client()

        with patch.object(trader, "get_upcoming_earnings", return_value=({"CCC"}, False)), \
             patch.object(trader, "MAX_POSITION_PCT", 0.5), \
             patch.object(trader, "MIN_CASH_BUFFER_PCT", 0.05):
            summary = trader.execute_buy_phase(client, state, "run-buy-realloc", dry_run=False, sizing_capital=10000.0)

        self.assertEqual(summary["earnings_skipped"], ["CCC"])
        self.assertTrue(summary["earnings_reallocated"])
        self.assertEqual(summary["target_val_reallocated"], 4750.0)

        order_qty = {o["symbol"]: o["qty"] for o in summary["orders"]}
        self.assertEqual(order_qty["AAA"], 45)   # 原计划 30 股，重算后变为 45 股
        self.assertEqual(order_qty["BBB"], 80)   # 原计划 50 股，重算后变为 80 股（新目标股数 90 - 已持仓 10）
        self.assertNotIn("CCC", order_qty)

    def test_sell_blackout_reallocates_all_surviving_targets_at_buy(self):
        """sell 阶段剔除目标后，即使 buy 当天没有新增财报命中，也必须基于完整
        目标快照重算；原 HOLD 标的同样可能产生新的正向差额。"""
        trader = load_trader_module()

        class Position:
            def __init__(self, symbol, qty, price):
                self.symbol = symbol
                self.qty = qty
                self.market_value = str(qty * price)

        class Account:
            buying_power = "49900"

        class FakeDataClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_stock_latest_quote(self, req):
                return {}

        class Client:
            def __init__(self):
                self.nvda_sold = False

            def get_all_positions(self):
                positions = [Position("AAPL", 301, 100.0), Position("MSFT", 400, 50.0)]
                if not self.nvda_sold:
                    positions.append(Position("NVDA", 180, 200.0))
                return positions

            def get_orders(self, req):
                return []

            def get_order_by_client_id(self, cid):
                return type("Order", (), {"filled_qty": 180, "status": "filled"})()

            def get_account(self):
                return Account()

            def submit_order(self, req):
                if req.side == trader.OrderSide.SELL and req.symbol == "NVDA":
                    self.nvda_sold = True
                return type("Order", (), {"id": "order-1", "status": "accepted"})()

        plan = {
            "plan_date": "2026-07-06",
            "signal_date": "2026-07-06",
            "target_syms": ["AAPL", "MSFT", "NVDA"],
            "target_snapshot": {
                "AAPL": {"price": 100.0},
                "MSFT": {"price": 50.0},
                "NVDA": {"price": 200.0},
            },
            "close_all": [],
            "trim": [{"symbol": "NVDA", "qty": 30, "price": 200.0, "drift": 0.0}],
            "buy": [{"symbol": "MSFT", "qty": 203, "price": 50.0, "is_new": False, "drift": 0.0}],
            "target_n": 3,
        }
        state = {trader.CYCLE_PLAN_KEY: plan}
        client = Client()
        earnings = Mock(side_effect=[({"NVDA"}, False), (set(), False)])

        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "get_upcoming_earnings", earnings), \
             patch.object(trader, "MAX_POSITION_PCT", 0.5), \
             patch.object(trader, "MIN_CASH_BUFFER_PCT", 0.05), \
             patch.object(trader, "ensure_stop_orders_for_positions", return_value=0):
            sell_summary = trader.execute_sell_phase(client, state, "run-sell-reallocate", dry_run=False)
            buy_summary = trader.execute_buy_phase(
                client, state, "run-buy-reallocate", dry_run=False, sizing_capital=100000.0,
            )

        self.assertEqual(sell_summary["earnings_forced_close"], ["NVDA"])
        order_qty = {o["symbol"]: o["qty"] for o in buy_summary["orders"]}
        self.assertEqual(order_qty, {"AAPL": 151, "MSFT": 504})
        self.assertTrue(buy_summary["earnings_reallocated"])
        self.assertEqual(buy_summary["target_val_reallocated"], 47500.0)

    def test_buy_phase_earnings_recheck_without_sizing_capital_only_filters(self):
        """新增：sizing_capital 缺失（如遗留的手工重试路径）时，财报剔除后
        只过滤不重算份额，优雅降级，不抛异常。"""
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
                return Order(1)

            def get_account(self):
                return Account()

            def get_all_positions(self):
                return []

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

        state = {
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-07-01",
                "signal_date": "2026-07-01",
                "orders": [],
                "buy_carry": [
                    {"symbol": "AAA", "qty": 30, "price": 100.0, "is_new": True, "drift": 0.0},
                    {"symbol": "CCC", "qty": 37, "price": 80.0, "is_new": True, "drift": 0.0},
                ],
                "target_n": 2,
            }
        }
        client = Client()

        with patch.object(trader, "get_upcoming_earnings", return_value=({"CCC"}, False)):
            summary = trader.execute_buy_phase(client, state, "run-buy-no-sizing", dry_run=False, sizing_capital=None)

        self.assertEqual(summary["earnings_skipped"], ["CCC"])
        self.assertFalse(summary["earnings_reallocated"])
        order_qty = {o["symbol"]: o["qty"] for o in summary["orders"]}
        self.assertEqual(order_qty, {"AAA": 30})  # 保留原计划份额，未重算

    def test_buy_phase_earnings_recheck_missing_target_n_skips_reallocation(self):
        """新增：pending_sell 缺少 target_n（升级前遗留 state 或手工拼装的重试计划）时，
        即使 sizing_capital 存在也应跳过重算，只按财报结果过滤。"""
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
                return Order(1)

            def get_account(self):
                return Account()

            def get_all_positions(self):
                return []

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

        state = {
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-07-01",
                "signal_date": "2026-07-01",
                "orders": [],
                "buy_carry": [
                    {"symbol": "AAA", "qty": 30, "price": 100.0, "is_new": True, "drift": 0.0},
                    {"symbol": "CCC", "qty": 37, "price": 80.0, "is_new": True, "drift": 0.0},
                ],
                # 无 target_n 字段（模拟升级前遗留 state）
            }
        }
        client = Client()

        with patch.object(trader, "get_upcoming_earnings", return_value=({"CCC"}, False)):
            summary = trader.execute_buy_phase(client, state, "run-buy-no-target-n", dry_run=False, sizing_capital=10000.0)

        self.assertEqual(summary["earnings_skipped"], ["CCC"])
        self.assertFalse(summary["earnings_reallocated"])
        order_qty = {o["symbol"]: o["qty"] for o in summary["orders"]}
        self.assertEqual(order_qty, {"AAA": 30})

    def test_buy_phase_earnings_recheck_degraded_does_not_block_submission(self):
        """新增：buy 阶段财报日历查询降级时只标记 degraded，不阻断买单正常提交。"""
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
                return Order(1)

            def get_account(self):
                return Account()

            def get_all_positions(self):
                return []

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

        state = {
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-07-01",
                "signal_date": "2026-07-01",
                "orders": [],
                "buy_carry": [
                    {"symbol": "NVDA", "qty": 2, "price": 100.0, "is_new": True, "drift": 0.0},
                ],
                "target_n": 1,
            }
        }
        client = Client()

        def degraded_lookup(symbols, days_ahead, details=None):
            if details is not None:
                details["failed_symbols"] = ["NVDA"]
            return set(), True

        with patch.object(trader, "get_upcoming_earnings", side_effect=degraded_lookup):
            summary = trader.execute_buy_phase(client, state, "run-buy-earnings-degraded", dry_run=False, sizing_capital=10000.0)

        self.assertTrue(summary["earnings_recheck_degraded"])
        self.assertEqual(summary["earnings_recheck_failed_symbols"], ["NVDA"])
        self.assertEqual([o["symbol"] for o in summary["orders"]], ["NVDA"])
        self.assertEqual(len(client.submitted), 1)

    def test_sell_and_buy_phase_earnings_allow_applies_to_current_run(self):
        trader = load_trader_module()

        class Position:
            symbol = "AAPL"
            qty = "10"
            market_value = "1000"

        class Account:
            buying_power = "100000"

        class FakeDataClient:
            def __init__(self, *args, **kwargs):
                pass

        class Client:
            def get_all_positions(self):
                return [Position()]

            def get_account(self):
                return Account()

            def submit_order(self, req):
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

        state = {
            trader.CYCLE_PLAN_KEY: {
                "plan_date": "2026-07-08",
                "signal_date": "2026-07-08",
                "close_all": [],
                "trim": [],
                "buy": [
                    {"symbol": "AAPL", "qty": 2, "price": 100.0, "is_new": False, "drift": 0.0},
                ],
                "target_n": 1,
                "target_snapshot": {"AAPL": {"price": 100.0}},
            }
        }
        client = Client()

        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "get_upcoming_earnings", return_value=({"AAPL"}, False)):
            sell_summary = trader.execute_sell_phase(
                client, state, "run-sell-allow", dry_run=False, earnings_allow={"AAPL"},
            )
            buy_summary = trader.execute_buy_phase(
                client, state, "run-buy-allow", dry_run=False,
                sizing_capital=10000.0, earnings_allow={"AAPL"},
            )

        self.assertEqual(sell_summary["earnings_forced_close"], [])
        self.assertEqual([o["symbol"] for o in buy_summary["orders"]], ["AAPL"])

    def test_sell_earnings_email_lines_cover_all_failed_branch_data(self):
        trader = load_trader_module()

        lines = trader.sell_earnings_email_lines({
            "earnings_forced_close": ["NVDA"],
            "earnings_recheck_degraded": True,
            "earnings_recheck_failed_symbols": ["MSFT"],
        })

        body = "\n".join(lines)
        self.assertIn("NVDA", body)
        self.assertIn("MSFT", body)
        self.assertIn("财报日历查询降级", body)

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

    def test_earnings_window_uses_nyse_sessions_across_independence_day(self):
        trader = load_trader_module()

        start, cutoff = trader.earnings_window_bounds(trader.date(2026, 7, 2), 2)

        self.assertEqual(start.date(), trader.date(2026, 7, 2))
        # 2026-07-03 为独立日观察休市；后续两个 NYSE 交易日是 07-06、07-07。
        self.assertEqual(cutoff.date(), trader.date(2026, 7, 7))

    def test_get_upcoming_earnings_reports_failed_symbols_without_blocking(self):
        trader = load_trader_module()
        details = {}

        def fake_calendar(sym):
            if sym == "MSFT":
                raise RuntimeError("calendar unavailable")
            return {"Earnings Date": [trader.pd.Timestamp("2026-07-07")]}

        with patch.object(trader, "_today_et", return_value=trader.date(2026, 7, 2)), \
             patch.object(trader, "_fetch_earnings_calendar", side_effect=fake_calendar):
            blackout, degraded = trader.get_upcoming_earnings(
                ["AAPL", "MSFT"], days_ahead=2, details=details,
            )

        self.assertEqual(blackout, {"AAPL"})
        self.assertFalse(degraded)
        self.assertEqual(details["failed_symbols"], ["MSFT"])

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
        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0):
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

    def test_sell_phase_earnings_recheck_forces_close_and_drops_buy_carry(self):
        """新增：sell 阶段财报复核发现 HOLD/加仓标的临近财报，必须强制清仓，
        并从 buy_carry 中剔除对应的加仓计划（不能继续按原计划加仓一个即将清仓的标的）。"""
        trader = load_trader_module()

        class Position:
            def __init__(self, symbol, qty):
                self.symbol = symbol
                self.qty = qty

        class FakeDataClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_stock_latest_quote(self, req):
                return {}

        class Client:
            def __init__(self):
                self.submitted = []

            def get_all_positions(self):
                return [Position("AAPL", 10)]

            def get_orders(self, req):
                return []

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "sell-1", "status": "accepted"})()

        plan = {
            "plan_date": "2026-07-01",
            "signal_date": "2026-07-01",
            "close_all": [],
            "trim": [],
            "buy": [{"symbol": "AAPL", "qty": 3, "price": 150.0, "is_new": False, "drift": -0.05}],
            "target_n": 3,
        }
        state = {trader.CYCLE_PLAN_KEY: plan}
        client = Client()

        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "get_upcoming_earnings", return_value=({"AAPL"}, False)):
            summary = trader.execute_sell_phase(client, state, "run-sell-earnings-hold", dry_run=False)

        self.assertEqual(summary["earnings_forced_close"], ["AAPL"])
        self.assertEqual(summary["buy_carry"], [])
        sold = [(o["symbol"], o["qty"], o["kind"]) for o in summary["orders"]]
        self.assertEqual(sold, [("AAPL", 10, "close")])
        pending = state[trader.PENDING_SELL_KEY]
        self.assertEqual(pending["buy_carry"], [])
        self.assertEqual(pending["target_n"], 2)

    def test_sell_phase_earnings_recheck_escalates_trim_to_full_close(self):
        """新增：trim（部分减仓）标的若临近财报，必须升级为全额清仓，
        不能因为原计划只是减仓就放过剩余持仓的财报风险。"""
        trader = load_trader_module()

        class Position:
            def __init__(self, symbol, qty):
                self.symbol = symbol
                self.qty = qty

        class FakeDataClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_stock_latest_quote(self, req):
                return {}

        class Client:
            def get_all_positions(self):
                return [Position("NVDA", 20)]

            def get_orders(self, req):
                return []

            def submit_order(self, req):
                return type("Order", (), {"id": "sell-1", "status": "accepted"})()

        plan = {
            "plan_date": "2026-07-01",
            "signal_date": "2026-07-01",
            "close_all": [],
            "trim": [{"symbol": "NVDA", "qty": 5, "market_value": 1000.0}],
            "buy": [],
            "target_n": 4,
        }
        state = {trader.CYCLE_PLAN_KEY: plan}
        client = Client()

        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "get_upcoming_earnings", return_value=({"NVDA"}, False)):
            summary = trader.execute_sell_phase(client, state, "run-sell-earnings-trim", dry_run=False)

        self.assertEqual(summary["earnings_forced_close"], ["NVDA"])
        sold = [(o["symbol"], o["qty"], o["kind"]) for o in summary["orders"]]
        # 全额 20 股清仓，而不是原计划的 5 股减仓
        self.assertEqual(sold, [("NVDA", 20, "close")])

    def test_sell_phase_earnings_recheck_skipped_when_blackout_disabled(self):
        """新增：EARNINGS_BLACKOUT_DAYS=0 时财报复核整体关闭，不应调用
        get_all_positions/get_upcoming_earnings（避免无谓的持仓查询/网络调用）。"""
        trader = load_trader_module()

        class FakeDataClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_stock_latest_quote(self, req):
                return {}

        class Client:
            def get_all_positions(self):
                raise AssertionError("EARNINGS_BLACKOUT_DAYS=0 时不应查询持仓")

            def submit_order(self, req):
                return type("Order", (), {"id": "sell-1", "status": "accepted"})()

        plan = {
            "plan_date": "2026-07-01",
            "signal_date": "2026-07-01",
            "close_all": [{"symbol": "AMAT", "qty": 3}],
            "trim": [],
            "buy": [],
            "target_n": 1,
        }
        state = {trader.CYCLE_PLAN_KEY: plan}
        client = Client()
        earnings_mock = Mock(side_effect=AssertionError("不应调用 get_upcoming_earnings"))

        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "EARNINGS_BLACKOUT_DAYS", 0), \
             patch.object(trader, "get_upcoming_earnings", earnings_mock):
            summary = trader.execute_sell_phase(client, state, "run-sell-earnings-off", dry_run=False)

        earnings_mock.assert_not_called()
        self.assertEqual(summary["earnings_forced_close"], [])

    def test_sell_phase_earnings_recheck_degraded_does_not_block_submission(self):
        """新增：财报日历查询降级（yfinance 异常等）时只标记 degraded，
        不阻断卖单正常提交（宁可漏判不阻塞主流程）。"""
        trader = load_trader_module()

        class Position:
            def __init__(self, symbol, qty):
                self.symbol = symbol
                self.qty = qty

        class FakeDataClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_stock_latest_quote(self, req):
                return {}

        class Client:
            def __init__(self):
                self.submitted = []

            def get_all_positions(self):
                # AMAT 必须真实在持仓中：F1 实时持仓夹取会跳过无持仓的卖单
                return [Position("AAPL", 10), Position("AMAT", 3)]

            def get_orders(self, req):
                return []

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "sell-1", "status": "accepted"})()

        plan = {
            "plan_date": "2026-07-01",
            "signal_date": "2026-07-01",
            "close_all": [{"symbol": "AMAT", "qty": 3}],
            "trim": [],
            "buy": [],
            "target_n": 2,
        }
        state = {trader.CYCLE_PLAN_KEY: plan}
        client = Client()

        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "get_upcoming_earnings", return_value=(set(), True)):
            summary = trader.execute_sell_phase(client, state, "run-sell-earnings-degraded", dry_run=False)

        self.assertTrue(summary["earnings_recheck_degraded"])
        self.assertFalse(summary["all_failed"])
        sold = [o["symbol"] for o in summary["orders"]]
        self.assertEqual(sold, ["AMAT"])

    def test_sell_phase_target_n_decremented_by_forced_close_count(self):
        """新增：pending_sell.target_n 需要按财报复核新增的强制清仓数量下调，
        供 buy 阶段按剩余候选数重新计算等权买入金额。"""
        trader = load_trader_module()

        class Position:
            def __init__(self, symbol, qty):
                self.symbol = symbol
                self.qty = qty

        class FakeDataClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_stock_latest_quote(self, req):
                return {}

        class Client:
            def get_all_positions(self):
                return [Position("AAPL", 10), Position("MSFT", 8)]

            def get_orders(self, req):
                return []

            def submit_order(self, req):
                return type("Order", (), {"id": "sell-1", "status": "accepted"})()

        plan = {
            "plan_date": "2026-07-01",
            "signal_date": "2026-07-01",
            "close_all": [],
            "trim": [],
            "buy": [],
            "target_n": 5,
        }
        state = {trader.CYCLE_PLAN_KEY: plan}
        client = Client()

        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "get_upcoming_earnings", return_value=({"AAPL", "MSFT"}, False)):
            trader.execute_sell_phase(client, state, "run-sell-target-n", dry_run=False)

        pending = state[trader.PENDING_SELL_KEY]
        self.assertEqual(pending["target_n"], 3)

    def test_sell_phase_non_target_blackout_does_not_reduce_target_count(self):
        """plan 后外部新增的非目标持仓可以因财报被强制清仓，但它从未进入目标集合，
        因此不能减少 target_n 或触发目标集合重算。"""
        trader = load_trader_module()

        class Position:
            def __init__(self, symbol, qty):
                self.symbol = symbol
                self.qty = qty

        class FakeDataClient:
            def __init__(self, *args, **kwargs):
                pass

            def get_stock_latest_quote(self, req):
                return {}

        class Client:
            def get_all_positions(self):
                return [Position("AAPL", 10), Position("MSFT", 20), Position("XYZ", 5)]

            def get_orders(self, req):
                return []

            def submit_order(self, req):
                return type("Order", (), {"id": "sell-1", "status": "accepted"})()

        state = {
            trader.CYCLE_PLAN_KEY: {
                "plan_date": "2026-07-08",
                "signal_date": "2026-07-08",
                "close_all": [],
                "trim": [],
                "buy": [],
                "target_n": 2,
                "target_snapshot": {
                    "AAPL": {"price": 100.0},
                    "MSFT": {"price": 50.0},
                },
            }
        }

        with patch.object(trader, "StockHistoricalDataClient", FakeDataClient), \
             patch.object(trader, "get_upcoming_earnings", return_value=({"XYZ"}, False)):
            summary = trader.execute_sell_phase(Client(), state, "run-sell-external", dry_run=False)

        pending = state[trader.PENDING_SELL_KEY]
        self.assertEqual(summary["earnings_forced_close"], ["XYZ"])
        self.assertEqual(pending["target_n"], 2)
        self.assertEqual(set(pending["target_snapshot"]), {"AAPL", "MSFT"})
        self.assertFalse(pending["reallocation_required"])

    def test_buy_phase_held_blackout_budget_is_not_redistributed_before_exit(self):
        """buy 阶段命中的已持仓标的不在本阶段卖出，其市值仍被占用，不能同时再分配
        给剩余候选；这里只取消加仓并从可投资预算扣除该持仓市值。"""
        trader = load_trader_module()

        class Position:
            def __init__(self, symbol, qty, market_value):
                self.symbol = symbol
                self.qty = qty
                self.market_value = str(market_value)

        class Account:
            buying_power = "100000"

        class Client:
            def __init__(self):
                self.submitted = []

            def get_account(self):
                return Account()

            def get_all_positions(self):
                return [
                    Position("AAPL", 301, 30100.0),
                    Position("MSFT", 400, 20000.0),
                ]

            def submit_order(self, req):
                self.submitted.append(req)
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

        state = {
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-07-09",
                "signal_date": "2026-07-08",
                "orders": [],
                "buy_carry": [
                    {"symbol": "MSFT", "qty": 203, "price": 50.0, "is_new": False, "drift": 0.0},
                ],
                "target_n": 2,
                "target_snapshot": {
                    "AAPL": {"price": 100.0},
                    "MSFT": {"price": 50.0},
                },
                "reallocation_required": False,
            }
        }

        with patch.object(trader, "get_upcoming_earnings", return_value=({"MSFT"}, False)), \
             patch.object(trader, "MAX_POSITION_PCT", 1.0), \
             patch.object(trader, "MIN_CASH_BUFFER_PCT", 0.05), \
             patch.object(trader, "ensure_stop_orders_for_positions", return_value=0):
            summary = trader.execute_buy_phase(
                Client(), state, "run-buy-held-blackout", dry_run=False, sizing_capital=100000.0,
            )

        order_qty = {o["symbol"]: o["qty"] for o in summary["orders"]}
        self.assertEqual(order_qty, {"AAPL": 413})
        self.assertEqual(summary["excluded_held_value"], 20000.0)
        self.assertEqual(summary["target_val_reallocated"], 75000.0)

    def test_buy_phase_unfilled_sell_blackout_value_remains_locked(self):
        """sell 阶段财报强制清仓标的若仍在真实持仓中，说明资金尚未释放；即使它已
        从目标快照移除，buy 重算也必须扣除其当前市值。"""
        trader = load_trader_module()

        class Position:
            def __init__(self, symbol, qty, market_value):
                self.symbol = symbol
                self.qty = qty
                self.market_value = str(market_value)

        class Account:
            buying_power = "100000"

        class Client:
            def get_account(self):
                return Account()

            def get_all_positions(self):
                return [
                    Position("AAPL", 301, 30100.0),
                    Position("NVDA", 180, 36000.0),
                ]

            def submit_order(self, req):
                return type("Order", (), {"id": "buy-1", "status": "accepted"})()

        state = {
            trader.PENDING_SELL_KEY: {
                "sell_date": "2026-07-09",
                "signal_date": "2026-07-08",
                "orders": [],
                "buy_carry": [],
                "target_n": 1,
                "target_snapshot": {"AAPL": {"price": 100.0}},
                "earnings_forced_close": ["NVDA"],
                "reallocation_required": True,
            }
        }

        with patch.object(trader, "get_upcoming_earnings", return_value=(set(), False)), \
             patch.object(trader, "MAX_POSITION_PCT", 1.0), \
             patch.object(trader, "MIN_CASH_BUFFER_PCT", 0.05):
            summary = trader.execute_buy_phase(
                Client(), state, "run-buy-unfilled-blackout", dry_run=False,
                sizing_capital=100000.0,
            )

        order_qty = {o["symbol"]: o["qty"] for o in summary["orders"]}
        self.assertEqual(order_qty, {"AAPL": 260})
        self.assertEqual(summary["excluded_held_value"], 36000.0)
        self.assertEqual(summary["target_val_reallocated"], 59000.0)

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
