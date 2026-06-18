import unittest

from tradebot.research import (
    AssetProfile,
    build_synthetic_universe,
    run_multi_asset_research,
    run_parameter_sensitivity,
    run_stress_suite,
)


class ResearchTest(unittest.TestCase):
    def test_parameter_sensitivity_returns_plateau_rows(self):
        rows = run_parameter_sensitivity(
            pullback_values=[0.006, 0.008, 0.010],
            seed=3,
        )

        self.assertEqual([row.parameter_value for row in rows], [0.006, 0.008, 0.010])
        self.assertTrue(all(row.metrics is not None for row in rows))

    def test_multi_asset_research_applies_global_t_exposure(self):
        profiles = [
            AssetProfile("NVDA", "科技巨头", 8, 10),
            AssetProfile("TSLA", "科技巨头", 12, 15),
            AssetProfile("CPOX", "CPO光通信", 35, 40),
        ]

        result = run_multi_asset_research(
            profiles=profiles,
            starting_quote=9000,
            global_t_max_exposure_pct=0.30,
            seed=5,
        )

        self.assertEqual(len(result.asset_results), 3)
        self.assertLessEqual(result.max_global_t_exposure, 9000 * 0.30 + 1)

    def test_stress_suite_includes_high_slippage_and_downtrend(self):
        rows = run_stress_suite(seed=9)
        names = {row.name for row in rows}

        self.assertIn("高滑点100bps", names)
        self.assertIn("连续五天下跌", names)

    def test_build_synthetic_universe_contains_requested_themes(self):
        universe = build_synthetic_universe(seed=11)
        symbols = {item.symbol for item in universe}

        self.assertTrue({"SPCX", "TSLA", "NVDA", "MU", "CPOX"}.issubset(symbols))


if __name__ == "__main__":
    unittest.main()
