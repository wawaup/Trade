import csv
import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path


LIVE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = LIVE_DIR / "service_watchdog.py"


def load_watchdog_module():
    spec = importlib.util.spec_from_file_location("service_watchdog_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ServiceWatchdogTests(unittest.TestCase):
    def write_runs(self, path: Path, rows: list[dict]):
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=["run_id", "run_at_utc", "status", "mode", "dry_run"])
            writer.writeheader()
            writer.writerows(rows)

    def test_detects_stale_service_when_last_run_exceeds_threshold(self):
        watchdog = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmp:
            runs_path = Path(tmp) / "paper_runs.csv"
            now = datetime(2026, 6, 23, 12, 0, 0)
            self.write_runs(runs_path, [{
                "run_id": "old-run",
                "run_at_utc": (now - timedelta(hours=26)).isoformat(timespec="seconds"),
                "status": "ok",
                "mode": "Paper",
                "dry_run": "False",
            }])

            result = watchdog.check_service_health(runs_path, now, max_age_hours=24)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "service_stale")
        self.assertEqual(result.last_run_id, "old-run")
        self.assertIn("26.0h", result.message)

    def test_accepts_recent_successful_run(self):
        watchdog = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmp:
            runs_path = Path(tmp) / "paper_runs.csv"
            now = datetime(2026, 6, 23, 12, 0, 0)
            self.write_runs(runs_path, [{
                "run_id": "fresh-run",
                "run_at_utc": (now - timedelta(hours=2)).isoformat(timespec="seconds"),
                "status": "ok",
                "mode": "Paper",
                "dry_run": "False",
            }])

            result = watchdog.check_service_health(runs_path, now, max_age_hours=24)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ok")
        self.assertEqual(result.last_run_id, "fresh-run")

    def test_flags_fresh_but_emergency_status_run(self):
        watchdog = load_watchdog_module()
        with tempfile.TemporaryDirectory() as tmp:
            runs_path = Path(tmp) / "paper_runs.csv"
            now = datetime(2026, 6, 23, 12, 0, 0)
            self.write_runs(runs_path, [{
                "run_id": "failed-run",
                "run_at_utc": (now - timedelta(hours=1)).isoformat(timespec="seconds"),
                "status": "order_submit_failed",
                "mode": "Paper",
                "dry_run": "False",
            }])

            result = watchdog.check_service_health(runs_path, now, max_age_hours=24)

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "last_run_failed")
        self.assertEqual(result.last_run_id, "failed-run")


if __name__ == "__main__":
    unittest.main()
