import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


LIVE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = LIVE_DIR / "universe_monitor.py"


def load_monitor_module():
    spec = importlib.util.spec_from_file_location("universe_monitor_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class UniverseMonitorTests(unittest.TestCase):
    def test_compare_symbol_lists_reports_added_removed_and_hashes(self):
        monitor = load_monitor_module()

        diff = monitor.compare_symbol_lists(["AAPL", "MSFT", "TSLA"], ["AAPL", "NVDA", "TSLA"])

        self.assertEqual(diff["added"], ["NVDA"])
        self.assertEqual(diff["removed"], ["MSFT"])
        self.assertTrue(diff["changed"])
        self.assertEqual(diff["previous_count"], 3)
        self.assertEqual(diff["current_count"], 3)
        self.assertNotEqual(diff["previous_hash"], diff["current_hash"])

    def test_write_change_snapshot_appends_jsonl_record(self):
        monitor = load_monitor_module()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "universe_changes.jsonl"
            record = {
                "run_at_utc": "2026-06-23T00:00:00Z",
                "source": "fallback",
                "changed": True,
                "added": ["NVDA"],
                "removed": [],
            }

            monitor.append_change_snapshot(path, record)

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows, [record])

    def test_monitor_universe_records_fallback_status_without_overwriting_by_default(self):
        monitor = load_monitor_module()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            universe_path = tmp_path / "universe.json"
            changes_path = tmp_path / "universe_changes.jsonl"
            universe_path.write_text(json.dumps({
                "symbols": ["AAPL", "MSFT"],
                "benchmarks": ["SPY", "QQQ"],
            }), encoding="utf-8")

            result = monitor.monitor_universe(
                universe_path=universe_path,
                changes_path=changes_path,
                build_universe_fn=lambda: {
                    "symbols": ["AAPL", "NVDA"],
                    "benchmarks": ["SPY", "QQQ"],
                    "source_status": {"ARKK_ARKW": "fallback"},
                },
                auto_refresh=False,
            )

            self.assertEqual(result["source_status"]["ARKK_ARKW"], "fallback")
            self.assertEqual(result["added"], ["NVDA"])
            self.assertEqual(result["removed"], ["MSFT"])
            persisted = json.loads(universe_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["symbols"], ["AAPL", "MSFT"])
            self.assertIn('"changed": true', changes_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
