import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


RESEARCH_DIR = Path(__file__).resolve().parents[2] / "research"
MODULE_PATH = RESEARCH_DIR / "build_universe.py"


def load_build_universe_module():
    spec = importlib.util.spec_from_file_location("build_universe_metadata_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BuildUniverseMetadataTests(unittest.TestCase):
    def test_build_universe_records_arkk_source_status(self):
        build_universe = load_build_universe_module()

        with patch.object(build_universe, "fetch_arkk_with_status", return_value=(["TSLA", "ROKU"], "online")):
            universe = build_universe.build_universe()

        self.assertEqual(universe["source_status"]["ARKK_ARKW"], "online")

    def test_try_fetch_arkk_preserves_legacy_list_return(self):
        build_universe = load_build_universe_module()

        with patch.object(build_universe, "fetch_arkk_with_status", return_value=(["TSLA"], "fallback")):
            self.assertEqual(build_universe.try_fetch_arkk(), ["TSLA"])


if __name__ == "__main__":
    unittest.main()
