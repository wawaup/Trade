import unittest

from tradebot.dashboard import build_dashboard_state


class DashboardTest(unittest.TestCase):
    def test_dashboard_state_has_backtest_and_live_sections(self):
        state = build_dashboard_state(seed=12)

        self.assertIn("backtest", state)
        self.assertIn("live", state)
        self.assertIn("glossary", state)
        self.assertGreaterEqual(len(state["backtest"]["assets"]), 3)
        self.assertIn("当前实盘交易", state["live"]["title"])

    def test_glossary_is_chinese_first_for_beginners(self):
        state = build_dashboard_state(seed=12)
        terms = [item["term"] for item in state["glossary"]]

        self.assertIn("VWAP / 成交量加权均价", terms)
        self.assertIn("Max Drawdown / 最大回撤", terms)


if __name__ == "__main__":
    unittest.main()
