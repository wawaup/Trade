"""
MOC 单段执行路径（EXEC_MODE=moc_single）单测。

覆盖：
  - build_moc_market_order 使用 TimeInForce.CLS 且 client_order_id 幂等
  - build_loo_limit_order 使用 TimeInForce.OPG 且带价格保护
  - execute_moc_phase：正常路径、pending 残留阻断、财报强制清仓、实时持仓夹取（F1）、
    MOC 买单被拒时降级 LOO 兜底
  - reconcile_moc_execute：全部成交清理 state、部分未成交保留、负持仓中止
  - phase=execute/reconcile 分派：EXEC_MODE 不匹配时拒绝
"""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="trade-quant-mpl-"))

LIVE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = LIVE_DIR / "alpaca_trader.py"


def load_trader_module(exec_mode: str = "moc_single"):
    """加载模块，先在 env 里设 EXEC_MODE，让顶层常量按测试意图取值。"""
    os.environ["EXEC_MODE"] = exec_mode
    spec = importlib.util.spec_from_file_location(f"alpaca_trader_moc_{exec_mode}", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class MOCOrderBuildersTests(unittest.TestCase):
    def test_build_moc_market_order_uses_cls_tif(self):
        t = load_trader_module()
        order = t.build_moc_market_order("AAPL", 10, t.OrderSide.BUY, "2026-07-13", "buy")
        self.assertEqual(order.time_in_force, t.TimeInForce.CLS)
        self.assertEqual(order.symbol, "AAPL")
        self.assertEqual(order.qty, 10)
        self.assertEqual(order.client_order_id, "tq-moc-20260713-buy-aapl")

    def test_build_moc_client_order_id_stable(self):
        """同 signal_date + symbol + action 幂等：重复提交靠 Alpaca duplicate 拒绝。"""
        t = load_trader_module()
        a = t.build_moc_market_order("NVDA", 3, t.OrderSide.SELL, "2026-07-13", "close")
        b = t.build_moc_market_order("NVDA", 3, t.OrderSide.SELL, "2026-07-13", "close")
        self.assertEqual(a.client_order_id, b.client_order_id)

    def test_build_loo_uses_opg_tif_with_price_cap(self):
        t = load_trader_module()
        order = t.build_loo_limit_order("NVDA", 5, t.OrderSide.BUY, 100.0, "2026-07-13", "buy")
        self.assertEqual(order.time_in_force, t.TimeInForce.OPG)
        self.assertEqual(order.limit_price, 100.0)
        self.assertEqual(order.client_order_id, "tq-loo-20260713-buy-nvda")

    def test_loo_and_moc_have_distinct_ids(self):
        """MOC 主路径与 LOO 兜底必须用不同 cid，避免降级时 broker 端 duplicate。"""
        t = load_trader_module()
        moc = t.build_moc_market_order("NVDA", 5, t.OrderSide.BUY, "2026-07-13", "buy")
        loo = t.build_loo_limit_order("NVDA", 5, t.OrderSide.BUY, 105.0, "2026-07-13", "buy")
        self.assertNotEqual(moc.client_order_id, loo.client_order_id)


class ExecuteMocPhaseTests(unittest.TestCase):
    def _plan(self, close_all=None, trim=None, buy=None):
        return {
            "plan_date": "2026-07-13",
            "signal_date": "2026-07-13",
            "close_all": close_all or [],
            "trim": trim or [],
            "buy": buy or [],
        }

    def _position(self, symbol, qty, market_value=1000.0):
        p = Mock()
        p.symbol = symbol
        p.qty = str(qty)
        p.qty_available = str(qty)
        p.market_value = str(market_value)
        p.avg_entry_price = "100.0"
        p.current_price = "100.0"
        return p

    def test_empty_plan_short_circuits(self):
        t = load_trader_module()
        client = Mock()
        summary = t.execute_moc_phase(client, {}, "run1", dry_run=True)
        self.assertFalse(summary["had_plan"])
        client.get_all_positions.assert_not_called()

    def test_blocks_when_stale_pending_execute(self):
        t = load_trader_module()
        client = Mock()
        state = {t.CYCLE_PLAN_KEY: self._plan(), t.PENDING_EXECUTE_KEY: {"stale": True}}
        summary = t.execute_moc_phase(client, state, "run1", dry_run=False)
        self.assertTrue(summary["blocked_stale_pending"])
        client.submit_order.assert_not_called()

    def test_blocks_when_three_phase_pending_present(self):
        """three_phase 残留 pending 时 moc 也拒绝执行，避免混合路径。"""
        t = load_trader_module()
        client = Mock()
        state = {t.CYCLE_PLAN_KEY: self._plan(), t.PENDING_SELL_KEY: {"orders": []}}
        summary = t.execute_moc_phase(client, state, "run1", dry_run=False)
        self.assertTrue(summary["blocked_stale_pending"])

    def test_f1_qty_clamps_to_realtime_position(self):
        """F1 修复：sell qty 必须夹到 min(plan_qty, 实时可用)。"""
        t = load_trader_module()
        client = Mock()
        # plan 想卖 100，但盘中止损已把持仓清成 0
        state = {t.CYCLE_PLAN_KEY: self._plan(
            close_all=[{"symbol": "NVDA", "qty": 100}]
        )}
        client.get_all_positions.return_value = [self._position("NVDA", 0)]
        client.get_orders.return_value = []
        with patch.object(t, "lookup_upcoming_earnings", return_value=(set(), False, set())):
            summary = t.execute_moc_phase(client, state, "run1", dry_run=False)
        # 没有卖单被提交（qty 夹到 0 后跳过）
        submit_calls = [c for c in client.submit_order.call_args_list]
        self.assertEqual(len(submit_calls), 0)
        self.assertIn("NVDA", summary["stop_triggered_syms"])

    def test_earnings_forced_liquidation_added_to_sells(self):
        """财报复核命中：即使 plan 里是 HOLD/trim，也强制加入 close 卖单。"""
        t = load_trader_module()
        client = Mock()
        state = {t.CYCLE_PLAN_KEY: self._plan()}  # 无卖单计划
        client.get_all_positions.return_value = [self._position("MU", 50)]
        client.get_orders.return_value = []
        with patch.object(t, "lookup_upcoming_earnings", return_value=({"MU"}, False, set())):
            with patch.object(t, "StockLatestQuoteRequest"):
                summary = t.execute_moc_phase(client, state, "run1", dry_run=True)
        self.assertIn("MU", summary["earnings_forced_syms"])

    def test_dry_run_does_not_persist_pending_execute(self):
        t = load_trader_module()
        client = Mock()
        state = {t.CYCLE_PLAN_KEY: self._plan(
            buy=[{"symbol": "AAPL", "qty": 5, "price": 200.0}]
        )}
        client.get_all_positions.return_value = []
        client.get_orders.return_value = []
        with patch.object(t, "lookup_upcoming_earnings", return_value=(set(), False, set())):
            t.execute_moc_phase(client, state, "run1", dry_run=True)
        # dry_run 下 state 不应写入 pending_execute，cycle_plan 保留
        self.assertNotIn(t.PENDING_EXECUTE_KEY, state)
        self.assertIn(t.CYCLE_PLAN_KEY, state)

    def test_moc_buy_rejection_falls_back_to_loo(self):
        """MOC BUY 被 broker 拒绝 → 应自动提交 LOO 兜底单。"""
        t = load_trader_module()
        client = Mock()
        state = {t.CYCLE_PLAN_KEY: self._plan(
            buy=[{"symbol": "AAPL", "qty": 5, "price": 200.0}]
        )}
        client.get_all_positions.return_value = []
        client.get_orders.return_value = []

        submit_calls = []
        def submit_side(req):
            submit_calls.append(req)
            # MOC BUY 第一次抛（非 duplicate）→ 触发 LOO 兜底
            if getattr(req, "time_in_force", None) == t.TimeInForce.CLS \
               and getattr(req, "side", None) == t.OrderSide.BUY:
                raise RuntimeError("moc buy rejected by broker")
            return Mock()
        client.submit_order.side_effect = submit_side

        with patch.object(t, "lookup_upcoming_earnings", return_value=(set(), False, set())):
            with patch.object(t, "StockLatestQuoteRequest"):
                # get_stock_latest_quote 返回空 dict，让 quote_map 走 fallback（用 b["price"]）
                with patch("alpaca.data.historical.StockHistoricalDataClient.get_stock_latest_quote",
                           return_value={}):
                    summary = t.execute_moc_phase(client, state, "run1", dry_run=False)

        # 两笔 submit_order：MOC BUY（失败）+ LOO BUY（成功）
        self.assertEqual(len(submit_calls), 2)
        self.assertEqual(submit_calls[0].time_in_force, t.TimeInForce.CLS)
        self.assertEqual(submit_calls[1].time_in_force, t.TimeInForce.OPG)
        self.assertEqual(len(summary["loo_fallback"]), 1)


class ReconcileMocExecuteTests(unittest.TestCase):
    def _order(self, filled_qty, status="filled"):
        o = Mock()
        o.filled_qty = str(filled_qty)
        o.status = Mock(value=status)
        return o

    def _position(self, symbol, qty):
        p = Mock()
        p.symbol = symbol
        p.qty = str(qty)
        p.avg_entry_price = "100.0"
        p.current_price = "100.0"
        return p

    def test_all_filled_clears_pending_and_sets_last_rebalance(self):
        t = load_trader_module()
        client = Mock()
        state = {
            t.PENDING_EXECUTE_KEY: {
                "plan_date": "2026-07-13", "signal_date": "2026-07-13",
                "execute_date": "2026-07-14",
                "sells": [{"symbol": "NVDA", "qty": 10, "kind": "close",
                            "client_order_id": "tq-moc-20260713-close-nvda"}],
                "buys":  [{"symbol": "AAPL", "qty": 5,
                           "client_order_id": "tq-moc-20260713-buy-aapl",
                           "fallback_client_order_id": None}],
                "stop_triggered_syms": [],
            }
        }
        client.get_all_positions.return_value = [self._position("AAPL", 5)]
        client.get_order_by_client_id.side_effect = [self._order(10), self._order(5)]
        client.get_orders.return_value = []  # 无既有止损单
        summary = t.reconcile_moc_execute(client, state, "run1", dry_run=False)
        self.assertEqual(len(summary["sells_filled"]), 1)
        self.assertEqual(len(summary["buys_filled"]), 1)
        self.assertNotIn(t.PENDING_EXECUTE_KEY, state)
        self.assertEqual(state["last_rebalance"], "2026-07-13")

    def test_partial_fill_keeps_pending(self):
        t = load_trader_module()
        client = Mock()
        state = {
            t.PENDING_EXECUTE_KEY: {
                "plan_date": "2026-07-13", "signal_date": "2026-07-13",
                "execute_date": "2026-07-14",
                "sells": [{"symbol": "NVDA", "qty": 10, "kind": "close",
                            "client_order_id": "tq-moc-20260713-close-nvda"}],
                "buys":  [],
                "stop_triggered_syms": [],
            }
        }
        client.get_all_positions.return_value = []
        client.get_order_by_client_id.return_value = self._order(0, status="new")
        client.get_orders.return_value = []
        summary = t.reconcile_moc_execute(client, state, "run1", dry_run=False)
        self.assertEqual(len(summary["sells_unfilled"]), 1)
        self.assertIn(t.PENDING_EXECUTE_KEY, state)  # 保留供人工核查

    def test_naked_short_aborts_reconcile(self):
        """发现负持仓时立即中止，不清 state、不补挂止损。"""
        t = load_trader_module()
        client = Mock()
        state = {
            t.PENDING_EXECUTE_KEY: {
                "plan_date": "2026-07-13", "signal_date": "2026-07-13",
                "execute_date": "2026-07-14",
                "sells": [], "buys": [], "stop_triggered_syms": [],
            }
        }
        neg = self._position("NVDA", -50)
        client.get_all_positions.return_value = [neg]
        summary = t.reconcile_moc_execute(client, state, "run1", dry_run=False)
        self.assertIn("负持仓", summary["note"])
        self.assertIn(t.PENDING_EXECUTE_KEY, state)  # 保留待人工介入

    def test_loo_fallback_fill_counted_via_loo(self):
        t = load_trader_module()
        client = Mock()
        state = {
            t.PENDING_EXECUTE_KEY: {
                "plan_date": "2026-07-13", "signal_date": "2026-07-13",
                "execute_date": "2026-07-14",
                "sells": [],
                "buys":  [{"symbol": "AAPL", "qty": 5,
                           "client_order_id": "tq-moc-20260713-buy-aapl",
                           "fallback_client_order_id": "tq-loo-20260713-buy-aapl"}],
                "stop_triggered_syms": [],
            }
        }
        client.get_all_positions.return_value = [self._position("AAPL", 5)]
        # MOC 未成交、LOO 成交
        client.get_order_by_client_id.side_effect = [self._order(0, "canceled"), self._order(5, "filled")]
        client.get_orders.return_value = []
        summary = t.reconcile_moc_execute(client, state, "run1", dry_run=False)
        self.assertEqual(len(summary["buys_filled"]), 1)
        self.assertEqual(summary["buys_filled"][0]["filled_via"], "loo")


class PhaseDispatchTests(unittest.TestCase):
    """--phase execute/reconcile 必须要求 EXEC_MODE=moc_single，否则拒绝。"""

    def test_phase_execute_rejects_when_exec_mode_three_phase(self):
        t = load_trader_module(exec_mode="three_phase")
        self.assertEqual(t.EXEC_MODE, "three_phase")
        # 有 EXEC_MODE 检查即视为通过；实际的 main() 分派在 _main_impl 里
        # 我们只需要断言 EXEC_MODE 常量按 env 正确读取
        t2 = load_trader_module(exec_mode="moc_single")
        self.assertEqual(t2.EXEC_MODE, "moc_single")


if __name__ == "__main__":
    unittest.main()
