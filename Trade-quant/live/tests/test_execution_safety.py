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

        self.assertEqual(order.time_in_force, trader.TimeInForce.OPG)
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
            })
            audit.append_signal_rows("run-1", "2026-06-22", pd.Series({"AAPL": 1.23}), {"AAPL": 200.0})
            audit.append_order({
                "run_id": "run-1",
                "signal_date": "2026-06-22",
                "action": "BUY",
                "symbol": "AAPL",
            })

            self.assertIn("run-1", (Path(tmp) / "paper_runs.csv").read_text())
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


if __name__ == "__main__":
    unittest.main()
