import importlib.util
import sys
import unittest
from pathlib import Path


LIVE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = LIVE_DIR / "local_review_compare.py"


def load_compare_module():
    spec = importlib.util.spec_from_file_location("local_review_compare_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class LocalReviewCompareTests(unittest.TestCase):
    def test_compare_review_payload_flags_matching_targets_and_hashes(self):
        compare = load_compare_module()
        remote = {
            "latest_run": {"run_id": "run-1", "status": "ok"},
            "target_symbols": ["MU", "AMAT"],
            "universe": {"hash": "abc", "count": 2},
        }
        local = {
            "target_symbols": ["MU", "AMAT"],
            "universe": {"hash": "abc", "count": 2},
        }

        result = compare.compare_payloads(remote, local)

        self.assertTrue(result["ok"])
        self.assertEqual(result["checks"]["target_symbols"]["status"], "match")
        self.assertEqual(result["checks"]["universe_hash"]["status"], "match")

    def test_compare_review_payload_reports_mismatches(self):
        compare = load_compare_module()
        remote = {
            "target_symbols": ["MU", "AMAT"],
            "universe": {"hash": "abc", "count": 2},
        }
        local = {
            "target_symbols": ["AMAT", "MU"],
            "universe": {"hash": "def", "count": 2},
        }

        result = compare.compare_payloads(remote, local)

        self.assertFalse(result["ok"])
        self.assertEqual(result["checks"]["target_symbols"]["status"], "mismatch")
        self.assertEqual(result["checks"]["universe_hash"]["status"], "mismatch")

    def test_markdown_report_contains_review_sections(self):
        compare = load_compare_module()

        report = compare.format_markdown_report({
            "ok": False,
            "remote_run_id": "run-1",
            "checks": {
                "target_symbols": {"status": "mismatch", "remote": ["MU"], "local": ["AMAT"]},
            },
        })

        self.assertIn("云端 run_id: run-1", report)
        self.assertIn("target_symbols", report)
        self.assertIn("mismatch", report)

    def test_build_local_payload_uses_local_review_snapshot(self):
        compare = load_compare_module()

        payload = compare.build_local_payload(root=LIVE_DIR)

        self.assertIn("target_symbols", payload)
        self.assertIn("universe", payload)


if __name__ == "__main__":
    unittest.main()
