import unittest
from pathlib import Path


LIVE_DIR = Path(__file__).resolve().parents[1]
CRON_SETUP = LIVE_DIR / "deploy" / "cron_setup.sh"


class DeployScriptTests(unittest.TestCase):
    def test_cron_setup_recreates_incomplete_virtualenv(self):
        content = CRON_SETUP.read_text(encoding="utf-8")

        self.assertIn('PIP_BIN="$VENV_DIR/bin/pip"', content)
        self.assertIn('[ ! -x "$PYTHON_BIN" ] || [ ! -x "$PIP_BIN" ]', content)
        self.assertIn('rm -rf "$VENV_DIR"', content)
        self.assertIn('"$PYTHON_BIN" -m pip install -q --upgrade pip', content)


if __name__ == "__main__":
    unittest.main()
