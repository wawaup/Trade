from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendFilesTest(unittest.TestCase):
    def test_frontend_page_has_platform_sections_and_glossary(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("研究仪表盘", html)
        self.assertIn("策略回测", html)
        self.assertIn("Paper订单", html)
        self.assertIn("数据与结果", html)
        self.assertIn("金融概念逐字说明", html)
        self.assertIn('id="researchPage"', html)
        self.assertIn('id="backtestPage"', html)
        self.assertIn('id="paperOrdersPage"', html)
        self.assertIn('id="dataResultsPage"', html)
        self.assertIn('data-page-target="researchPage"', html)
        self.assertIn('data-page-target="backtestPage"', html)
        self.assertIn('data-page-target="paperOrdersPage"', html)
        self.assertIn('data-page-target="dataResultsPage"', html)
        self.assertIn("top-page-nav", html)
        self.assertIn("knowledgeCard", html)
        self.assertLess(html.index("top-page-nav"), html.index("status-strip"))
        self.assertNotIn("<details", html)
        self.assertIn("仓位分配设置", html)
        self.assertIn("allocationForm", html)
        self.assertIn("totalAccountQuote", html)
        self.assertIn("research-lab-layout", html)
        self.assertIn("strategy-control-panel", html)
        self.assertIn("quant-workspace", html)
        self.assertIn("factor-tabs", html)
        self.assertIn("live-trading-layout", html)
        self.assertIn("live-left-panel", html)
        self.assertIn("live-center-panel", html)
        self.assertIn("live-right-panel", html)
        self.assertIn("K线 / VWAP 主视窗", html)
        self.assertIn("风控指示灯", html)
        self.assertIn("lightweight-charts", html)
        self.assertNotIn("chart-candles", html)
        self.assertIn("data-factor-panel=\"assets\"", html)
        self.assertIn("data-factor-panel=\"sensitivity\"", html)
        self.assertIn("data-factor-panel=\"stress\"", html)
        self.assertIn('tbody id="paperOrderRows"', html)
        self.assertIn('tbody id="resultHistoryRows"', html)
        self.assertIn('id="dataSourceCards"', html)

    def test_frontend_script_polls_live_state(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn("/api/state", js)
        self.assertIn("setInterval", js)
        self.assertIn("switchPage", js)
        self.assertIn("toggleKnowledgeCard", js)
        self.assertIn("collapseKnowledgeCard", js)
        self.assertIn("contains(event.target)", js)
        self.assertIn("/api/allocation", js)
        self.assertIn("renderAllocationForm", js)
        self.assertIn("saveAllocation", js)
        self.assertIn("renderRiskLights", js)
        self.assertIn("renderChartPlaceholder", js)
        self.assertIn("/api/state/static", js)
        self.assertIn("/api/state/live", js)
        self.assertIn("/api/backtest/run", js)
        self.assertIn("/api/klines", js)
        self.assertIn("initTradingChart", js)
        self.assertIn("runBacktestFromControls", js)
        self.assertIn("switchFactorTab", js)
        self.assertIn("validateAllocationForm", js)
        self.assertIn("scrollTop = term.scrollHeight", js)

    def test_platform_frontend_fetches_new_api_endpoints(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn("/api/data-sources", js)
        self.assertIn("/api/paper/orders", js)
        self.assertIn("/api/backtest/results", js)
        self.assertIn("function renderPaperOrders", js)
        self.assertIn("function renderDataSources", js)

    def test_docker_service_files_exist(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        self.assertIn("tradebot.cli", dockerfile)
        self.assertIn("8765", compose)

    def test_knowledge_card_is_left_bottom(self):
        css = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")

        self.assertIn(".knowledge-card", css)
        self.assertIn("left: var(--space-xl)", css)
        self.assertIn("bottom: var(--space-xl)", css)
        self.assertNotIn("right: var(--space-xl)", css)
        self.assertIn("scrollbar-color", css)
        self.assertIn(".knowledge-body::-webkit-scrollbar-thumb", css)
        self.assertIn("@keyframes pulse-danger", css)
        self.assertIn(".risk-light.danger", css)
        self.assertIn(".field input.error", css)


if __name__ == "__main__":
    unittest.main()
