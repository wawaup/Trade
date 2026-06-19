from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendFilesTest(unittest.TestCase):
    def test_frontend_page_has_quantdinger_shell(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn('class="qd-app-shell"', html)
        self.assertIn('class="qd-sidebar"', html)
        self.assertIn("IDE 指标开发", html)
        self.assertIn("策略与实盘监控", html)
        self.assertIn("Paper 订单", html)
        self.assertIn("Paper订单", html)
        self.assertIn("金融概念逐字说明", html)
        self.assertIn("运行回测", html)
        self.assertIn("参数敏感度示例", html)
        self.assertIn("压力情景示例", html)
        self.assertNotIn(">智能调参<", html)
        self.assertNotIn(">压力测试<", html)
        self.assertIn("刷新数据", html)
        self.assertIn("买入做T", html)
        self.assertIn("卖出减T", html)
        self.assertIn("市价单", html)
        self.assertIn("限价单（暂未开放）", html)
        self.assertIn("创建策略（暂未开放）", html)
        self.assertIn("停止策略（暂未开放）", html)
        self.assertIn("disabled", html)
        self.assertIn('id="indicatorIdePage"', html)
        self.assertIn('id="strategyLivePage"', html)
        self.assertIn('id="paperOrdersPage"', html)
        self.assertIn('data-page-target="indicatorIdePage"', html)
        self.assertIn('data-page-target="strategyLivePage"', html)
        self.assertIn('data-page-target="paperOrdersPage"', html)
        self.assertIn('class="qd-code-panel"', html)
        self.assertIn('class="qd-market-stage"', html)
        self.assertIn('class="qd-quick-trade"', html)
        self.assertIn("knowledgeCard", html)
        self.assertNotIn("<details", html)
        self.assertIn("仓位分配设置", html)
        self.assertIn("allocationForm", html)
        self.assertIn("totalAccountQuote", html)
        self.assertIn("dataSourceSelect", html)
        self.assertIn("csvDailyPath", html)
        self.assertIn("csvIntradayPath", html)
        self.assertIn("K线 / VWAP 主视窗", html)
        self.assertIn("风控指示灯", html)
        self.assertIn('id="guardrails"', html)
        self.assertIn("lightweight-charts", html)
        self.assertNotIn("chart-candles", html)
        self.assertIn('tbody id="paperOrderRows"', html)
        self.assertIn('tbody id="resultHistoryRows"', html)
        self.assertIn('id="dataSourceCards"', html)
        forbidden_copy = [
            "CONTACT US",
            ">Support<",
            "Feature request",
            "Run Backtest",
            ">Long<",
            ">Short<",
            ">Market<",
            ">Limit<",
            "Create Strategy",
            "Stop Strategy",
        ]
        for copy in forbidden_copy:
            self.assertNotIn(copy, html)

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
        self.assertIn("collectDataSourceControls", js)
        self.assertIn("dailyPath", js)
        self.assertIn("intradayPath", js)
        self.assertIn("switchFactorTab", js)
        self.assertIn("validateAllocationForm", js)
        self.assertIn("scrollTop = term.scrollHeight", js)
        self.assertIn("refreshAllData", js)
        self.assertIn("submitPaperOrder", js)
        self.assertIn("switchOrderType", js)

    def test_platform_frontend_fetches_new_api_endpoints(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn("/api/data-sources", js)
        self.assertIn("/api/paper/orders", js)
        self.assertIn('method: "POST"', js)
        self.assertIn("/api/backtest/results", js)
        self.assertIn("function renderPaperOrders", js)
        self.assertIn("function renderDataSources", js)

    def test_quantdinger_shell_javascript_bindings_exist(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn("function switchWorkspace", js)
        self.assertIn("function renderStrategyList", js)
        self.assertIn("function renderQuickTradeAccount", js)
        self.assertIn("function renderStrategyPerformance", js)
        self.assertIn("paperOrderRowsFull", js)

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

    def test_platform_styles_define_new_surfaces(self):
        css = (ROOT / "web" / "styles.css").read_text(encoding="utf-8")

        self.assertIn(".qd-app-shell", css)
        self.assertIn(".qd-sidebar", css)
        self.assertIn(".qd-ide-grid", css)
        self.assertIn(".qd-code-panel", css)
        self.assertIn(".qd-market-stage", css)
        self.assertIn(".qd-quick-trade", css)
        self.assertIn(".source-card", css)
        self.assertIn(".paper-orders-table", css)
        self.assertIn(".result-history-table", css)


if __name__ == "__main__":
    unittest.main()
