import unittest
from pathlib import Path


LIVE_DIR = Path(__file__).resolve().parents[1]
CRON_SETUP = LIVE_DIR / "deploy" / "cron_setup.sh"
REQUIREMENTS = LIVE_DIR / "requirements.txt"


class DeployScriptTests(unittest.TestCase):
    def test_cron_setup_recreates_incomplete_virtualenv(self):
        content = CRON_SETUP.read_text(encoding="utf-8")

        self.assertIn('PIP_BIN="$VENV_DIR/bin/pip"', content)
        self.assertIn('[ ! -x "$PYTHON_BIN" ] || [ ! -x "$PIP_BIN" ]', content)
        self.assertIn('rm -rf "$VENV_DIR"', content)
        self.assertIn('"$PYTHON_BIN" -m pip install -q --upgrade pip', content)

    def test_cron_setup_does_not_install_full_research_requirements_by_default(self):
        content = CRON_SETUP.read_text(encoding="utf-8")

        self.assertNotIn('RESEARCH_REQ="$(dirname "$PROJECT_ROOT")/research/requirements.txt"', content)
        self.assertNotIn('-r "$RESEARCH_REQ"', content)
        self.assertIn("live 交易脚本不默认安装完整 research 依赖", content)

    def test_live_requirements_include_research_runtime_imports(self):
        content = REQUIREMENTS.read_text(encoding="utf-8")

        self.assertIn("matplotlib", content)
        self.assertIn("scipy", content)


if __name__ == "__main__":
    unittest.main()
