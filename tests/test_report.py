import unittest

from tradebot.metrics import BacktestMetrics
from tradebot.report import render_html_report
from tradebot.research import ResearchSummary


class ReportTest(unittest.TestCase):
    def test_html_report_is_chinese_first_and_has_left_glossary(self):
        summary = ResearchSummary(
            title="实盘前工业级回测报告",
            rows=[],
            sensitivity=[],
            stress=[],
            terminal_logs=["[INFO] [SIGNAL] NVDA 日K过滤通过"],
        )
        html = render_html_report(
            summary,
            aggregate_metrics=BacktestMetrics(
                total_return_pct=0.02,
                max_drawdown_pct=-0.03,
                win_rate=0.55,
                profit_factor=1.4,
                sharpe=1.1,
                sortino=1.8,
            ),
        )

        self.assertIn("金融概念逐字说明", html)
        self.assertIn("<aside", html)
        self.assertIn("夏普比率", html)
        self.assertIn("Terminal 风格执行 Log", html)
        self.assertIn("样本外测试", html)


if __name__ == "__main__":
    unittest.main()
