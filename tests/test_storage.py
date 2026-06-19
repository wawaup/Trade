import tempfile
import unittest
from pathlib import Path

from tradebot.storage import BacktestResultStore


class BacktestResultStoreTests(unittest.TestCase):
    def test_append_list_get_and_reload_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "backtests.json"
            store = BacktestResultStore(path)

            first = store.append({"resultId": "bt-0001", "summary": {"totalReturnPct": 0.01}})
            second = store.append({"resultId": "bt-0002", "summary": {"totalReturnPct": 0.02}})

            self.assertEqual(first["resultId"], "bt-0001")
            self.assertEqual(second["resultId"], "bt-0002")
            self.assertEqual([row["resultId"] for row in store.list_recent()], ["bt-0001", "bt-0002"])

            reloaded = BacktestResultStore(path)
            self.assertEqual(reloaded.get("bt-0001")["summary"]["totalReturnPct"], 0.01)
            self.assertEqual(reloaded.get("bt-0002")["summary"]["totalReturnPct"], 0.02)
            self.assertIsNone(reloaded.get("missing"))

    def test_list_recent_limits_to_newest_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            store = BacktestResultStore(Path(tmpdir) / "backtests.json")
            for idx in range(4):
                store.append({"resultId": f"bt-{idx}", "summary": {"idx": idx}})

            recent = store.list_recent(limit=2)

            self.assertEqual([row["resultId"] for row in recent], ["bt-2", "bt-3"])

    def test_clear_removes_persisted_results(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "backtests.json"
            store = BacktestResultStore(path)
            store.append({"resultId": "bt-0001", "summary": {}})

            store.clear()

            self.assertEqual(store.list_recent(), [])
            self.assertEqual(BacktestResultStore(path).list_recent(), [])


if __name__ == "__main__":
    unittest.main()
