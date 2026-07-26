"""daily_reconciliation.build_report 的单元测试。

全部走纯函数路径（无网络/文件 IO）；broker 对象用最小假类模拟。
每个用例对应脚本 docstring 里编号的一条对账项。
"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import daily_reconciliation as dr


class Position:
    def __init__(self, symbol, qty, market_value):
        self.symbol = symbol
        self.qty = qty
        self.market_value = market_value


class Order:
    def __init__(self, symbol, qty, side, client_order_id, status="new"):
        self.symbol = symbol
        self.qty = qty
        self.side = side
        self.client_order_id = client_order_id
        self.status = status


TODAY = date(2026, 7, 27)
CLEAN_STATE = {"high_watermark": 100_000.0, "last_rebalance": "2026-07-20",
               "kill_switch": False}


def report(state=None, equity=90_000.0, positions=(), orders=(),
           max_position_pct=0.50, enable_stops=True, today=TODAY):
    return dr.build_report(dict(CLEAN_STATE, **(state or {})), equity,
                           list(positions), list(orders),
                           max_position_pct=max_position_pct,
                           enable_stops=enable_stops, today=today)


class BuildReportTests(unittest.TestCase):

    def test_clean_account_no_issues(self):
        r = report(positions=[Position("NVDA", 10, 1900.0)],
                   orders=[Order("NVDA", 10, "sell", "tq-stop-nvda-1")])
        self.assertEqual(r["issues"], [])
        self.assertEqual(r["notes"], [])

    def test_negative_position_is_issue(self):
        r = report(positions=[Position("AMAT", -3, -1800.0)])
        self.assertTrue(any("负持仓" in i and "AMAT" in i for i in r["issues"]))

    def test_concentration_breach_is_issue(self):
        # DOCN 事故重演：$99k 持仓 / $89.8k 净值 ≈ 110% > 50% 上限
        r = report(equity=89_800.0,
                   positions=[Position("DOCN", 678, 99_000.0)],
                   orders=[Order("DOCN", 678, "sell", "tq-stop-docn-1")])
        self.assertTrue(any("集中度超限" in i and "DOCN" in i for i in r["issues"]))

    def test_concentration_within_tolerance_not_issue(self):
        # 50% 上限 × 1.05 容差 = 52.5%；51% 不应报警
        r = report(equity=100_000.0,
                   positions=[Position("NVDA", 100, 51_000.0)],
                   orders=[Order("NVDA", 100, "sell", "tq-stop-nvda-1")])
        self.assertFalse(any("集中度" in i for i in r["issues"]))

    def test_missing_stop_without_inflight_is_issue(self):
        r = report(positions=[Position("NVDA", 10, 1900.0)])
        self.assertTrue(any("无止损保护" in i and "NVDA" in i for i in r["issues"]))

    def test_missing_stop_with_inflight_cycle_is_note_only(self):
        r = report(state={"pending_execute": {"execute_date": str(TODAY),
                                              "sells": [], "buys": []}},
                   positions=[Position("NVDA", 10, 1900.0)])
        self.assertFalse(any("无止损保护" in i for i in r["issues"]))
        self.assertTrue(any("无止损保护" in n for n in r["notes"]))

    def test_stops_disabled_no_missing_stop_issue(self):
        r = report(positions=[Position("NVDA", 10, 1900.0)], enable_stops=False)
        self.assertFalse(any("止损" in i for i in r["issues"]))

    def test_orphan_stop_is_issue(self):
        r = report(orders=[Order("KLAC", 5, "sell", "tq-stop-klac-1")])
        self.assertTrue(any("孤儿止损" in i and "KLAC" in i for i in r["issues"]))

    def test_inactive_orphan_stop_ignored(self):
        r = report(orders=[Order("KLAC", 5, "sell", "tq-stop-klac-1", status="canceled")])
        self.assertEqual(r["issues"], [])

    def test_stop_qty_exceeds_position_is_issue(self):
        r = report(positions=[Position("NVDA", 5, 950.0)],
                   orders=[Order("NVDA", 8, "sell", "tq-stop-nvda-1")])
        self.assertTrue(any("止损超量" in i for i in r["issues"]))

    def test_stop_qty_below_position_is_note(self):
        r = report(positions=[Position("NVDA", 10, 1900.0)],
                   orders=[Order("NVDA", 6, "sell", "tq-stop-nvda-1")])
        self.assertFalse(any("止损不足量" in i for i in r["issues"]))
        self.assertTrue(any("止损不足量" in n for n in r["notes"]))

    def test_foreign_order_is_issue(self):
        r = report(orders=[Order("TSLA", 100, "buy", "manual-web-123")])
        self.assertTrue(any("意外挂单" in i and "TSLA" in i for i in r["issues"]))

    def test_kill_switch_with_position_is_issue(self):
        r = report(state={"kill_switch": True},
                   positions=[Position("NVDA", 10, 1900.0)],
                   orders=[Order("NVDA", 10, "sell", "tq-stop-nvda-1")])
        self.assertTrue(any("kill_switch=true 但仍有持仓" in i for i in r["issues"]))

    def test_equity_above_hwm_beyond_tol_is_issue(self):
        r = report(equity=101_000.0)  # hwm=100k，超 0.5% 容差
        self.assertTrue(any("高水位未更新" in i for i in r["issues"]))

    def test_equity_above_hwm_within_tol_not_issue(self):
        r = report(equity=100_400.0)
        self.assertFalse(any("高水位" in i for i in r["issues"]))

    def test_stale_pending_execute_is_issue(self):
        r = report(state={"pending_execute": {"execute_date": "2026-07-20",
                                              "sells": [], "buys": []}})
        self.assertTrue(any("pending_execute 已滞留" in i for i in r["issues"]))

    def test_fresh_pending_execute_not_stale(self):
        r = report(state={"pending_execute": {"execute_date": "2026-07-26",
                                              "sells": [], "buys": []}})
        self.assertFalse(any("滞留" in i for i in r["issues"]))

    def test_pending_without_date_is_issue(self):
        r = report(state={"pending_buy": {"orders": []}})
        self.assertTrue(any("无法判断新鲜度" in i for i in r["issues"]))

    def test_format_report_renders_issues_and_notes(self):
        r = report(positions=[Position("AMAT", -3, -1800.0)])
        text = dr.format_report(r)
        self.assertIn("🚨 差异", text)
        self.assertIn("负持仓", text)


if __name__ == "__main__":
    unittest.main()
