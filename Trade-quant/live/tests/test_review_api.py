import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


LIVE_DIR = Path(__file__).resolve().parents[1]
MODULE_PATH = LIVE_DIR / "review_api.py"


def load_review_module():
    spec = importlib.util.spec_from_file_location("review_api_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReviewApiTests(unittest.TestCase):
    def test_authorize_requires_bearer_token(self):
        review = load_review_module()

        self.assertFalse(review.authorize("", "secret"))
        self.assertFalse(review.authorize("Bearer wrong", "secret"))
        self.assertTrue(review.authorize("Bearer secret", "secret"))

    def test_build_latest_review_payload_reads_latest_run_related_rows(self):
        review = load_review_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            audit_dir = root / "audit"
            audit_dir.mkdir()
            (audit_dir / "paper_runs.csv").write_text(
                "run_id,run_at_utc,status,mode,dry_run,target_symbols,equity,regime\n"
                "old,2026-06-22T00:00:00,ok,Paper,True,,100000,牛市\n"
                "new,2026-06-23T00:00:00,ok,Paper,True,\"MU,AMAT\",100000,牛市\n",
                encoding="utf-8",
            )
            (audit_dir / "signals.csv").write_text(
                "run_id,signal_date,rank,symbol,combo_score,signal_close\n"
                "new,2026-06-22,1,MU,1.106,1211.38\n"
                "new,2026-06-22,2,AMAT,1.032,640.10\n",
                encoding="utf-8",
            )
            (audit_dir / "orders.csv").write_text(
                "run_id,signal_date,action,symbol,qty,order_type,time_in_force,client_order_id,status,message\n"
                "new,2026-06-22,BUY,MU,41,market,opg,tq-20260622-enter-mu,dry_run,reference_price=1211.3800\n",
                encoding="utf-8",
            )
            (root / "trader.log").write_text("line1\nline2\n", encoding="utf-8")
            (root / "state.json").write_text(json.dumps({"high_watermark": 100000}), encoding="utf-8")
            data_dir = root.parent / "data"
            data_dir.mkdir(exist_ok=True)
            (data_dir / "universe.json").write_text(json.dumps({
                "symbols": ["MU", "AMAT"],
                "benchmarks": ["SPY", "QQQ"],
                "built_date": "2026-06-23",
            }), encoding="utf-8")

            payload = review.build_latest_review_payload(root)

            self.assertEqual(payload["latest_run"]["run_id"], "new")
            self.assertEqual(payload["latest_run"]["equity"], "100000")
            self.assertEqual([row["symbol"] for row in payload["signals"]], ["MU", "AMAT"])
            self.assertEqual(payload["target_symbols"], ["MU", "AMAT"])
            self.assertEqual(payload["orders"][0]["symbol"], "MU")
            self.assertEqual(payload["universe"]["count"], 2)
            self.assertTrue(payload["universe"]["hash"])
            self.assertIn("line2", payload["log_tail"])

    def test_route_health_and_latest_review_respect_auth(self):
        review = load_review_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "audit").mkdir()

            status, headers, body = review.route_request("/health", "", "secret", root)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["status"], "ok")

            status, headers, body = review.route_request("/review/latest", "", "secret", root)
            self.assertEqual(status, 401)

            status, headers, body = review.route_request("/review/latest", "Bearer secret", "secret", root)
            self.assertEqual(status, 200)
            self.assertIn("latest_run", json.loads(body))


if __name__ == "__main__":
    unittest.main()
