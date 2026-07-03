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

    def test_cron_setup_replaces_managed_cron_block(self):
        content = CRON_SETUP.read_text(encoding="utf-8")

        self.assertIn("TRADE_QUANT_CRON_BEGIN", content)
        self.assertIn("TRADE_QUANT_CRON_END", content)
        self.assertIn("awk", content)
        self.assertIn('skip=1', content)
        self.assertIn('skip=0', content)
        self.assertNotIn('grep -v "run_trader.sh"', content)

    def test_cron_setup_registers_universe_monitor_and_review_api_hint(self):
        content = CRON_SETUP.read_text(encoding="utf-8")

        self.assertIn("UNIVERSE_CMD=", content)
        self.assertIn("universe_monitor.py", content)
        self.assertIn("universe_monitor.log", content)
        self.assertIn("review_api.py --host 127.0.0.1 --port 8765", content)

    def test_three_phase_rebalance_crons_are_registered(self):
        content = CRON_SETUP.read_text(encoding="utf-8")

        self.assertIn('echo "CRON_TZ=America/New_York"', content)
        self.assertIn('echo "TZ=America/New_York"', content)
        self.assertIn('PLAN_CMD="05 16 * * 1-5', content)
        self.assertIn('SELL_CMD="50 15 * * 1-5', content)
        self.assertIn('CRON_CMD="15 09 * * 1-5', content)
        self.assertIn("--phase plan", content)
        self.assertIn("--phase sell", content)
        self.assertIn("--phase buy", content)
        self.assertIn("TZ=America/New_York VENV_PYTHON=$PYTHON_BIN", content)

    def test_watchdog_runs_after_main_trader_cron(self):
        content = CRON_SETUP.read_text(encoding="utf-8")

        self.assertIn('WATCHDOG_CMD="20 20 * * 1-5', content)

    def test_cron_setup_tightens_sensitive_file_permissions(self):
        content = CRON_SETUP.read_text(encoding="utf-8")

        self.assertIn('chmod 600 "$ENV_FILE"', content)
        self.assertIn('chmod 750 "$LOG_DIR"', content)
        self.assertIn('chmod 600 "$STATE_FILE"', content)

    def test_run_trader_wraps_execution_with_timeout(self):
        RUN_TRADER = LIVE_DIR / "deploy" / "run_trader.sh"
        content = RUN_TRADER.read_text(encoding="utf-8")

        self.assertIn('timeout "$TIMEOUT_SEC"', content)

    def test_live_requirements_include_research_runtime_imports(self):
        content = REQUIREMENTS.read_text(encoding="utf-8")

        self.assertIn("matplotlib", content)
        self.assertIn("scipy", content)


if __name__ == "__main__":
    unittest.main()
