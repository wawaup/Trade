from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendFilesTest(unittest.TestCase):
    def test_frontend_page_has_two_main_sections_and_glossary(self):
        html = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

        self.assertIn("策略历史回测", html)
        self.assertIn("当前实盘交易", html)
        self.assertIn("金融概念逐字说明", html)
        self.assertIn('data-page-target="backtestPage"', html)
        self.assertIn('data-page-target="livePage"', html)
        self.assertIn("top-page-nav", html)
        self.assertIn("knowledgeCard", html)
        self.assertLess(html.index("top-page-nav"), html.index("status-strip"))
        self.assertNotIn("<details", html)

    def test_frontend_script_polls_live_state(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn("/api/state", js)
        self.assertIn("setInterval", js)
        self.assertIn("switchPage", js)
        self.assertIn("toggleKnowledgeCard", js)
        self.assertIn("collapseKnowledgeCard", js)
        self.assertIn("contains(event.target)", js)

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


if __name__ == "__main__":
    unittest.main()
