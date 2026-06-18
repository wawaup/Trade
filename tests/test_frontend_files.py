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

    def test_frontend_script_polls_live_state(self):
        js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")

        self.assertIn("/api/state", js)
        self.assertIn("setInterval", js)
        self.assertIn("switchPage", js)


if __name__ == "__main__":
    unittest.main()
